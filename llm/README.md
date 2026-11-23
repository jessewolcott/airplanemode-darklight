# The Dark Light: how it's built

Checked-in reference for LLMs and engineers who open this repo with no prior context
(`.dockerignore` lists `llm`, so none of this reaches the image). Written 2026-11-22 against the code at release `v0.3.0`+ (main).
When the code and this folder disagree, the code wins; fix these notes.

## Overview

The Dark Light is an AirplaneMode app: a browser text-adventure **engine** that runs offline on a
travel router (NanoPi R6S; not installed on the GL.iNet lite profile, which has no model, platform
decision 0017). Games are JSON "world" files in
`worlds/` (three ship: `darklight`, `frozenpass`, `fogbell`), and everything else is shared and
game-agnostic. A **deterministic engine** (`engine.py`) owns all game state and rules. A **small
language model** (Gemma 4 E2B on the box since platform decision 0018, Qwen2.5 0.5B in standalone
dev; served by llama.cpp, OpenAI-style API at `LLM_URL`) gets three
narrow jobs: classify the *verb* of free text the rule parser couldn't read, rewrite engine facts
as prose ("narrate"), and rephrase authored dialogue ("voice"). Every model output passes a
deterministic validator, and anything that fails falls back to plain engine text. With no model, a
keyword + fuzzy-match parser (`parsing.fallback_parse`) handles free text, so the game is fully
playable. A FastAPI server (`app.py`) holds sessions in memory and serves a single static page
(`static/index.html`), a phone page for party mode, and an author report. An exhaustive solver
(`solver.py`) proves every ending is reachable in time; `cli.py` wraps it with author tools.

## Files in this folder

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | The big picture: components, the division of labour engine vs model, request flow of one turn, process model, environment variables, platform contract |
| [world-format.md](world-format.md) | The world JSON key by key, conditions and option lists, variants, modes, and exactly what `validate()` and the JSON schema each check |
| [engine.md](engine.md) | `Game` state, the turn loop (`do`/`tick`), free turns, every verb handler, use/interaction rules, timers, clock, endings, hints, snapshot/restore, the rule parser `parse_rule` |
| [parser-and-model.md](parser-and-model.md) | The 3-layer parser pipeline, fuzzy noun/place matching, `build_command`, `fallback_parse` step by step, and every model call (prompt shape, JSON schema, guards, fallbacks, stats) |
| [api-and-page.md](api-and-page.md) | Every HTTP endpoint with request/response shapes, sessions and undo, storage (`DATA_DIR`, leaderboard, save codes), party mode, author report, and how the page works (localStorage, resume, map, audio) |
| [solver-and-cli.md](solver-and-cli.md) | The solver algorithm (state key, dead ends, traps, closing moves), `cli.py` new/check/solve/play/map/draft |
| [testing-and-evals.md](testing-and-evals.md) | The four test files, parse evals (tuned vs held-out, current numbers, floors), Dockerfile, compose, CI, release |
| [extending.md](extending.md) | Recipes: add a world, a world verb, a world key, a parser feature, a model feature; plus a gotchas list |

## Quick commands (from the repo root)

```powershell
python cli.py check --all-modes        # validate + solve every world in every difficulty (CI)
python cli.py solve darklight          # fastest routes, traps, closing moves
python cli.py play darklight --debug   # terminal play, shows each parsed command
python tests\test_endings.py; python tests\test_engine.py; python tests\test_kit.py
python tests\test_app.py               # needs requirements.txt (fastapi, httpx)
python evals\parse_eval.py --show      # parse accuracy; add --llm http://localhost:8080 for the model
docker compose up -d --build           # standalone dev + own 0.5B model -> http://localhost:8765
```

Verified on 2026-11-22: all of the above pass (endings 15/15 routes, engine 15/15, kit 17/17,
app 20/20). Parse eval: tuned set 112/112 with the fallback, held-out set 18/33 (54.5%).

## Rules that matter most (from the repo's CLAUDE.md)

- The page uses **relative URLs** (`api/turn`, never `/api/turn`); the platform serves it under `/darklight/`.
- **Works without a model**: empty `LLM_URL` means no model calls.
- **Never let the model decide game state.** Any new model feature needs a deterministic guard.
- Game content lives in world files, never in code. Engine and parser stay generic.
- Never tune the parser against `evals/parse_heldout.json`.
- Nothing in the image may call outside services (`cli.py draft` is dev-only).
- `DATA_DIR` empty = memory only; never default it to a container path.
- Ask Jesse before pushing a `v*` release tag.
