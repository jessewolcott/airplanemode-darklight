# World file format

A world is one JSON file in `worlds/` (or in `WORLDS_DIR`). **Its id is the file name stem**
(`worlds/fogbell.json` → `fogbell`); `load_world` (engine.py:953) sets `world["id"]` and the file
itself should not contain `id` (the schema has `additionalProperties: false` and no `id` property).

Three layers of checking, each catching different things:

1. `schemas/world.schema.json` (draft-07): shapes, types, enums, key names, id/alias patterns.
   VS Code applies it to `worlds/*.json` and `templates/*.json` (`.vscode/settings.json`);
   `tests/test_kit.py::test_schema_accepts_every_world` enforces it (needs `jsonschema`, installed in CI).
2. `engine.validate(world)` (engine.py:807): cross-references (ids that must exist), fallbacks,
   conditions, verbs, variants, modes, theme colours, placeholders. Runs at server start, in
   `cli.py check` and `cli.py draft`.
3. `solver` (solver.py): reachability, deadline fit, dead ends (see solver-and-cli.md).

The best worked example is `worlds/darklight.json` (uses nearly every feature); `fogbell.json` shows
own verbs and `interactions.requires`; `templates/starter.json` is the minimal valid world.

## Top level

| Key | Req (schema) | Req (validate) | Meaning / who uses it |
|---|---|---|---|
| `title` | yes | yes | shown in picker, header, party page |
| `blurb` | | | one line for the picker (`/api/worlds`, `<option title>`) |
| `setting` | | | a few words the narrate/voice system prompts use ("a lighthouse on a rocky island during a storm"); defaults to "a strange place" |
| `intro` | yes | yes | shown italic at game start (not narrated) |
| `start` | yes | yes | starting room id (validate checks it exists) |
| `theme` | | | `{accent, beat}` colours `#rrggbb`. validate checks *every* value is a colour. The page only uses `accent` (sets CSS `--lamp`); `beat` is allowed by the schema but unused |
| `ui` | | | `clock_title` (side-panel heading), `clock_labels` (`[[turns_left_above, label], ...]`, highest first, last `[0, ...]`), `over_label`, `map_label` (SVG aria-label), `ambience` (`storm` / `wind` / `none`) |
| `verb_defaults` | yes | yes | `{light, unlock, fill}` → item ids. The tool used when the player doesn't name one ("light lamp" = use matches on lamp). validate requires all three to be items |
| `tool_missing` | | | `{light, unlock, fill}` messages when the default tool isn't visible |
| `verbs` | | | the world's own verbs: `{"ring": ["toll", "sound"]}` |
| `map` | yes | yes | `room id -> [x, y]` grid position for the map SVG and `cli.py map`. Every room needs one and every entry must be a room |
| `rooms` | yes | yes | see below |
| `items` | yes | yes | see below (people, containers, doors are items too) |
| `uses` | yes | yes | item-on-target rules (most puzzles) |
| `interactions` | | | `help`/`board`/own-verb rules on a target |
| `timers` | | | flags that run out |
| `variants` | | | randomized item placement per game |
| `modes` | | | per-world overrides of relaxed/normal/hard |
| `clock` | | | `{deadline, deadline_ending, beats}` |
| `endings` | yes | yes | `{id: {title, tone, text, hint}}` |
| `hints` | | | ordered `{done, hint}` list |

## Conditions and option lists

**Condition strings**: `has:<item>`, `flag:<name>`, `seen:<room>`, joined with `|` meaning OR.
There is no AND and no NOT. `Game.check` (engine.py:176). validate checks the kind, that `has:`
names an item and `seen:` a room, and that flag names are non-empty. Schema pattern:
`^(has|flag|seen):[a-z0-9_]+(\|...)*$`.

`seen:` means the room was entered *while lit* (`do()` adds the room to `seen` only if not dark),
while `visited` means entered at all.

**Option lists**: `fact`, `ending` and `cost` may be a plain value or a list of
`{"if": cond, "<key>": value}` checked in order; the first match wins; **the last option must have
no `if`** (the fallback). `Game.pick` (engine.py:187). validate's `choice()` helper enforces the
fallback rule and checks each value (`text`: non-empty string; `ending`: known ending; `cost`:
int >= 0). At runtime a list with no match returns `None`; `finish(None)` is a no-op (tested in
`test_missing_fallbacks_dont_crash`).

## Rooms

```json
"base": { "name": "Base of the Lighthouse", "aliases": ["tower"], "desc": "...",
  "exits": { "west": "shore",
             "up": { "to": "stair", "flag": "door_unlocked", "blocked": "The iron door is locked..." } },
  "items": ["iron_door"] }
```

