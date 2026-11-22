# The Dark Light: a tiny-LM text adventure

An [AirplaneMode](https://github.com/jessewolcott/airplanemode) app. It's a text adventure that
plays in the browser. A small language model (served by llama.cpp) directs it, and a
deterministic engine keeps it honest. It ships with two adventures: **The Dark Light** (the
lighthouse) and **The Frozen Pass** (a short mountain story). Players choose one from the menu at the top of the page.

## Running it

**On the box:** the [airplanemode](https://github.com/jessewolcott/airplanemode) platform repo
runs the released image at `/darklight/` and connects it to the shared model. On the GL.iNet
profile there is no model, and the game plays in plain-text mode.

**On its own, for development:** this repo's `compose.yaml` runs the game with its own
0.5B model:

```
docker compose up -d --build        # first run downloads the model (~400 MB)
open http://localhost:8765
```

Use a different port with `PORT=9000`, or a different model with any GGUF on Hugging Face, for
example `MODEL=Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`.

**Tests:**

```
python tests/test_endings.py        # every ending in every world is still reachable (no deps)
python tests/test_engine.py         # engine rules and fixed bugs (no deps)
pip install -r requirements.txt
python tests/test_app.py            # server, undo/resume, sessions, model guards (fake model)
```

**Releasing:** push a tag like `v0.2.0`. GitHub Actions runs the tests and publishes
`ghcr.io/jessewolcott/airplanemode-darklight:0.2.0` for arm64 and amd64. Then bump the pinned
tag in the platform repo's `compose.yaml`.

## Fixed map, semi-solid story

The map never changes. It's drawn in the side panel from the world's `map`, and the model
can't add or move places. The story has a fixed spine with branches:

- **Clock.** Actions that change the world take one turn. Anything that changes nothing is
  free: looking, examining, repeating a conversation, or a failed attempt. A ship gets closer at
  fixed beats (turns 8, 18, 27 and 34) and hits the rocks at turn 38.
- **Choices with a cost.** The missing keeper is in the dark cellar. Carrying them out costs
  4 turns, or 1 if you talked to them first and rigged the block and tackle they mention. The
  driftwood bonfire is a quick signal, but not a perfect one. The rowboat lets you leave.
- **Light with limits.** The lantern burns for 20 turns, warns when it's low, then goes out.
  Top it up from the oil can. A lit lantern set down on the floor still lights the room.
- **Seven written endings** in The Dark Light, chosen by flags and timing:

| Ending | How you get it |
|---|---|
| The Keeper's Light | light the lamp after rescuing the keeper |
| A Light Alone | light the lamp, keeper left behind |
| Two Keepers of the Fire | light the driftwood after rescuing the keeper |
| Fire on the Shore | light the driftwood alone |
| Two in the Boat | row away with the keeper |
| The Long Row | row away alone |
| The Teeth | the clock runs out |

The ending text, clock beats and timer messages are shown exactly as written, never rewritten
by the model. The side panel counts the endings you've found and shows a hint for each
missing one. Your browser remembers them, per adventure.

## Saving, resuming and undo

- After every turn, the browser saves the game state and transcript. Reloading the page
  picks up where you left off. If the server restarted, the page rebuilds the game from that
  save without you noticing. Saves come from the browser, so the server checks every id in one
  before using it.
- `undo` (typed, or the button) takes back the last turn that changed something, up to 30
  turns back, including an ending.
- The server forgets games idle for `SESSION_TTL` seconds (default 2 hours) and holds at most
  `MAX_SESSIONS` (default 500), so memory stays bounded on the box.

## How the work is split

A model this small can't be trusted with game state. It forgets, invents items and
contradicts itself. So it only gets narrow jobs, and every job has a deterministic guard:

| Job | Who | Guard |
|---|---|---|
| Rules, state, win condition | `engine.py` (plain Python) | the model never sees or edits state |
| Standard commands (`take key`, `n`, `unlock door`, `take matches and lantern`, `take all`) | rule parser | handles about 70% of turns instantly |
| Free-text intent ("head over to the lighthouse") | **model** classifies the verb | JSON-schema grammar limits output to valid verbs and exits; code does the noun and place matching with fuzzy strings; unambiguous verb words override the model |
| Narration of story beats (moving, finding, succeeding) | **model** rewrites engine facts | output rejected and replaced by plain facts if it's first person, repetitive, too long, drops a listed item or the direction taken, claims an action that didn't happen, or copies the few-shot examples |
| Dialogue | **model** voices the authored line | rejected and shown as written if it drops or adds any named thing or place, uses he/she, isn't dialogue, or is far longer or shorter |
| Nudges when the player stalls | director (engine hint plus a turn counter) | hints come from the world file |

Failures and lookups (`examine`, "you can't go that way") skip the model and show the
engine's own text. The status bar always shows ground truth.

Tick **engine facts** in the UI to see, for each turn, what the model said, what the parser
decided, and the facts the narration was built from. `GET /api/stats` reports, since start-up,
how often the model's narration and dialogue were used or thrown out, and which guard
rejected them. Use it to compare models (for example 0.5B against 1.5B on the R6S).

## Writing an adventure

Each file in `worlds/` is one adventure; its file name is its id. The server refuses to start
if a world has problems (an exit to a room that doesn't exist, a typo'd item id, an ending or
talk list with no fallback, a bad condition), and lists every one. The building blocks:

- `rooms`, `map`, `items` (`portable`, `person`, `container`, `locked_flag`, `light_flag`,
  `desc_flags`), `start`, `intro`, `setting` (used in the narrator's prompt), `ui` (clock
  title and labels, map label).
- `uses`: item on target. Keys: `set`, `clear`, `renew`, `requires`, `unless`, `refuse`, `where`,
  `consume`, `ending`.
- `interactions`: `help` or `board` a target. Keys: `cost`, `move`, `move_player`, `ending`.
- `talk` lines on a person: `if`, `say`, plus optional `set` and `reveal` (put an item in a room).
- `timers`: a flag that runs out after N turns, with warning and "out" messages.
- `clock` (beats and a deadline), `endings` (with a `hint` for the tracker), `hints`.

Conditions are `has:item`, `flag:name` or `seen:room`, joined with `|` for "or". Anything
that can vary (`ending`, `fact`, `cost`) can be a list of `{if, ...}` options, checked in order,
with a last option that has no `if`. Add a route for every new ending to `tests/test_endings.py`.

## Files

- `worlds/*.json`: the adventures.
- `engine.py`: deterministic engine, rule parser, save/restore and world validation.
- `app.py`: FastAPI server, sessions, model prompts and guards. Set `LLM_URL` empty to run with no model.
- `static/index.html`: the UI. It uses relative URLs, so it works under any path prefix.
- `app.yaml`: this app's AirplaneMode manifest (port, path, memory, needs, env).
- `tests/`: endings, engine and server tests.

## Known limits

- Narration still adds harmless flavour that can contradict details (for example, a
  "beam of light" before the lamp is lit). The guards catch missing items, directions and
  false actions, not every invented detail. The prompts and guards were tuned on 0.5B.
- Dialogue voicing is strict on purpose, so a small model's version is often rejected
  and the authored line is shown. `/api/stats` shows how often.
- Saves and found endings live in the browser, so they don't follow a player to another device.
