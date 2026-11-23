# Architecture

## Components

```
            browser (static/index.html, party.html, author.html)
                 |  relative fetch: api/...
                 v
   app.py  FastAPI + uvicorn (one process, all state in memory)
     |-- sessions: OrderedDict[sid -> Session(game, undo stack, name, scored)]
     |-- parties:  dict[code -> party.Party]
     |-- store:    store.Store (leaderboard + save codes; JSON files in DATA_DIR or memory)
     |-- understand(): engine.parse_rule -> llm_parse (model) -> parsing.fallback_parse
     |-- narrate()/voice(): model rewrite + guards, else plain engine text
     |-- /api/author: solver.solve in a thread (AUTHOR_TOOLS=1 only)
     v
   engine.py  Game (deterministic state + rules), parse_rule, validate, load_worlds
   parsing.py mentions / place_direction / build_command / cue_verb / fallback_parse
   solver.py  exhaustive search over Game clones
   worlds/*.json  the games (data only)
     |
     +--> llama.cpp server at LLM_URL (/v1/chat/completions, /v1/models), optional
```

| File | Lines | Role |
|---|---|---|
| `engine.py` | ~975 | `Game` class, verbs, clock/timers, endings, variants/modes, snapshot/restore, `parse_rule`, `validate`, `load_world(s)` |
| `parsing.py` | ~194 | noun matching (`mentions`), place matching (`place_direction`), `build_command`, `cue_verb`, `fallback_parse` |
| `app.py` | ~740 | server: env config, sessions, model calls and guards, API, party endpoints, stats, author report, static pages |
| `solver.py` | ~229 | Dijkstra-style exhaustive search; reports routes, unreachable endings, dead ends, traps, closing moves |
| `cli.py` | ~263 | `new`, `check`, `solve`, `play`, `map`, `draft` |
| `party.py` | ~112 | `Party` rounds (propose, vote, lazy settle), `new_code` |
| `store.py` | ~102 | `Store`: save codes + scores as two small JSON files, atomic writes |
| `static/` | | `index.html` (game), `party.html` (phones), `author.html` (solver report); all inline CSS/JS, no external assets |
| `schemas/world.schema.json` | | JSON Schema (draft-07) for world files; VS Code uses it via `.vscode/settings.json` |
| `templates/starter.json` | | world template for `cli.py new` |
| `docs/writing-a-game.md` | | the human authoring guide (also fed to Claude by `cli.py draft`) |
| `evals/` | | parse test sets + scorer |
| `tests/` | | four plain-Python test scripts (no pytest) |

## The division of labour (the core design idea)

A 0.5B model forgets, invents items and contradicts itself, so it is never trusted with state. It
only gets jobs whose output can be checked by code:

| Job | Who does it | Guard / fallback | Where |
|---|---|---|---|
| Rules, state, win condition | engine | model never sees or edits state | `engine.Game` |
| Standard commands (`take key`, `n`, `take all`) | rule parser | exact; returns `None` when unsure | `engine.parse_rule` (engine.py:692) |
| Free-text intent | **model** classifies the verb only | JSON-schema grammar restricts verb and direction to valid enums; code finds nouns/places; an unambiguous cue word overrides the model; `unknown` falls through to the fallback parser | `app.llm_parse` (app.py:146) |
| Free text, no model | keyword parser | fuzzy nouns, place names, keyword verbs; harmless when unsure (examines rather than uses) | `parsing.fallback_parse` (parsing.py:139) |
| Narration of story beats | **model** rewrites facts | `narration_problem` (8 checks), one cooler retry, else `plain(facts)` | app.py:213, 248, 389 |
| Dialogue | **model** voices the authored line | `voice_problem` (8 checks), one retry, else the authored line verbatim | app.py:288, 312, 327 |
| Nudges when stuck | director: engine hint + turn counter | hints authored in the world; off in hard mode | `run_turn` (app.py:575) |

Clock beats, timer warnings, and ending text are always shown verbatim (never sent to the model).

## One turn, end to end (`POST api/turn`)

1. `turn()` → `get_session(sid)` (404 if unknown; refreshes LRU position) → `run_turn(s, text)` (app.py:556).
2. Input trimmed to 200 chars. `undo` / `take back` / `go back in time` → `undo_turn`. If the game
   is over, a fixed "This story is over" reply.
3. `understand(text, game)` (app.py:524):
   - `parse_rule` → if a command, `parsed_by="rules"`.
   - else if `LLM_URL`: `llm_parse`; a non-`unknown` verb → `"model"`; an `unknown` verb tries
     `fallback_parse` → `"fallback"` if that knows; else returns the model's unknown. Any exception
     (HTTP error, timeout, bad JSON) is swallowed.
   - else / on exception: `fallback_parse` → `"fallback"`.
