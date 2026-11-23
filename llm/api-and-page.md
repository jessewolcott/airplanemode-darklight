# HTTP API, storage, party mode and the pages

All routes are in `app.py`. Paths below are as the app sees them; on the box Caddy serves them
under `/darklight/` and strips the prefix. There is no authentication: a session id (uuid4 hex) is
the capability to play that game; party player ids are random tokens.

## Endpoints

| Method + path | Body / query | Returns | Notes |
|---|---|---|---|
| `GET /api/worlds` | | `{default, worlds: [{id, title, blurb}]}` | app.py:440 |
| `POST /api/new` | `{world?, mode?, name?}` (body optional) | `{session, intro, text, facts, narrated, status, save, ...world_info}` | random variant layout per game (`secrets.randbits(32)`); unknown mode → normal; unknown world → 404. Intro room facts are narrated |
| `POST /api/turn` | `{session, input}` | turn result (below) | 404 "Unknown session. Start a new game." if expired/restarted |
| `POST /api/undo` | `{session}` | like a turn, `parsed_by: "undo"`, `undone: true` | same as typing `undo` |
| `POST /api/resume` | `{save, name?}` | `{session, text, facts, narrated: false, status, save, ...world_info}` | rebuilds a game from a browser snapshot via `Game.restore`; 400 if world unknown or save invalid |
| `POST /api/name` | `{session, name}` | `{name}` | name cleaned: whitespace collapsed, max 20 chars, printable only |
| `POST /api/savecode` | `{session}` | `{code, persistent}` | 6-char code from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` |
| `GET /api/savecode/{code}` | `?name=` | same as resume | case-insensitive, dashes ignored; 404 if unknown or older than 30 days |
| `GET /api/leaderboard` | `?world=` (default world) | `{world, fastest: {relaxed, normal, hard}, collectors, persistent}` | |
| `POST /api/party` | `{session}` | `{code, round_seconds}` | 4-letter code |
| `POST /api/party/{code}/join` | `{name?}` | `{player, name, title, theme}` | 409 if full (40) |
| `GET /api/party/{code}` | `?player=&since=` | `{round, ends_in, round_seconds, players, you, proposals, log, title, status}` | settles a due round first |
| `POST /api/party/{code}/propose` | `{player, text}` | party view | 403 unknown player, 409 empty / 12 proposals already |
| `POST /api/party/{code}/vote` | `{player, proposal}` | party view | 409 if already played |
| `GET /api/health` | | `{llm: "none"|"ready"|"offline", model}` | `GET {LLM_URL}/v1/models` with 3 s timeout |
| `GET /api/stats` | `?samples=1` | counters (+ samples) | see parser-and-model.md |
| `GET /api/author/{world}` | | `{world, title, map, summary, problems, reports}` | only with `AUTHOR_TOOLS=1` (else 404); solver runs in a thread; cached per world for the process life |
| `GET /` | | `static/index.html` | |
| `GET /party` | | `static/party.html` | |
| `GET /author` | | `static/author.html` | served regardless of `AUTHOR_TOOLS`; its API call 404s when off |
| `/static/*` | | static files | mounted but the pages are self-contained |

**Turn result** (`run_turn`, app.py:556): `text` (narrated or plain), `facts` (all engine facts),
`command`, `model_raw` (model JSON or null), `parsed_by` (`rules` / `model` / `fallback` / `undo` /
`none`), `narrated`, `director` (hint or null), `beats` (verbatim clock/timer facts),
`ending_text`, `status` (`Game.status()`), `save` (`Game.snapshot()`).

**world_info** (app.py:422): `world`, `title`, `map` (`{nodes: [{id, name, x, y}], edges: [{a, b,
vertical, lock}]}` from `build_map`, computed once per world at import), `ui`, `theme`, `modes`
(`[{id, label}]`), `endings` (`[{id, title, tone, hint}]`), `endings_total`, `persistent`.

## Sessions and undo

- `Session` (app.py:67): `game`, `undo` (list of snapshots), `last` (monotonic time), `name`,
  `scored` (endings already recorded).
- `open_session` prunes first, then adds; `get_session` refreshes `last` and LRU position.
- `prune` drops sessions idle > `SESSION_TTL` or beyond `MAX_SESSIONS` (oldest first), then
  parties whose session is gone.
- Undo stack: snapshot before each turn that changed state (equality ignores `since_progress`),
  max `UNDO_DEPTH = 30`. Undo restores via `Game.restore(s.game.base, snapshot)` and returns
  "You take that back." + plain room facts. Undo works after an ending.
- No per-session lock for ordinary turns; the engine part of a turn is synchronous, so concurrent
  requests to one session interleave only around the awaited model calls.

## Scores and the leaderboard

- `record_score` (app.py:545) runs after every turn: only if the session has a `name`, the game
  has an `ending`, and that ending isn't in `s.scored` yet. Entry: `{name, world, ending, tone,
  turns, mode, at}`. Scores come from the server's own game, never from the browser.
- Undoing and re-reaching the same ending in the same session doesn't score twice.
- `Store.leaderboard(world, ("best",))` (store.py:87): `fastest[mode]` = each name's fewest-turn
  `best`-tone ending, top 10, ties by time; `collectors` = names by number of distinct endings
  (any mode), top 10.

## Storage (`store.py`)

- `Store(DATA_DIR)`: `usable_dir` creates the folder and probes a write; if it fails or
  `DATA_DIR` is empty, everything lives in memory and `persistent` is false (the page then says
  scores/codes reset on restart).
- Two files: `saves.json` (`{code: {at, save}}`) and `scores.json` (list). Loaded at start (a
  damaged file is ignored and rebuilt on the next write). Each write dumps the whole file to
  `*.tmp` and `os.replace`s it (atomic). An `OSError` on write switches that file to memory-only.
- Caps: saves expire after 30 days (purged on each new save), at most 2000 (oldest evicted);
  scores keep the last 3000.
- On the box `DATA_DIR` is unset on purpose (no volume, protects eMMC/SD). The dev compose sets
  `DATA_DIR=/data` on a named volume. Never default it to a container path.
- Tests force `DATA_DIR=""` so they never write a store.

## Party mode (`party.py` + app.py:603-673)

One game on a shared screen (the host's normal session); everyone on the Wi-Fi opens `/party`
on a phone, joins with the 4-letter code, suggests commands and votes.

- `Party(session_id, round_seconds)` holds `players` (id → name, max 40), `proposals` (max 12 per
  round; each `{id, text, key, by, voters}`), `round_ends`, `round_no`, `log` (last 60 played
  moves), an `asyncio.Lock`.
- `propose`: text collapsed to 120 chars; key = lowercased, stripped of ` .!?`; the player's vote
  moves to it; an identical key just adds a vote. The **first proposal starts the round clock**.
- `vote`: one vote per player per round (moves the vote).
- `winner`: most voters; ties go to the earliest proposal.
- **Lazy settling**: no background task. Every party GET/propose/vote calls `settle(p)`; if the
  round is due, under the lock it closes the round and plays the winner through the normal
  `run_turn` on the host session (so the model, guards, scoring and undo all apply; a proposal
  of "undo" really undoes). The result is appended to the log.
- The host page polls `api/party/{code}?since=n` every 1.5 s, shows proposals, and renders new log
  entries as turns in its own transcript. Phones poll every 1.5 s too, so rounds settle promptly.
- Party codes use `ABCDEFGHJKMNPQRSTUVWXYZ` (no I, L, O). Parties live only in memory.

## The game page (`static/index.html`)

Single file, inline CSS and JS, no framework, no external assets. All fetches are relative
(`api/new`, `api/turn`, ...). Layout: header (title, narrator status dot, "engine facts" toggle,
world picker, map toggle on narrow screens, undo, new game, then the AirplaneMode home button
`a.am-home` at the right end; below 600 px the controls wrap to their own row under the title and
the button), log, status bar, command form, and an
aside (map SVG, clock bar, endings tracker, party panel, leaderboard, options). Side panel shows
at >= 940 px; on phones the map button swaps it in place of the log.

**Start-up** (`start()`): restore prefs from localStorage, `GET api/worlds`, choose the saved
`adventure` or the default, hide the picker if only one world, then `resume(true)` or `newGame()`.
`checkLLM()` polls `api/health` every 5 s while the model is offline.

**localStorage keys** (every access wrapped in try/catch; storage can be missing):

| Key | Content |
|---|---|
| `<world>-save` | `{save: snapshot, log: transcript innerHTML}` written after every turn (`keep`, last 80 entries) |
| `<world>-endings` | `{ending_id: title}` found endings (the tracker is client-side only) |
| `adventure` | last world id |
| `mode` | difficulty |
| `name` | player name (party page reads it too) |
| `speak`, `ambience` | toggles |

**Resume**: if a non-ended save exists, `POST api/resume` and restore the transcript with "You pick
up where you left off." If a turn returns 404 (server restarted or session expired), the page
resumes from the save and retries the request once.

**Rendering a turn** (`fill`): story paragraph (plain styling if not narrated), beats as italic
side-ruled lines, director hint as "A thought: ...", an ending card with tone colour and "N of M
found", and a hidden debug block (model raw, parsed command, facts, narrated or plain) shown when
"engine facts" is ticked. Then `render(status)` (status bar, clock bar from `turns/deadline`,
clock label from `ui.clock_labels`, map, endings tracker, input disabled when over).

**Map**: SVG grid from `world_info.map`; visited rooms highlighted, current room outlined; edges
with an unset `lock` flag drawn dashed red.

**Audio, all offline**: read-aloud uses `speechSynthesis`; ambience is generated brown noise
through a low-pass filter with an LFO swell (`ui.ambience`: `wind` 500 Hz, otherwise 900 Hz),
plus random low thunder rumbles for `storm`.

**Theme**: `theme.accent` overrides CSS `--lamp`; the title's last word gets the accent colour.

**Home button** (platform app contract item 10): `<a class="am-home" href="../">` with the house
icon, 44 px, on every page (index, party, author; all are served at the app root, so `../` is
the AirplaneMode menu). The game needs no confirm (it's saved in localStorage and resumes), but
while the host runs party mode the click asks first, since the party can't be picked up again.
`tests/test_app.py::test_every_page_has_the_home_button` checks all three pages.

Other controls: name (sent with new/resume and via `api/name`), difficulty (starts a new game),
save code get/load, start/end party mode (shows code and the `party` URL built with
`new URL("party", location.href)`), leaderboard tabs per mode. Up/down arrows recall command
history.

## The party page (`static/party.html`)

Header (title, home button at the right), then a join card (code from `?code=` if given, name from localStorage), then the latest played move,
round countdown ("first idea starts the clock"), a suggest box, and the proposals as vote buttons
(yours outlined). The player id is kept in `sessionStorage` as `party-<CODE>` so a reload rejoins.

## The author page (`static/author.html`)

Header (title, world picker, home button at the right) → `GET api/author/{world}` → map, problems list, and per layout: positions searched,
deadline, a table of each ending's fastest turns and route, moves that close off endings, traps.
Same content as `python cli.py solve`.