| Key | Notes |
|---|---|
| `name` | required (both). Shown in status bar and map; used by `place_direction` to match "go to the cottage" |
| `aliases` | other words for the place ("beach" for "Rocky Shore"); only used by `place_direction` |
| `desc` | required. Part of `room_facts` |
| `exits` | `{direction: room_id}` or `{direction: {to, flag, blocked}}` (a locked exit passable once `flag` is set). Directions: `north south east west up down` only. validate: direction valid, destination exists, flagged exit has `blocked`. Schema requires `exits` (engine indexes `r["exits"]` directly) |
| `items` | item ids initially here. Schema requires the key (engine indexes it directly). Items listed here can be people, fixtures or portable things |
| `dark` / `dark_desc` | a dark room shows only "pitch black", `dark_desc`, and the exits; items are invisible until a lit light source is carried or on the floor |

Exits are one-way; author both directions. `cli.py map` assumes exits agree with the grid.

## Items

```json
"lantern": { "name": "lantern", "aliases": ["lantern", "hand lantern", "lamp"], "portable": true,
  "light_flag": "lantern_lit", "desc": "...",
  "desc_flags": { "lantern_dry": "...", "lantern_lit": "..." } }
```

| Key | Notes |
|---|---|
| `name` | required. Used in all engine messages ("You take the lantern."). The narration guard checks the **last word** of each listed item name survives, so make it distinctive |
| `aliases` | required, >= 1, lowercase words (schema `^[a-z]+( [a-z]+)*$`). `Game.resolve` picks the visible item whose alias has the most words all present in the input. On a tie (same word count) the first visible item wins, and `visible()` lists room items before inventory. Keep aliases unambiguous between items visible together. Darklight deliberately shares `lamp` between the lantern and the great lamp: in the lamp room "fill lamp" resolves to the great lamp (room first), and `v_use` with no target prefers targets not in your inventory |
| `desc` | required. Can contain `{variant_id}` placeholders |
| `portable` | can be taken |
| `person` | talks, can be helped; listed as "The X is here."; "take X" on a person becomes `help` |
| `container` + `contains`, `desc_open`, `open_fact` | `open` moves contents into the room. Containers are one-shot (no close) |
| `locked_flag` | a door-like item: `open` says "locked" until the flag is set, then "already open". (The passage itself is a flagged exit.) |
| `light_flag` | while that flag is set the item lights dark rooms, whether carried or on the floor |
| `desc_flags` | `{flag: desc}`; iterates in order and the **last** set flag wins |
| `talk` | list of `{if, say, set, reveal}`; first line whose `if` matches is said; last line must have no `if`. `set` sets a flag the first time (and counts as progress, costing a turn); `reveal: {item: room}` places an item (e.g. the tackle appears on the shelves). Repeat talk with nothing new is free |

## Uses (item on target)

```json
{ "item": "matches", "target": "great_lamp", "requires": "lamp_fueled",
  "missing": "You strike a match, but the wick is dry...", "set": "lamp_lit", "fact": "...",
  "ending": [ { "if": "flag:keeper_rescued", "ending": "keeper_light" }, { "ending": "lone_light" } ] }
```

Evaluated by `Game.v_use` (engine.py:572) in this order: item must be held; target inferred if
omitted; target visible; a rule for (item, target) exists; `where` (room id, else `wrong_place`);
`unless` flag set → `already`; `requires` flag missing → `missing`; first matching `refuse`
(`[{if, say}]`) → its `say`; then apply: pick `fact`, `clear` (discard flag + its timer), `set`
(starts its timer if any), `renew` (restart a running timer), `consume` (remove the item from
inventory), progress, then `ending`.

`requires`/`unless`/`set`/`clear`/`renew` are **plain flag names**, not condition strings.
validate checks item/target are items, `fact` present and well-formed, `where` is a room, `ending`
valid, and each `refuse` has `if`+`say` with a valid condition. It does *not* check flag names
(typos in flags are only caught by the solver as unreachable endings).

## Interactions

```json
{ "verb": "help", "target": "keeper", "unless": "keeper_rescued", "already": "...", "set": "keeper_rescued",
  "cost": [ { "if": "has:tackle", "cost": 1 }, { "cost": 4 } ],
  "move": { "keeper": "cottage" }, "move_player": "cottage", "fact": [ ... ] }
```

`verb` must be `help`, `board`, or one of the world's own `verbs`. Keys: `target` (item), `fact`,
`requires`+`missing`, `unless`+`already`, `set`, `cost` (extra turns on top of the normal 1;
option list allowed; cost and fact are chosen *before* flags change), `move` (`{item: room}`, works
on people), `move_player` (room id; marks visited and appends room facts), `ending`.
`Game.interact` (engine.py:511). With no object, the target is inferred only if exactly one rule
for that verb has a visible target. validate checks verb, target, fact, cost, move, move_player,
ending.

## Own verbs

