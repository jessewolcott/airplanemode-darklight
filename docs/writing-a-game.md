# Writing a game

A game is one JSON file in `worlds/`. Its file name is its id (`worlds/sunkenbell.json` is
`sunkenbell`). The engine, server, page, parser, narrator and solver are shared, so a new game is
only that file. No code changes are needed.

## The loop

```
python cli.py new sunkenbell --title "The Sunken Bell"   # start from templates/starter.json
python cli.py map sunkenbell                             # check the layout
python cli.py play sunkenbell                            # play it in the terminal
python cli.py check sunkenbell                           # validate + solve; fix until it's clean
python cli.py solve sunkenbell                           # fastest routes, traps, choices that close endings
```

Or have Claude write the first draft on your dev machine, checked by the same tools:

```
pip install -r requirements-dev.txt
python cli.py draft "A diver trapped in a sunken bell tower as the tide turns" --id sunkenbell
```

VS Code checks world files against `schemas/world.schema.json` as you type, and autocompletes keys.

When it's clean: play it through, add a route for each ending to `tests/test_endings.py`, and
commit. On the box, the server loads every file in `worlds/` (plus `WORLDS_DIR` if set), so a new
game shows up in the picker without any other change.

## Building blocks

**Top level.** `title`, `blurb` (one line for the picker), `setting` (a few words the narrator
uses: "a lighthouse during a storm"), `intro`, `start` (room id), `theme` (`accent`: a colour like
`#f2b34b`), `ui` (`clock_title`, `clock_labels` as `[[turns_left_above, "label"], ...]`,
`over_label`, `map_label`).

**Rooms.** `name`, `aliases` (other words players use for the place: "beach" for "Rocky
Shore"), `desc`, `exits` (`north`/`south`/`east`/`west`/`up`/`down` to a room id, or
`{"to", "flag", "blocked"}` for an exit that opens when a flag is set), `items`, and optionally
`dark` with `dark_desc`. `map` gives every room a grid position `[x, y]` for the side panel.

**Items.** `name`, `aliases` (lowercase words players type; the longest match wins), `desc`.
Options:
- `portable`: can be taken.
- `person`: talks, and can be helped.
- `container` with `contains`, `desc_open` and `open_fact`.
- `locked_flag`: a door that opens when that flag is set.
- `light_flag`: lights a dark room while that flag is set, carried or on the floor.
- `desc_flags`: `{flag: description}`. The last matching flag wins.
- `talk`: a list of `{if, say, set, reveal}` lines. The first line whose `if` matches is said. The
  last line has no `if`. `set` sets a flag the first time the line is said, and `reveal` puts an
  item into a room (a tip that uncovers something).

**Uses** (item on target), which is how most puzzles work:
- `item`, `target`, `fact`.
- `set`, `clear` and `renew` flags (`renew` restarts a timer).
- `requires` with a `missing` message, and `unless` with an `already` message.
- `refuse`: a list of `{if, say}` for other reasons it can't happen yet.
- `where`: a room id, with a `wrong_place` message.
- `consume`: the item is used up.
- `ending`: the use ends the game.

`verb_defaults` names the tool that `light`, `unlock` and `fill` use when the player doesn't say
("light lamp" means use the matches on the lamp). `tool_missing` gives each one a message for when
the player doesn't have the tool.

**Interactions:** `help` or `board` a target, or one of the world's own `verbs`. Keys:
- `requires` with a `missing` message: a flag that must be set first.
- `fact`.
- `set`, `unless` and `already`.
- `cost`: extra turns the action takes.
- `move`: `{item: room}`, to move people or things.
- `move_player`: a room id.
- `ending`.

**The world's own verbs.** `"verbs": {"pray": ["kneel"], "push": ["shove", "press"]}` adds verbs
the parser and the model both understand. Give each one at least one interaction.

**Time.** `clock`: `deadline` (turns), `deadline_ending`, and `beats` (`{turn, fact}`), which are
shown exactly as written. Only actions that change something use a turn. Looking, examining,
failed attempts and repeated talk are free. `timers` makes a flag run out:
`{"lantern_lit": {"turns": 20, "warn": 4, "warn_fact", "out_fact", "then": "lantern_dry"}}`.

**Endings.** `{title, tone, hint, text}`. The tone is one of `best`, `good`, `bittersweet`, `mixed`,
`dark` or `worst`. It sets the colour, and the solver uses it to spot traps where only `dark` or
`worst` endings are left. `hint` shows in the tracker until the ending is found.

**Hints.** An ordered list of `{done, hint}`. The first hint whose `done` isn't true yet is the
current one.

**Variants** (a different layout each game):
`"variants": [{"id": "key_spot", "item": "brass_key", "options": [{"at": "drawer", "text": "in my desk"}, {"at": "sea_chest", "text": "in the sea chest"}]}]`.
`at` is a room or a container. Any text can say `{key_spot}`, and it's filled in with the chosen
option's `text`. Real games pick a random layout. Tests and the solver use every one.

**Modes.** Every game gets `relaxed` (1.5× the deadline, no timers), `normal` and `hard` (0.85× the
deadline, no hints). Override per world with `"modes": {"hard": {"deadline": 0.75}}`.

**Conditions.** `has:item`, `flag:name` and `seen:room`, joined with `|` for "or". Anything that
can vary (`ending`, `fact`, `cost`) can be a list of `{"if": ..., "<key>": ...}` options, checked in
order. The last option has no `if`.

## What the checks catch

`validate` (run on start-up, and by `check`):
- an exit or item that names something that doesn't exist
- a talk or ending list with no fallback
- a bad condition
- an unknown mode or colour
- a `{placeholder}` with no variant
- a verb that has no interactions

`solve` plays every possible game through the real engine. It reports:
- **unreachable endings**
- **endings too slow for the deadline**
- **dead ends**: positions from which no ending is possible
- **traps**: only bad endings left
- **moves that close off an ending for good**. Some of these are the choices you meant to make,
  and some are surprises.

It skips "drop" moves unless you pass `--drops`, which is slower but finds things like dropping
the only light in a dark room.

## Writing for the narrator

The on-device model rewrites your facts as prose, and guards throw its version out if it drifts.
Help it:
- Use present tense and second person.
- Keep item names short and concrete ("brass key", not "the curious key of brass").
- Give each item a final word that's distinctive. The guard checks that word survives.
- People are "they". The guards reject he and she, so gendered prose falls back to plain text.
