# Extending the app, and gotchas

## Add a world (no code changes)

1. `python cli.py new <id> --title "..."` (copies `templates/starter.json`), or
   `python cli.py draft "<premise>" --id <id>` on the dev machine.
2. Edit `worlds/<id>.json` (VS Code validates against the schema as you type). Guidance for the
   narrator: present tense, second person, short concrete item names with a distinctive last word,
   people are "they" (gendered prose makes the guards fall back to plain text), describe fixtures
   in the room `desc` (only portable items and people are listed automatically).
3. `python cli.py map <id>`, `python cli.py play <id> --debug`, then
   `python cli.py check <id> --all-modes` until clean, and `python cli.py solve <id>` to review
   traps and closing moves (with `--drops` for the slow, thorough version).
4. Add a route per ending to `ROUTES` in `tests/test_endings.py` (rule-parser commands only;
   the solver's routes are a good starting point).
5. Update the hard-coded world set in `tests/test_app.py::test_worlds_and_new_game`.
6. Consider adding parse eval cases for the new world's phrasings (none exist for fogbell yet).
7. Run all tests. Update the README games table, `app.yaml` description, and (in the platform
   repo) `portal/apps.json` description if they list games.

On the box a world can also be added without a new image by mounting a folder at `WORLDS_DIR`
(locally: drop the file in `./games`, git-ignored, and restart the container). A broken file
stops the server from starting.

## Add a world-specific verb

Use `"verbs": {"pray": ["kneel"]}` plus at least one interaction with `"verb": "pray"`. That's
all: the rule parser, `cue_verb`, the fallback, the model's enum and system prompt, and the engine
dispatch pick it up automatically. A verb that needs item-on-item logic should be a `use` rule
instead.

## Add a new world key / mechanic

Engine features must stay generic (no game names in code). Change, in step:
1. `engine.py`: the behaviour (and `make_instance`, `snapshot`/`restore`, `clone`, `_signature`,
   `state_key` in `solver.py` if it adds mutable state; a state change missing from
   `_signature` makes the turn free, and missing from `state_key` makes the solver merge distinct
   positions).
2. `validate()` in `engine.py`: cross-reference checks and fallbacks.
3. `schemas/world.schema.json` (`additionalProperties: false` everywhere, so unknown keys fail the
   schema test).
4. `docs/writing-a-game.md`, and `DRAFT_RULES` in `cli.py` if Claude drafts should use it.
5. Tests in `tests/test_engine.py` or `tests/test_kit.py`; solver actions in `solver.actions` if it
   introduces a new kind of move.

## Improve the parser

1. Reproduce: `python evals/parse_eval.py --show` lists misses. Real failures come from the box via
   `GET /api/stats?samples=1` (`samples.unparsed`).
2. Add the failing phrasings to `evals/parse_cases.json` (never to the held-out file).
3. Fix generically, usually in `parsing.py` (`FALLBACK_VERBS`, `CUE_VERBS`, `PHRASES`,
   `fallback_parse` rules, `build_command`) or `engine.parse_rule` (keep it exact: return `None`
   when unsure rather than guess).
4. Re-run the eval: the tuned set must stay >= 95% and held-out >= 50% (`test_kit` floors); look at
   held-out only as a final honest score. Also run `test_endings` (routes go through
   `parse_rule`) and `test_kit` (solver routes must replay).
5. Now and then add a fresh held-out batch written without looking at the parser.

Caution with `CUE_VERBS`: a cue overrides the model's verb whenever nouns were mentioned, so only
add words that mean exactly one verb anywhere in a sentence.

## Add or change a model feature

Rule: the model never decides state. Pattern used by all three jobs:
1. A narrow prompt with a fixed system message and few-shot pairs (fixed prefix for
   `cache_prompt`), tight `max_tokens`.
2. Structured output via `chat(..., schema=...)` when the answer is a choice (enums limited to what
   is valid right now, like the current room's exits).
3. A deterministic validator returning a reason string, counted in `STATS` and sampled in `SAMPLES`.
4. One retry at a lower temperature, then a plain fallback that needs no model.
5. A no-model path (`LLM_URL` empty) and a test with a fake `server.chat`.
Few-shot examples must use an invented setting, with its distinctive words added to an
`*EXAMPLE_ONLY` set so copying is detected.

## Gotchas

- **Relative URLs only** in pages (`api/...`, `party`); an absolute `/api/...` breaks behind the
  `/darklight/` prefix.
- **`LLM_URL` code default is `http://llm:8080`, not empty.** Running `uvicorn app:app` locally
  without setting `LLM_URL=""` makes every free-text turn try the model first (falls back after the
  connection error; a hung host means up to 45 s per call). Tests set it to `""` before import.
- **Single worker only**: sessions, parties, stats and caches are in process memory.
- **Free turns depend on `_signature()`**: anything a turn can change must be in it, or that turn
  costs no time.
- **Multi-object commands cost one turn** total ("take all"); the solver doesn't use them, so its
  fastest routes are upper bounds.
- **`mentions()` searches the whole world**, not just visible items (visible first). The model path
  can produce commands about absent items; the engine answers "You don't see that here."
- **Alias words under 3 letters** (and filler words) are ignored by fuzzy matching; `resolve()` in
  the rule parser uses exact words and tie-breaks to the first visible item (room before inventory).
- **Placeholders**: any `{lowercase_word}` anywhere in the JSON is treated as a variant placeholder
  by validate, including in prose.
- **Condition strings vs flag names**: `if`/`done` take conditions (`flag:x|has:y`); `requires`,
  `unless`, `set`, `clear`, `renew`, `locked_flag`, `light_flag`, exit `flag` take bare flag names.
  validate doesn't check flag spelling; the solver catches the resulting unreachable endings.
- `clock.beats` is required by the schema and read without a default by the engine; validate
  doesn't check it. `clock.deadline_ending` must be a plain id for validate even though `finish()`
  accepts option lists.
- `theme.beat` is accepted by the schema but unused by the page; only `theme.accent` is applied.
- `desc_flags`: the **last** set flag in object order wins.
- Containers can't be closed; `open` is one-shot and spills the contents into the room.
- `tick()` only runs if the game isn't over, so an ending on the deadline turn wins over the
  deadline ending.
- Narration is only attempted for story beats (progress or a room change) and never for `talk`
  turns (those may be voiced instead). Looks and failures always show plain engine text.
- `restore()` accepts arbitrary flag strings (bounded in count and length), since flags aren't
  declared anywhere.
- Undo stack and scored endings live in the session; a resumed session (after restart) starts with
  an empty undo stack.
- A party proposal of "undo" undoes the host's last move (it goes through `run_turn`).
- `/author` HTML is always served; only `/api/author/*` is gated by `AUTHOR_TOOLS`. Keep it off on
  the box (the solver is CPU-heavy).
- `.dockerignore`'s `*.md` only matches top-level files, so `llm/` lands in locally built images.
- The platform's `portal/apps.json` description for darklight says "five endings"; the shipped
  darklight world has seven (stale text in the other repo).
- Windows: port 8000 is reserved; the standalone compose publishes 8765. H: is exFAT (git needs
  `safe.directory`). This repo has no `.gitattributes` (git `autocrlf` is on), unlike the platform repo.
- `cli.py draft` is the only thing that calls an outside service, and it must never run in the image.