`"verbs": {"ring": ["toll", "sound", "clang"], "shout": ["call", "yell"], "wade": ["venture"]}`
(fogbell). Each verb: one lowercase word, not a built-in (`BUILTIN_VERBS`, engine.py:42); synonyms
single lowercase words; **at least one interaction must use it**. They are added to the rule
parser's verb words (`Game.verb_words`), to `cue_verb` (any position in the sentence), to the
model's verb enum and system prompt, and dispatched to `interact(verb, object)`.

## Time: clock and timers

```json
"clock": { "deadline": 38, "deadline_ending": "wreck",
           "beats": [ { "turn": 8, "fact": "Through the rain, you glimpse..." } ] },
"timers": { "lantern_lit": { "turns": 20, "warn": 4, "then": "lantern_dry",
            "warn_fact": "...", "out_fact": "..." } }
```

- `clock.deadline`: when `turns >= deadline` the game ends with `deadline_ending` (an ending id or
  option list). `beats` are shown verbatim when their turn is crossed. Schema requires `beats`
  (engine reads `clock["beats"]` without a default; validate doesn't check it).
- `timers`: keyed by a flag. Setting that flag starts the countdown; when it hits 0 the flag is
  removed, `then` is set (optional) and `out_fact` shown. `warn_fact` shows when the remaining
  count crosses `warn`. validate: `turns` int and `out_fact` present.

## Endings

`{title, tone, text, hint}`; tone ∈ `best good bittersweet mixed dark worst`. Tone sets the
colour on the page, the leaderboard (only `best` counts for "fastest"), and the solver
(`best/good/bittersweet/mixed` = good; positions with only `dark/worst` left are "traps"). `hint`
appears in the endings tracker until found. `text` is shown verbatim.

## Hints

Ordered `[{done, hint}]`. The current hint is the first whose `done` condition is false;
otherwise "You've done everything there is to do." Used by the `hint` verb and the director.
`done` usually ORs the step with later-step flags so it stays satisfied.

## Variants

```json
"variants": [ { "id": "key_spot", "item": "brass_key", "options": [
  { "at": "drawer", "text": "in my desk" }, { "at": "sea_chest", "text": "in the old sea chest in the cellar" } ] } ]
```

Each game picks one option per variant (`pick_choices`, random seed per new game in the server;
the first option when no seed, which is what tests, `play` without `--layout`, and `Game(world)`
use). `make_instance` removes the item from every room and container, puts it at `at` (a room or a
container item), and replaces `{id}` anywhere in the world copy with the option's `text`.
validate: id + known item, >= 2 options, each `at` a room or container, and **every `{placeholder}`
in the whole file must be a variant id** (regex `\{([a-z_]+)\}` over the JSON dump, so avoid
literal `{lowercase}` in prose). The solver checks every layout combination.

## Modes

Built-in (engine.py:45): `relaxed` (deadline ×1.5, no timers, hints), `normal`, `hard` (×0.85,
timers, no hints). Override per world: `"modes": {"hard": {"deadline": 0.75, "label": "..."}}`;
allowed settings `deadline`, `timers`, `hints`, `label`. Scaling: deadline `max(1, round(d*s))`,
each beat `max(1, min(deadline-1, round(t*s)))`. Timer turn counts and interaction costs are not
scaled.

## What validate() checks, in order (engine.py:807-950)

1. Top-level keys present: `id title intro start map rooms items uses endings verb_defaults`
   (returns early if any are missing).
2. `start` is a room; each room has a map position, `name` and `desc`; each exit direction is valid
   and leads to a known room; flagged exits have `blocked`; room items exist; every map entry is a room.
3. Items have `name aliases desc`; `contains` items exist; talk lists end with an unconditional line,
   each line has `say`, conditions valid, `reveal` targets valid.
4. `verb_defaults.light/unlock/fill` name items.
5. Uses: item/target known, `fact` present/valid, `where` room, `ending` valid, `refuse` entries.
6. Own verbs: format, not built-in, synonyms format, has an interaction.
7. Variants: id/item, >= 2 options, options are rooms or containers.
8. Theme values are `#rrggbb`.
9. Modes: known mode names and settings.
10. Placeholders all have variants.
11. Interactions: verb allowed, target known, fact, cost, move, move_player, ending.
12. Clock: int deadline, known `deadline_ending` (note: only a plain id is accepted here, though the
    engine's `finish` would take an option list).
13. Hint `done` conditions.
14. Timers: int `turns` and `out_fact`.
15. Endings have `title text tone`.

Returns a list of human-readable problems; `load_world` raises `ValueError` listing them all.

## Keeping things in step

Adding a world key means updating **four** places: the engine (behaviour), `validate()`,
`schemas/world.schema.json`, and `docs/writing-a-game.md` (also `DRAFT_RULES` in `cli.py` if
drafts should use it). See extending.md.
