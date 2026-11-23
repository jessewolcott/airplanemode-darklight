# Parser pipeline and model calls

## Pipeline (app.py:524 `understand`)

```
text ──> engine.parse_rule ──(None)──> LLM_URL set? ──yes──> app.llm_parse
             │ cmd                          │no                 │ verb != unknown → "model"
             v                              v                   │ verb == unknown → fallback_parse;
          "rules"                    parsing.fallback_parse     │    known → "fallback", else model's "unknown"
                                         → "fallback"           │ exception → fallback_parse → "fallback"
```

`cli.py play` and the evals' "no model" scorer use `parse_rule(text) or fallback_parse(text)`.

The key split: **code decides which things and places were named** (fuzzy string matching is
exact and cheap); **the model only decides what the player wants to do** (the verb), a small
classification task a 0.5B model handles. Both the model path and the fallback feed the same
`build_command(verb, nouns, direction, game)`.

## Shared helpers (`parsing.py`)

- `words_of(text)` (parsing.py:46): `[a-z]+` words of length >= 3, minus `FILLER`
  (`the and you your with into onto from that this over back there toward towards`).
- `fuzzy_in(word, said)` (parsing.py:50): exact or `difflib.SequenceMatcher` ratio >= 0.8 (so
  "latern" matches "lantern", "matchs" matches "matches").
