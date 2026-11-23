# The engine (`engine.py`)

The engine owns all state and rules. It has no I/O, no model, no randomness except
`pick_choices(world, seed)`. A command is a dict like `{"verb": "use", "object": "matches",
"target": "lantern"}`; a turn returns a list of **facts** (plain English strings).

## Constants (engine.py:15-49)

- `DIRECTIONS` = north south east west up down; `DIR_SYN` maps `n s e w u d upstairs downstairs`.
- `VERB_SYN`: first-word verb synonyms for the rule parser (e.g. `get/grab/pick/collect → take`,
  `read/check/search/x → examine`, `pour/apply → use`, `strike/burn/ignite → light`,
  `refuel → fill`, `ask/speak → talk`, `rescue/save → help`, `row/sail/flee/escape/ride → board`,
  `i/inv → inventory`, `l → look`).
- `STOP` words dropped by the rule parser; `SPLIT` prepositions (`with on in into onto using`).
- `FREE_VERBS` = help hint inventory look examine (never increment the turn counter).
- `MULTI_VERBS` = take drop examine (accept `objects` lists).
- `BUILTIN_VERBS` (can't be reused as world verbs) and `MODES` (relaxed/normal/hard).

## Building a game instance

`Game(world, choices=None, mode="normal")` (engine.py:115):

1. `make_instance` (engine.py:80) deep-copies the authored world, places variant items, fills
   `{placeholders}`, scales the clock for the mode, empties `timers` if the mode disables them, and
   records `_mode` / `_choices`. **The loaded world dict is never mutated** (tested).
2. `self.base` = authored world (used by restore/undo and `hints_on`), `self.world` = instance.
3. Mutable state:

| Attribute | Type | Meaning |
|---|---|---|
| `rooms`, `items` | dict copies | room `items` lists and item `contains`/`open` change during play |
| `room` | str | current room |
| `inv` | list | carried item ids, in pick-up order |
| `flags` | set | story flags |
| `visited` / `seen` | sets | entered at all / entered while lit |
| `taken` | set | items ever picked up (first take counts as progress) |
| `turns` | int | turn counter (the clock) |
| `since_progress` | int | turns since the last progress (director) |
| `timers` | dict | flag → turns left |
| `ending`, `ending_text` | | set by `finish()`; `over` property is `ending is not None` |
| `beats_now` | list | authored facts produced by `tick` this turn (shown verbatim) |
| `said` | tuple/None | `(speaker_id, line)` if someone spoke this turn (for voicing) |
| `_progress` | bool | set by `progress()` during a turn |

## Visibility and resolution

- `lit_here()`: any item in inventory or on the floor whose `light_flag` is set.
- `is_dark()`: room is `dark` and not lit. `visible()` = room items (none if dark) + inventory.
- `resolve(words)` (engine.py:165): among visible items, the one with the longest alias whose
  words are all present in `words` (exact words, no fuzziness). Used by `parse_rule`.
- `room_facts()` (engine.py:219): `Location: X.`, desc, `The <people> is here.`,
  `Items here: <portable names>.`, `Exits: ...`. In the dark: pitch black + `dark_desc` +
  `You can feel a way <exits>.` Non-portable, non-person items are not listed (describe them in
  the room `desc`). These exact prefixes matter: `plain()` and the narration guard parse them.
- `status()` (engine.py:238): the JSON the page renders (room, room_id, visited, exits, items,
  inventory, dark, turns, deadline, flags, over, mode, ending {id,title,tone}).

## The turn: `do(cmd)` (engine.py:306)

```
verb = cmd.verb or "unknown"; "", "none" values become None
"help" with an object -> "help_npc"
reset _progress, said, beats_now, ending_text
before = turns; sig = _signature()
if verb not in FREE_VERBS: turns += 1
handler = v_<verb>, or for a world verb: interact(verb, object)
if objects list and verb in MULTI_VERBS: run handler per object, concatenating facts
else facts = handler(cmd) or "You're not sure how to do that."
if _signature() == sig: turns = before        # nothing changed -> free turn
if not dark: seen.add(room)
if not over: facts += tick(before)
since_progress = 0 if _progress else +1 (except for help/hint verbs)
return facts
```

**Free turns**: `_signature()` (engine.py:299) = (room, inventory tuple, flags, ending, every
room's item list, every item's open state). If none changed, the turn costs nothing. So looks,
failed attempts ("You can't go west"), and repeated talk are free; talk that sets a new flag or a
take costs one. Timers, `visited`/`seen` and `since_progress` are not part of the signature.
Interaction `cost` adds extra turns inside the handler (it always accompanies a state change).

**`tick(before)`** (engine.py:339): `passed = turns - before`; if > 0: `run_timers(passed)`, then
clock beats with `before < turn <= turns`; `beats_now` = those facts; then if
`turns >= deadline`, `finish(deadline_ending)` and append the ending text. Because `tick` runs only
if not already over, a winning move on the last turn beats the deadline.

**`run_timers(passed)`** (engine.py:355): for each running timer: drop it if its flag was removed;
decrement by `passed`; at <= 0 remove timer and flag, set `then`, add `out_fact`; else add
`warn_fact` when the count crosses `warn` (`left > warn >= now`).

**`finish(spec)`** (engine.py:375): resolves an option list; unknown/None → no ending; else sets
`ending` and `ending_text`.

## Verb handlers

| Verb | Handler | Behaviour |
|---|---|---|
| `unknown` | `v_unknown` | "The narrator doesn't understand that. Try ..." |
| `help` (no object) | `v_help` | command list. Free |
| `hint` | `v_hint` | `Hint: <current_hint>` or the hard-mode refusal. Free |
| `look` | `v_look` | room facts. Free |
| `inventory` | `v_inventory` | carried list. Free |
| `go` | `v_go` | needs `direction`; locked exit → `blocked`; first visit = progress; returns `You go <d>.` + room facts (the narration guard requires `<d>` to survive) |
| `take` | `v_take` | dark → too dark; not visible / already held / not portable messages; moves item room→inv; first ever take = progress. `all` flag changes the empty message |
| `drop` | `v_drop` | inv→room (not progress) |
| `examine` | `v_examine` | no object → room facts; else `describe_item` (desc, `desc_flags`, `desc_open`). Free |
| `open` | `v_open` | `locked_flag` items: locked/already open; containers: open once, spill contents into the room, `open_fact` + `Inside: ...` or `It is empty.`; progress |
| `talk` | `v_talk` | object or the first visible person; dark with a person in the room → "Someone groans in reply."; first matching line; `set` (first time only, progress) and `reveal`; records `said` |
| `help` + object | `v_help_npc` → `interact("help", o)` | |
| `board` | `v_board` → `interact("board", o)` | |
| world verb | lambda → `interact(verb, o)` | |
| `light`/`unlock`/`fill` | `_shortcut` (engine.py:548) → `v_use` | `light X`: object becomes the default tool, X the target. `unlock X`/`fill X`: object defaults to the tool, X the target. If the tool isn't visible: "You don't see that here." if the target isn't visible either, else `tool_missing[verb]` or "You have nothing to <verb> it with." If the tool is visible but not held, `v_use` says "You need to be holding the X first." |
| `use` | `v_use` (engine.py:572) | see world-format.md "Uses" for the rule order. Target inference: rules for this item whose target is visible; if more than one, prefer targets not in your inventory; exactly one → use it, else "Use the X on what?" |

`interact(verb, o)` (engine.py:511): infer target if exactly one visible rule target; dark → "too
dark"; not visible; no rule → "You can't <verb> the X."; `unless`/`requires`; pick cost and fact
first; `set`; `turns += cost`; `move` (via `place`, which removes the item from any room or the
inventory and puts it in the destination room); `move_player`; `ending`; progress.

## Endings, traps and "solving"

Endings come from three places: a use's `ending`, an interaction's `ending`, or the clock's
`deadline_ending`. Each can be conditional on flags/inventory/seen rooms via option lists (e.g.
darklight's lamp lights to `keeper_light` if `flag:keeper_rescued`, else `lone_light`). Once
`ending` is set, `over` is true and `run_turn` refuses further turns except `undo`.

Traps and dead ends are not engine concepts; they are found by the solver (solver-and-cli.md):
a *dead end* is a reachable position with no ending possible (a bug; check fails), a *trap* is a
position where only `dark`/`worst` endings remain (often intended, reported only). Darklight's
designed tension: rescuing the keeper costs turns (4, or 1 with the tackle the keeper tells you
about), the lantern burns out after 20 turns (refillable with oil, but the oil is also the lamp's
fuel and is consumed by it), and the ship arrives at turn 38.

## Snapshot and restore (engine.py:622-689)

`snapshot()` is plain JSON: world id, mode, variant choices, room, inv, flags, visited, seen,
taken, turns, since_progress, timers, ending, each room's item list, open items, container
contents. It is sent to the browser after every turn (`save`), used for undo, and stored behind
save codes.

`Game.restore(world, s)` rebuilds `Game(world, choices, mode)` and then overwrites state, checking
everything because saves come from the browser: mode known; choices exactly match the variants
and options; world id matches; room ids and item ids exist; list lengths <= 500; flags list <= 200
strings of 1-64 chars (flag names are **not** checked against the world); ints 0..100000; timer
keys exist in the instance's timers; ending known. Any failure raises `ValueError` (the server
turns it into HTTP 400). Note `beats_now`, `said` and `ending_text` are not saved; a restored game
that is over has `ending` but no `ending_text`, which is fine because further turns are refused.
(The page never resumes a save whose `ending` is set; it starts a new game instead.)

## The rule parser: `parse_rule(text, game)` (engine.py:692)

Fast and exact; returns `None` whenever unsure so smarter layers can try.

1. Lowercase, commas → ` and `, words = `[a-z]+` minus `STOP`. Empty → None.
2. One word that is a direction → `go`.
3. First word must be a verb word (`game.verb_words()`: `VERB_SYN` + world verbs), else None.
4. World verb: resolve the rest; words present but no item → None.
5. `look` alone → look; `look <x>` → examine.
6. `inventory` with extra words → None ("i need a hint"). `inventory`/`hint` → bare.
7. `help`/`talk`/`board`: resolve rest (unresolvable words → None).
8. `go`: drop `back stairs ladder tower`; exactly one direction word → go; bare `climb` → up;
   otherwise None ("walk up the path to the house" is left to the model/fallback).
9. `take up X` → take X. `take all`/`everything` → all portable visible not-held items (or
   `{"object": None, "all": True}`); `drop all` → inventory.
10. Multi-object for take/drop/examine with `and`: `split_objects` (all parts must resolve; "block
    and tackle" resolves to one item twice, so stays single).
11. `open X with Y` → `use Y on X`.
12. take/drop/examine/open: resolve; `take <person>` → `help <person>`; `take <non-portable
    interaction target>` → None ("get in the boat").
13. Two-noun verbs split at the first `SPLIT` word: `use A [on B]` (A must resolve and not be a
    person); `light A` (if A is the light tool, retry without its alias words: "strike a match and
    light the lantern"); `unlock/fill A [with B]` → `use B on A` if B else the shortcut.
14. Anything else → None.

Solver routes are written in this grammar ("take matches", "use oil on lamp", "help keeper"), so
tests can replay them through `parse_rule`.

## Loading

`load_world(path)` parses, sets `id` from the file stem, validates, raises with all problems.
`load_worlds(*folders, only=None)` loads every `*.json` from each folder in order (later folders
override same ids), optionally filtered, and raises if none are found.
