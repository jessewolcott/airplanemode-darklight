# The Dark Light (AirplaneMode app): notes for Claude

A browser text adventure. A **deterministic engine** (`engine.py` + `world.json`) owns all
game state, and a **small language model** (llama.cpp, OpenAI-style API at `LLM_URL`) only:
1. classifies the *verb* of free-text commands (code does noun and place matching by fuzzy string),
2. narrates story beats from engine facts, with validators that fall back to plain text,
3. (via the engine) offers hints when the player stalls.

Fixed map, five written endings (clock, keeper rescue, bonfire, rowboat, wreck). Ending text
and clock beats are shown verbatim, never sent through the model.

Part of AirplaneMode: the platform repo is `../airplanemode`. Read its `CLAUDE.md` for the
environment notes (H:\Github on exFAT, Docker Desktop, port 8000 reserved, commit attribution)
and `docs/app-contract.md` for the rules this app must follow.

## Commands

```powershell
docker compose up -d --build         # standalone dev with its own 0.5B model -> http://localhost:8765
python tests/test_endings.py         # every ending still reachable (CI runs this before building)
```

Inside the platform stack it's served at `/darklight/` and uses the shared model (1.5B on the R6S).

## Rules

- The page must use **relative URLs** (`api/turn`, not `/api/turn`): it runs behind a path prefix.
- **Works without a model**: `LLM_URL` empty means no model calls, plain engine text only.
- Never let the model decide game state. New model features need a deterministic guard, and
  output that fails validation falls back to engine facts.
- Narration validators (in `app.py`) reject:
  - first person and gendered pronouns (the keeper is "they"),
  - dropped items or a dropped direction,
  - copied few-shot words,
  - claimed actions the engine didn't report.
- Adding an ending: add an `ending` on a `uses`/`interactions` rule, an entry in `endings`,
  and a route in `tests/test_endings.py`. The test asserts every ending has a route.

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-darklight:X.Y.Z`
(arm64 + amd64, public). Then bump the pinned tag in `../airplanemode/compose.yaml`.
**Ask Jesse before pushing a release tag.** Released: `v0.1.0`.

## Open follow-up

On the GL.iNet profile there's no model, so free-text commands ("walk to the cottage") aren't
understood: only the rule parser's short forms work. Next step: a no-model fallback in
`app.py` that uses the same fuzzy noun and place matching plus keyword verbs
(`CUE_VERBS`/`VERB_SYN`) when the model is unavailable.
