# Tests, evals, build, CI and release

## Tests

Plain Python scripts (no pytest). Each defines `test_*` functions and a `main()` that runs them all,
prints `ok`/`FAIL` per test and exits non-zero on failure. Run from the repo root (they insert the
root on `sys.path`). Status on 2026-11-22: all pass.

| File | Needs | Covers |
|---|---|---|
| `tests/test_endings.py` | stdlib | `ROUTES`: a scripted command list per ending per world, replayed with `parse_rule` only (every step must parse). Asserts every world has routes and every ending has one; checks the ending reached. Plus: the bonfire only lights on the shore |
| `tests/test_engine.py` | stdlib | 15 engine rules and fixed bugs: dropped lit lantern still lights the room; missing-tool messages; looking/failing is free; talk with news costs a turn but repeating doesn't; multi-object take; "block and tackle" is one object; keeper's tip shortens the rescue (cost 4 vs 1); tackle hidden until mentioned; lantern burns out and can be refilled; refill renews the timer; `use oil` prefers the room's target; snapshot round trip; restore rejects tampered saves; validate catches mistakes; missing fallbacks don't crash |
| `tests/test_kit.py` | stdlib (+ `jsonschema` optional) | 17: variants move items and fill text without mutating the loaded world; random layouts cover every option; variant survives save/restore; modes (darklight deadlines relaxed 57 / normal 38 / hard 32, relaxed has no timers, hard has no hints); world verbs parse and run (fogbell `shout`); interaction `requires`; validate catches verb/variant/placeholder mistakes; solver finds every ending in every world with no problems; **solver routes replay through `parse_rule` with the same turns**; solver catches a constructed dead end and a too-slow ending; **parse-eval floors** (tuned >= 95%, held-out >= 50%); `cli.py check` passes and the template is valid and solvable; `check` fails on a broken world; `play` reaches an ending from stdin; schema accepts every world + template (skipped without jsonschema); `load_world` reports every problem |
| `tests/test_app.py` | `requirements.txt` (fastapi, httpx) | 20, with `LLM_URL=""`, `DATA_DIR=""`, `AUTHOR_TOOLS=1` set before importing `app`: narration guard reasons; voice guard; `build_command` multi-take and use-pair; fuzzy `mentions`; session TTL and cap; worlds + new game; turn and undo (free turns aren't undo steps); undo an ending; resume after a "restart" (sessions cleared) and bad saves rejected; stats + health; **fake model** (monkeypatched `server.chat` returning queued replies: rejected narration falls back, accepted voice used, rejected voice falls back, stats count reasons); modes and random layouts via the API; no director hints in hard mode; save codes; leaderboard scores from the server (no double counting, no anonymous scores); store persists across instances; a party round plays the winning idea on the host session; author report + stats samples + page routes |

Notes:
- `test_kit` temporarily writes `templates/_broken_test.json` and `templates/_bad_load.json` and
  deletes them in `finally`.
- The fake-model pattern in `test_fake_model_voice_and_fallbacks` is the way to test new model
  features without a model: replace `server.chat` with an async function that pops canned replies,
  and set `server.LLM_URL` to something non-empty.
- After adding a world, `test_endings` fails until you add routes for every ending, and
  `test_app::test_worlds_and_new_game` asserts the exact set of world ids (update it).

## Parse evals (`evals/`)

`python evals/parse_eval.py [--show] [--llm URL]`.

**Case format** (`parse_cases.json`, `parse_heldout.json`): a list of groups
```json
{ "world": "darklight", "at": ["n", "take matches"], "cases": [["grab the lantern", "take lantern"], ...] }
```
`at` is setup steps (rule-parser commands) to reach a position; each case is `[player text,
intended command]`, where the intended command is rule-parser grammar or `"unknown"`.

**Scoring** (`score`, parse_eval.py:62): for each case, parse the text on a clone of the position,
then compare the **effect** (the snapshot after `do()` minus `since_progress`, plus the facts) with
the effect of the intended command. So "unlock door" and "use key on door" both count as right.

**Parsers scored**: `rules only` (`parse_rule` or unknown), `rules + fallback (no model)`, and with
`--llm` the server's `app.understand` (rules → model → fallback), importing `app` after setting
`LLM_URL`.

**Sets**:
- `parse_cases.json`: 15 groups, 112 cases (darklight + frozenpass). The set the parser was tuned against.
- `parse_heldout.json`: 9 groups, 33 cases (darklight + frozenpass). **Never tune against it**;
  it's the honest number. Add new failing phrasings to `parse_cases.json`, and add a fresh held-out
  batch now and then. No fogbell cases yet.

**Numbers on 2026-11-22**:

| Set | rules only | rules + fallback |
|---|---|---|
| tuned (112) | 57.1% | 100.0% |
| held-out (33) | 27.3% | 54.5% |

Model numbers (`--llm`) have not been recorded yet (open follow-up: run against the standalone 0.5B and the box's Gemma 4 E2B).
Floors enforced by `test_kit.test_parse_eval_floor`: tuned >= 0.95, held-out >= 0.5. Raise them as
the parser improves.

## Build

- `Dockerfile`: `python:3.12-slim`, `pip install -r requirements.txt` (fastapi 0.115.6,
  uvicorn 0.34.0, httpx 0.28.1), `COPY . .`, `EXPOSE 8000`,
  `CMD uvicorn app:app --host 0.0.0.0 --port 8000` (single process; required, state is in memory).
- `.dockerignore` excludes `.git .github .vscode tests evals templates schemas __pycache__ *.md
  compose.yaml requirements-dev.txt docs/screenshots docs/icon.svg`. Docker matches these from the
  context root, so `*.md` only excludes **top-level** markdown; `docs/writing-a-game.md` and this
  `llm/` folder get copied into *locally built* images (harmless, small). CI builds from a git
  checkout where `llm/` doesn't exist. `.playwright-mcp/` and `games/` are also copied locally.
- `requirements-dev.txt`: `anthropic` (draft only), `jsonschema` (schema test).

## Standalone dev (`compose.yaml`)

Project name `darklight-dev`:
- `llm`: `ghcr.io/ggml-org/llama.cpp:server-b12602`, `-hf ${MODEL:-Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M}`,
  port 8080, context 4096, 2 parallel slots, models cached in the `models` volume (first run
  downloads ~400 MB).
- `darklight`: built from `.`, `LLM_URL=http://llm:8080`, `AUTHOR_TOOLS=1`, `DATA_DIR=/data`
  (volume `data`), `WORLDS_DIR=/games` (bind `./games:ro`; `games/*.json` is git-ignored),
  published on `${PORT:-8765}:8000`. Port 8000 is reserved by Windows on the dev machine, hence 8765.
- URLs: `http://localhost:8765/`, `/party`, `/author`.

On the box the platform repo (`../airplanemode/compose.yaml`) runs the pinned released image with
`LLM_URL: ${LLM_URL-http://llm:8080}` and `mem_limit: ${DARKLIGHT_MEM:-256m}`, behind Caddy at
`/darklight/` (`../airplanemode/Caddyfile`), with a portal entry in `../airplanemode/portal/apps.json`.

## CI (`.github/workflows/image.yml`)

Triggers: push to `main`, push of `v*` tags, pull requests. One job:
1. checkout, Python 3.12.
2. `pip install -r requirements.txt jsonschema`, then `cli.py check --all-modes`,
   `test_endings`, `test_engine`, `test_kit`, `test_app`, `evals/parse_eval.py` (the eval itself
   doesn't fail on a low score; the floors live in `test_kit`).
3. QEMU + buildx, log in to ghcr.io (not on PRs), metadata tags `type=ref,event=branch` (→ `:main`)
   and `type=semver,pattern={{version}}` (→ `:0.3.0` for `v0.3.0`), build `linux/arm64,linux/amd64`,
   push unless PR.

## Release

1. **Ask Jesse first** (pushing a `v*` tag publishes a public image).
2. Push `vX.Y.Z`; CI tests and publishes `ghcr.io/jessewolcott/airplanemode-darklight:X.Y.Z`.
3. Bump the pinned tag in `../airplanemode/compose.yaml`. Released so far: v0.1.0, v0.2.0, v0.3.0, v0.3.1 (home button).

Nothing has been tested on real router hardware yet (screenshots are from the laptop stack).