- `mentions(text, game)` (parsing.py:54): items whose alias words *all* fuzzy-match words the
  player said, ordered by first position in the text. Searches visible items first, then **all
  other items in the world** (so it can return things that aren't here; the engine then says "You
  don't see that here", and `fallback_parse` prefers visible ones). Because `words_of` drops short
  words, alias parts under 3 letters (or filler) effectively never match; multi-word aliases like
  "tin of matches" match via their other single-word aliases instead.
- `place_direction(text, game)` (parsing.py:69): for each exit of the current room, does any word of
  the destination room's `name` or `aliases` fuzzy-match? Returns the direction only if exactly one
  exit matches ("head over to the lighthouse" → east).
- `cue_verb(text, game)` (parsing.py:128): the first word anywhere in the text that is a world verb or
  synonym, or in `CUE_VERBS` (`open read alight ignite kindle unlock refill talk speak ask rescue
  save row paddle flee escape ride sled toboggan`). These words mean one thing wherever they appear.

### `build_command(verb, nouns, direction, game)` (parsing.py:81)

- `go`: `{"go", direction}` or `unknown` if no direction.
- `help`/`talk`/`board`/world verbs (optional object): first noun or None.
- verbs that need no object (`look`, `inventory`, `hint`): bare.
- verbs that need an object (`take drop examine open use light unlock fill`) with no nouns → `unknown`.
- `take`/`drop` with several nouns: keep portable-not-held (take) / held (drop); >1 → `objects`
  list, else the single kept one (or the first noun).
- `light`: first noun that isn't the light tool. `unlock`/`fill`: `{verb, object: None, target}`
  with the first non-tool noun (the engine fills in the tool).
- `use`, one noun not held: if a held item has a use rule on it → `use tool on noun` ("cut steps
  in the ice"); else if portable → `take` ("pocket the key"); else if container or locked_flag →
  `open`. Several nouns: the (a, b) pair the world has a use rule for, in either order; else held
  noun as object, next noun as target.
- anything else: `{verb, object: first noun}`.

## Fallback parser: `fallback_parse(text, game)` (parsing.py:139)

No model. Errs toward harmless commands. Steps:

1. Words `[a-z]+`; none → unknown.
2. `nouns = mentions(...)`, narrowed to the visible ones if any are visible.
3. `direction = place_direction(...)`; `dir_word` = first direction word in the text (ignoring
   one-letter abbreviations, which the rule parser handles).
4. Verb: first matching `PHRASES` pair (`set fire`/`on fire` → light, `top up` → fill,
   `cut steps`/`hack steps` → use), else `cue_verb`, else the first word in `FALLBACK_VERBS`
   (a broad keyword table: `snag pocket fetch → take`, `study observe → examine`,
   `wander stroll descend → go`, `carry haul lift drag → help`, `tie attach hook prop → use`, ...),
   or a world/built-in verb word of >= 3 letters.
5. `where ...` with no/go verb → `look` ("where can I go?").
6. Use `dir_word` as the direction only if no place matched and (verb is go or there are no
   nouns) (so "top up the lamp" isn't "up"). `climb` with no direction → up if there's an up exit.
7. People rules: use/take with a person who has a `help` interaction → help ("use the pulley to
   lift the keeper"); help with no noun → the only visible person; help/take with a person noun →
   help; help with only non-person nouns → take ("carry the logs"); for help, people nouns first.
8. examine with no nouns → look.
9. No verb: question word + nouns → examine ("what's in the drawer?"); a direction → go; nouns →
   talk if exactly one noun and it's a person, else examine the first noun; nothing → unknown.
10. go without direction → `{"verb": "go"}` (engine asks "Go where?").
11. Else `build_command`.

Known limit: about 55% on the held-out eval set (see testing-and-evals.md).

## The model client: `chat()` (app.py:109)

```python
POST {LLM_URL}/v1/chat/completions
{"messages": [...], "max_tokens": N, "temperature": T, "repeat_penalty": 1.15, "cache_prompt": True,
 # only when a schema is given:
 "response_format": {"type": "json_schema", "json_schema": {"name": "command", "schema": schema}}}
```

- Raises `RuntimeError` immediately if `LLM_URL` is empty. No `model` field (llama.cpp serves one model).
- One shared `httpx.AsyncClient(timeout=45)`. Returns `choices[0].message.content.strip()`.
- All callers catch exceptions and fall back. A down model costs the request timeout (connection
  refused is instant; a hung server can take up to 45 s per call).
- `cache_prompt: true` + fixed system prompt and few-shot prefix lets llama.cpp reuse the KV cache.

## Model call 1: PARSE (`llm_parse`, app.py:146)

Only reached when `parse_rule` returned None.

- Code first computes `nouns = mentions(text)`, `exits = game.exit_names()` (direction → room name),
  `direction = place_direction(text)`.
- **Schema** (grammar-enforced by llama.cpp):
  ```json
  {"type": "object",
   "properties": {"verb": {"enum": MODEL_VERBS + world verbs},
                  "direction": {"enum": ["none", <current room's exit directions>]}},
   "required": ["verb", "direction"]}
  ```
  `MODEL_VERBS` = unknown go take drop examine open use light unlock fill talk board look inventory hint help.
- **Messages**: system `PARSE_SYSTEM` (verb classification instructions: go needs the exit
  direction; examine for looking/reading; help for helping/rescuing/carrying a person; talk;
  board for boats/sleds/escaping; unknown if not a game action), plus, for worlds with own verbs,
  " This game also has these verbs: ring (toll, sound, clang); ...". Then 9 few-shot user/assistant
  pairs (`PARSE_EXAMPLES`, invented generic world: rope, crate, mill, sign, torch, miner, guard,
  canoe, "sing a song about cheese" → unknown). Final user message:
  ```
  Player: <text>
  Mentioned: <item names from mentions, or (nothing)>
  Exits: north -> Keeper's Cottage, east -> Base of the Lighthouse
  ```
- `max_tokens=30`, `temperature=0.0`. Output parsed with `json.loads`.
- **Guards after the model**:
  - `raw["mentioned"] = nouns` (for the debug view).
  - Cue override: if `cue_verb` finds a word and (there are nouns or the cue is `board`) and the
    model's verb is neither `go` nor the cue → replace the verb with the cue (`raw["verb_cue"]`).
  - For `go`: code's `place_direction` wins; else the model's direction unless `none`.
  - `build_command` turns verb + code's nouns into the command. The model never names objects.
- If the result is `unknown`, `understand` still tries `fallback_parse`.
- The page shows `model_raw` and the parsed command when "engine facts" is ticked.

## Model call 2: NARRATE (`narrate` app.py:389, `llm_narrate` app.py:248)

When: `NARRATE` and `LLM_URL`, and `use_model` is true. In `run_turn`, `use_model` = story beat
(progress happened or the facts include `Location:`) and verb != talk. The new-game intro room
description is also narrated. Resume, undo and plain failures use `plain()`.

- Input facts are the *action* facts only: `run_turn` removes `beats_now` and `ending_text`, which
  are shown verbatim.
- **Messages**: system `narrate_system(world)`: "You are the narrator of a text adventure set in
  {setting}. Rewrite the FACTS as 1 to 3 short sentences. Always say 'you', never 'I'. Present
  tense. Keep every fact, including every item and every exit. Never add objects, people, or
  events that are not in the FACTS. Do not give advice." Two few-shot pairs (`NARRATE_EXAMPLES`:
  an Old Mill room, "You take the rope."). User: each fact as `- fact` on its own line.
- `max_tokens=110`; tries `temperature 0.3`, then a retry at `0.0`.
- `tidy()`: strip quotes, drop a leading `Narration:`/`Narrator:`, cut after the last `.`/`!`/`?`
  if that's past char 20 (removes a truncated tail).
- **`narration_problem(out, facts)`** (app.py:213), first failing check wins:

| Reason | Rule |
|---|---|
| `empty` | no text |
| `first_person` | regex `\b(I|I'm|I've|I'd|me|my|myself|we|our|he|his|him|she|her)\b` (case-insensitive; also bans gendered strangers) |
| `too_long` | longer than `2.2 * len(facts joined) + 120` chars |
| `repetitive` | duplicate sentence or more than 5 sentences |
| `copied_example` | uses a word from `EXAMPLE_ONLY` (mill, waterwheel, flour, sack, rope, coil, shoulder, dust) that isn't in the facts |
| `claimed_action` | uses a verb from a `CLAIMS` group (take/grab/pocket..., light/lit/strike..., unlock..., pour/fill...) when no word of that group is in the facts |
| `dropped_direction` | a `You go <d>.` fact whose `<d>` isn't in the output |
| `dropped_item` | for `Items here: ...`, `Inside: ...` and `The X is here.` facts, the last word of each name must appear |

- Each rejection increments `STATS["narrate_rejected"][reason]` and logs to `SAMPLES["narrate"]`.
  Both attempts failing → `plain(facts)`.
- `plain(facts)` (app.py:380): drops `Location:` and `Exits:` (the status bar shows them), rewrites
  `Items here: X.` as `You see: X.`, joins with spaces.

## Model call 3: VOICE (`voice` app.py:327, `llm_voice` app.py:312)

When: a `talk` produced a line (`game.said`) that is in the action facts, `NARRATE` and `LLM_URL`.

- **Messages**: system `voice_system(world, speaker_name)`: rewrite the LINE as the speaker would
  say it, dialogue in double quotes with a few words saying who speaks, keep every detail and
  instruction, add no new places/objects/information, refer to the speaker as "the X" or "they".
  One few-shot pair (a ferryman and a washed-out bridge). User: `Player: <text>\nLINE: <line>`.
- `max_tokens=120`; temperature 0.5 then 0.2.
- **`voice_problem(out, line, world)`** (app.py:288):

| Reason | Rule |
|---|---|
| `not_dialogue` | empty or no `"` |
| `gendered` | he/his/him/himself/she/her/hers/herself |
| `too_long` | > `1.8 * len(line) + 60` |
| `too_short` | < `0.35 * len(line)` |
| `repetitive` | duplicate sentence |
| `copied_example` | bridge / ferryman / washed not in the line |
| `dropped_detail` | a world "detail word" in the line is missing from the output |
| `invented_detail` | a detail word in the output that isn't in the line |

  Detail words (`detail_words`, app.py:281) = words (>= 3 letters, minus `FILLER`) of every
  non-person item alias plus every room name in the world. So the voiced line must keep exactly
  the same named things and places.
- Accepted → replaces the authored line in the action; the turn is then not narrated (verb is
  talk) and `narrated` is reported true. Rejected twice → the authored line verbatim.

## Stats and samples (`/api/stats`)

`STATS` (app.py:60): `parse` counts by `parsed_by`; `narrate` and `voice` counts of
`requested/accepted/fell_back/error` plus `accept_rate`; `*_rejected` counts by guard reason.
`SAMPLES` (deques): last 40 rejected narrations and voicings with their inputs, last 60 unparsed
player texts (world, room, text, by). Everything resets on restart. The intended loop: pull
`?samples=1` from the box, turn real unparsed text into eval cases, fix, re-measure.

## The "director" (nudges)

Not a model call. `since_progress` counts turns without `progress()` (help/hint excluded; free
turns count). At `STUCK_TURNS` (default 6), if hints are on and the game isn't over, the reply's
`director` field carries `current_hint()` and the counter resets. The page shows it as
"A thought: ...". Hard mode never nudges.