4. `STATS["parse"][parsed_by] += 1`; unknown commands are logged into `SAMPLES["unparsed"]`.
5. `before = game.snapshot()`; `facts = game.do(cmd)` (the engine turn; see engine.md).
6. If the state changed (snapshot equality ignoring `since_progress`), push `before` onto the undo
   stack (max 30).
7. Director: if `since_progress >= STUCK_TURNS` and hints are on and the game isn't over, attach
   `current_hint()` and reset the counter.
8. `record_score(s)`: if the session has a name and reached an ending not yet scored in this
   session, append to the leaderboard.
9. Split facts: `authored = beats_now ∪ {ending_text}`; the rest is the "action".
10. If someone spoke (`game.said`), try `voice()`; a voiced line replaces the authored line.
11. Narrate with the model only if it's a *story beat*: `game._progress` or a `Location:` fact,
    and the verb isn't `talk`. Otherwise `plain(action)`.
12. Respond with `text`, `facts`, `command`, `model_raw`, `parsed_by`, `narrated`, `director`,
    `beats`, `ending_text`, `status`, `save`.

The browser stores `save` (a full snapshot) in localStorage after every turn, so a server restart
loses nothing: on a 404 the page calls `api/resume` and retries once.

## Process model and state

- Single uvicorn process (`CMD ["uvicorn", "app:app", ...]`, no `--workers`). Sessions, parties,
  stats and the author cache are module-level dicts, so **more than one worker would break
  sessions**. Keep it single-process.
- `WORLDS` are loaded and validated at import (app.py:47). A broken world file makes the server
  **refuse to start** (deliberate).
- Sessions expire after `SESSION_TTL` seconds idle and are capped at `MAX_SESSIONS` (LRU, `prune()`
  app.py:79). Parties whose host session is gone are deleted in the same sweep.
- The model client is one shared `httpx.AsyncClient(timeout=45)`.

## Environment variables (app.py:36-53, documented in `app.yaml`)

| Var | Default in code | Meaning |
|---|---|---|
| `LLM_URL` | `http://llm:8080` | llama.cpp base URL. **Empty string = no model calls at all.** Note the code default is *not* empty; the platform sets `${LLM_URL-http://llm:8080}`; the app isn't installed on the GL.iNet profile (decision 0017) |
| `NARRATE` | `1` | `0` disables narration and voicing (parsing still uses the model) |
| `STUCK_TURNS` | `6` | non-progress turns before the director shows a hint |
| `SESSION_TTL` | `7200` | idle seconds before a session is dropped |
| `MAX_SESSIONS` | `500` | session cap (LRU eviction) |
| `WORLDS_DIR` | unset | extra folder of world files; a same-named file overrides the bundled one |
| `GAMES` | all | comma list of world ids to offer |
| `WORLD` | `darklight` | default world (falls back to the first loaded if missing) |
| `DATA_DIR` | `""` | folder for `saves.json`/`scores.json`; empty = memory only |
| `AUTHOR_TOOLS` | `0` | `1` enables `/api/author/{world}` |
| `PARTY_SECONDS` | `20` | party voting round length |

## AirplaneMode platform contract (`../airplanemode/docs/app-contract.md`)

- One image `ghcr.io/jessewolcott/airplanemode-darklight`, arm64 + amd64, semver tags; the
  platform pins an exact tag (`../airplanemode/compose.yaml`: `:0.3.0` at time of writing).
- One port (8000) inside the container; Caddy serves it at `/darklight/` and **strips the prefix**
  (`handle_path /darklight/*`). Hence relative URLs everywhere in the pages.
- Fully offline: no CDNs/fonts/analytics; favicon is a data URI, audio is synthesized with Web
  Audio, read-aloud uses the browser's `speechSynthesis`.
- Works without the model (`needs: llm: optional`).
- Memory limit 256 MB (`app.yaml memory: 256m`, platform `mem_limit: ${DARKLIGHT_MEM:-256m}`).
- Small writes, one optional named volume (`volume: optional`, mount `/data` + `DATA_DIR=/data`).
  On the box it is deliberately unset, so leaderboard and save codes reset on restart.
- Model usage rules: short prompts, tight `max_tokens`, `cache_prompt: true`, `response_format`
  JSON schema for structured output, validate everything. All followed in `app.chat`.
- `app.yaml` is the manifest (id, image, port, path, memory, needs, env docs).

The app id, repo and image stay named `darklight` even though it hosts several games (owner's call).
