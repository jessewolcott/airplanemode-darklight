# Solver and author CLI

## Solver (`solver.py`)

Plays every possible game through the **real engine** (`Game.clone()` + `do()`), per variant
layout, and reports routes and problems. Used by `cli.py check`/`solve`/`draft`, `tests/test_kit.py`,
and `/api/author`.

### Search (`solve_layout`, solver.py:67)

1. Note the real (mode-scaled) deadline, then search a copy of the world with the deadline set to
   10^6 and no beats, so every position is explored regardless of time.
2. **Actions** (`actions`, solver.py:29): every exit; for visible items: take (portable, not held),
   open (closed container), talk (person); drop each held item (only with `allow_drop`, off by
   default; `cli.py solve --drops` turns it on); every interaction whose target is visible; every
   use rule whose item is held and target visible. Each action carries a text in rule-parser
   grammar using the item's first alias ("take matches", "use oil on lamp", "help keeper"). No
   look/examine (free, change nothing) and no light/unlock/fill shortcuts (covered by `use`).
3. **State key** (`state_key`, solver.py:59): room, inventory set, flags, each room's sorted items,
   open items, sorted timers, and `seen` only if a `seen:` condition appears outside `hints`
   (`uses_seen`). Turn count is **not** in the key, so the space is finite.
4. Dijkstra by turns (heap of `(turns, tiebreak, key)`): moves that changed nothing (`turns`
   unchanged and not over) are skipped; moves that end the game record the ending and the fastest
   (turns, state, text); others add an edge and relax distances. Capped at `max_states`
   (default 300 000); hitting it sets `truncated`.
5. **Reachability**: bitmask of endings reachable in one move from each state, propagated backwards
   over edges to a fixed point.
6. Results per ending: fastest `turns`, `route` (parent chain + final move), `in_time`
   (`turns <= real deadline`). The deadline ending, if not otherwise reached, is reported with
   route `["(let the clock run out)"]`.
7. **Dead ends**: states with no reachable ending (only computed if not truncated).
   **Traps**: states that can still reach something but no good-tone ending
   (`GOOD_TONES = best good bittersweet mixed`).
8. **Closing moves**: for each move text, for each ending reachable before it, count how often
   playing it loses that ending; a move "closes" an ending if it does so in >= 50% of the places it
   is played (so coincidences like the lantern burning out aren't blamed on a random move).

Note the search ignores the clock while exploring, so a timer-based failure is modelled (timers
are in the state) but time pressure from the deadline is judged only via each ending's fastest route.

### Outputs

- `solve(world, mode, **kw)` → list of reports, one per layout (`layouts()` = cartesian product of
  variant options, or `[{}]`).
- Report: `choices, mode, states, truncated, deadline, endings, unreachable, dead_ends (first 3
  with routes), dead_end_count, traps (first 3), trap_count, closes {move: [endings]}`.
- `problems(reports)` (solver.py:186) → failures: truncated search, unreachable endings, endings
  slower than the deadline, any dead ends. **Traps and closing moves are informational only.**
- `summary(world, reports)` → the human report printed by `cli.py solve`.

Solver fastest routes, normal mode (run 2026-11-22): darklight (19k positions per layout,
deadline 38) `flight` 1, `bonfire` 4, `bonfire_keeper`/`flight_keeper` 11, `lone_light` 16,
`keeper_light` 19; frozenpass (94 positions, deadline 30) `downhill` 1, `warm_alone` 10,
`warm_together` 18; fogbell (1056 positions per layout, deadline 26) `one_hand` 4, `two_home` 10,
`all_home` 13.

**Multi-object commands are cheaper than the solver thinks.** `do()` adds one turn per command,
then runs the handler for each object, so "take all" or "take matches and lantern" costs a single
turn. The solver only issues single takes, so its fastest times are upper bounds (e.g.
`tests/test_endings.py` reaches frozenpass `warm_alone` in 9 turns with `take all`, solver says 10).
Routes use full direction words ("north"), which `parse_rule` accepts.

## CLI (`cli.py`)

```
python cli.py new  <id> [--title "..."]          copy templates/starter.json to worlds/<id>.json
python cli.py check [worlds or ids ...] [--all-modes]
python cli.py solve <id|path> [--mode M] [--drops] [--max-states N] [--json]
python cli.py play  <id|path> [--mode M] [--layout SEED] [--debug]
python cli.py map   <id|path>
python cli.py draft "<premise>" --id <id> [--rounds 4] [--model claude-opus-5-5] [--force]
```

- `find(ref)` (cli.py:26): a path to an existing `.json`, else `worlds/<ref>.json`.
- **check** (cli.py:47): all `worlds/*.json` if none given; for each: `load_world` (validate) then
  solver problems for normal (or every mode with `--all-modes`). Prints `ok`/`FAIL` per file and
  exits 1 on any problem. CI runs `check --all-modes`. Note it only checks `worlds/`, not
  `WORLDS_DIR`/`games/` unless you pass paths.
- **solve** (cli.py:67): summary + problems, or raw JSON.
- **play** (cli.py:110): terminal game with `parse_rule(...) or fallback_parse(...)` (no model, no
  server); `--layout` seeds variants (default: authored layout); `--debug` prints each command;
  `quit`/`q!` exits. Prints the ending title and turns.
- **map** (cli.py:80): ASCII grid from `map`, `-`/`|` for east/south links, `x` for locked ones,
  and a list of up/down links.
- **draft** (cli.py:157): **dev machine only** (needs `pip install -r requirements-dev.txt` and an
  Anthropic credential). Sends `DRAFT_RULES` + `docs/writing-a-game.md` + the full
  `worlds/darklight.json` as a cached system prompt, asks for one world in a ```json block, then
  runs `validate` and the solver and feeds the problems back, up to `--rounds` times. Clean →
  writes `worlds/<id>.json` and prints the solver summary; otherwise saves the last attempt to
  `templates/<id>.draft.json` and exits non-zero. Uses the beta streaming API with adaptive
  thinking, effort high and server-side fallback. Per CLAUDE.md, it hasn't been run against the
  live API from this repo yet; check its first output carefully. Never part of the image.
