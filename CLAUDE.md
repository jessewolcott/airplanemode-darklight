# The Dark Light (AirplaneMode app): notes for Claude

A browser text-adventure **engine**. Games are JSON files in `worlds/` (darklight, frozenpass,
fogbell). A **deterministic engine** (`engine.py`) owns all game state. A **small language
model** (llama.cpp, OpenAI-style API at `LLM_URL`) only:
1. classifies the *verb* of free-text commands (code does noun and place matching),
2. narrates story beats from engine facts,
3. voices authored dialogue lines.

Every model output has a validator, and anything that fails it falls back to plain engine text.
With no model, `parsing.fallback_parse` (keywords plus fuzzy matching) handles free text.

Part of AirplaneMode: the platform repo is `../airplanemode`. Read its `CLAUDE.md` for the
environment notes (H:\Github on exFAT, Docker Desktop, port 8000 reserved, commit attribution)
and `docs/app-contract.md` for the rules this app must follow (offline, works without the model,
256 MB, small writes only to one volume).

## Commands

```powershell
docker compose up -d --build                 # standalone dev with its own 0.5B model -> http://localhost:8765 (/author, /party)
python cli.py check --all-modes              # validate + solve every world (CI runs this)
python cli.py solve <id>                     # fastest routes, traps, closing moves
python cli.py play <id> --debug              # terminal play, no server
python tests\test_endings.py; python tests\test_engine.py; python tests\test_kit.py
python tests\test_app.py                     # needs requirements.txt (fastapi, httpx)
python evals\parse_eval.py --show            # parse accuracy; --llm http://localhost:8080 to score the model
```

## Rules

- The page must use **relative URLs** (`api/turn`, not `/api/turn`): it runs behind a path prefix.
- **Works without a model**: `LLM_URL` empty means no model calls. Free text goes to the fallback parser.
- Never let the model decide game state. New model features need a deterministic guard.
- Game-specific content belongs in world files, never in code. The engine and parser stay generic.
- Adding or changing a world: run `python cli.py check <id>`, add a route per ending to
  `tests/test_endings.py`, and keep `schemas/world.schema.json` and `docs/writing-a-game.md` in
  step with any new world key (plus `validate()` in `engine.py`).
- Parser changes: run `evals/parse_eval.py`. **Never tune against `evals/parse_heldout.json`**:
  it's the honest score. Add new cases to `parse_cases.json`, and add a fresh held-out batch now
  and then. `test_kit.py` enforces floors.
- `cli.py draft` calls the Claude API (dev machine only, `requirements-dev.txt`). Nothing in the
  image may call outside services.
- Storage: `DATA_DIR` empty = memory only. Never default it to a path in the container filesystem.

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-darklight:X.Y.Z`
(arm64 + amd64, public). Then bump the pinned tag in `../airplanemode/compose.yaml`.
**Ask Jesse before pushing a release tag.** Released: `v0.1.0`, `v0.2.0`.

## Open follow-ups

- Platform: give darklight a small named volume and `DATA_DIR=/data` so the leaderboard and save
  codes survive restarts. Optionally mount a games folder at `WORLDS_DIR`.
- Run `evals/parse_eval.py --llm` against 0.5B and 1.5B to get real model numbers.
- Grow the parse cases from real `/api/stats?samples=1` logs on the box.
