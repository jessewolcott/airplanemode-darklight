# The Dark Light — a tiny-LM text adventure

A text adventure that runs in Docker and plays in the browser. A ~0.5B-parameter model
(Qwen2.5-0.5B-Instruct, Q4_K_M, ~400 MB, CPU only) directs it, served by llama.cpp.

```
docker compose up -d --build        # first run downloads the model (~1-2 min)
open http://localhost:8765
```

Use a different port with `PORT=9000 docker compose up -d`. Use a different model with any
GGUF on Hugging Face, for example `MODEL=HuggingFaceTB/SmolLM2-360M-Instruct-GGUF:Q8_0`.

## Fixed map, semi-solid story

The map never changes. It's drawn in the side panel from `world.json` → `map`, and the model
can't add or move places. The story has a fixed spine with branches:

- **Clock.** Most actions take one turn. A ship gets closer at fixed beats (turns 8, 18, 27
  and 34) and hits the rocks at turn 38. Looking around and asking for help don't use up turns.
- **Choices with a cost.** The missing keeper is in the dark cellar. Carrying them out costs
  4 turns. The driftwood bonfire is a quick signal, but not a perfect one. The rowboat lets you leave.
- **Five written endings,** chosen by flags and timing:

| Ending | How you get it |
|---|---|
| The Keeper's Light | light the lamp after rescuing the keeper |
| A Light Alone | light the lamp, keeper left behind |
| Fire on the Shore | light the driftwood on the shore |
| The Long Row | row away in the boat |
| The Teeth | the clock runs out |

The ending text and clock beats are shown exactly as written, never rewritten by the model.
Your browser remembers which endings you've found. To add a branch, add an `interactions` or
`uses` rule with an `ending` (or a list of `{if, ending}`) plus an entry in `endings`.

## How the work is split

A model this small can't be trusted with game state. It forgets, invents items and
contradicts itself. So it only gets narrow jobs, and every job has a deterministic guard:

| Job | Who | Guard |
|---|---|---|
| Rules, state, win condition | `engine.py` (plain Python) | the model never sees or edits state |
| Standard commands (`take key`, `n`, `unlock door`) | rule parser | handles about 70% of turns instantly |
| Free-text intent ("head over to the lighthouse") | **model** classifies the verb | JSON-schema grammar limits output to valid verbs and exits; code does the noun and place matching with fuzzy strings; unambiguous verb words override the model |
| Narration of story beats (moving, finding, succeeding) | **model** rewrites engine facts | output rejected and replaced by plain facts if it's first person, repetitive, too long, drops a listed item or the direction taken, or copies the few-shot examples |
| Nudges when the player stalls | director (engine hint plus a turn counter) | hints come from `world.json` |

Failures and lookups (`examine`, "you can't go that way") skip the model and show the
engine's own text. The status bar always shows ground truth.

Tick **engine facts** in the UI to see, for each turn, what the model said, what the parser
decided, and the facts the narration was built from.

## Files

- `game/world.json`: rooms, items, puzzles, hints. Edit this to write a new adventure.
- `game/engine.py`: deterministic engine plus the rule parser.
- `game/app.py`: FastAPI server, model prompts and guards.
- `game/static/index.html`: the UI.

## Known limits

- Narration still adds harmless flavour that can contradict details (for example, a
  "beam of light" before the lamp is lit). The guards catch missing items and directions,
  not every invented detail. A ~1.5B model (`Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`)
  narrates noticeably better at about 3x the latency.
- One object per command ("take the matches and lantern" only takes the matches).
- Sessions live in memory and reset when the container restarts.
