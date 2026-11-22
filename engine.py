"""Deterministic game engine.

The engine owns ALL game state and rules: the fixed map, the clock, the branching
flags and the endings. The language model never decides what is true; it only
(a) helps turn free text into a command and (b) rewrites the engine's facts as prose.

A world is a JSON file in worlds/. validate() checks one before the server will run it.
"""
import copy
import json
import random
import re
from pathlib import Path

DIRECTIONS = ["north", "south", "east", "west", "up", "down"]

DIR_SYN = {"n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down",
           "upstairs": "up", "downstairs": "down"}
VERB_SYN = {
    "l": "look", "look": "look",
    "go": "go", "walk": "go", "move": "go", "run": "go", "head": "go", "climb": "go", "enter": "go",
    "take": "take", "get": "take", "grab": "take", "pick": "take", "collect": "take",
    "drop": "drop", "discard": "drop",
    "examine": "examine", "x": "examine", "inspect": "examine", "read": "examine", "check": "examine", "search": "examine",
    "open": "open",
    "use": "use", "pour": "use", "apply": "use",
    "light": "light", "ignite": "light", "strike": "light", "burn": "light",
    "unlock": "unlock", "fill": "fill", "refuel": "fill",
    "talk": "talk", "speak": "talk", "ask": "talk",
    "help": "help", "rescue": "help", "save": "help",
    "board": "board", "row": "board", "sail": "board", "flee": "board", "escape": "board", "ride": "board",
    "i": "inventory", "inv": "inventory", "inventory": "inventory",
    "hint": "hint",
}
STOP = {"the", "a", "an", "my", "please", "some", "then", "at", "to", "around", "this", "that", "away"}
SPLIT = {"with", "on", "in", "into", "onto", "using"}
FREE_VERBS = ("help", "hint", "inventory", "look", "examine")  # never tick the clock
MULTI_VERBS = ("take", "drop", "examine")                      # "take the matches and the lantern"
ALL_WORDS = ({"all"}, {"everything"})


BUILTIN_VERBS = {"look", "go", "take", "drop", "examine", "open", "use", "light", "unlock", "fill",
                 "talk", "help", "board", "inventory", "hint", "unknown"}
# Difficulty applies to every world. A world can override any of these in "modes".
MODES = {
    "relaxed": {"deadline": 1.5, "timers": False, "hints": True, "label": "Relaxed: more time, no burning out"},
    "normal": {"deadline": 1.0, "timers": True, "hints": True, "label": "Normal"},
    "hard": {"deadline": 0.85, "timers": True, "hints": False, "label": "Hard: less time, no hints"},
}


def to_dir(word):
    if word in DIRECTIONS:
        return word
    return DIR_SYN.get(word)


def modes_for(world):
    return {m: {**spec, **world.get("modes", {}).get(m, {})} for m, spec in MODES.items()}


def pick_choices(world, seed=None):
    """Which option each variant uses. No seed = the authored layout (every first option)."""
    rng = random.Random(seed) if seed is not None else None
    return {v["id"]: (rng.choice(v["options"]) if rng else v["options"][0])["at"] for v in world.get("variants", [])}


def _fill(obj, subs):
    if isinstance(obj, str):
        for k, v in subs.items():
            obj = obj.replace("{" + k + "}", v)
        return obj
    if isinstance(obj, list):
        return [_fill(x, subs) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, subs) for k, v in obj.items()}
    return obj


def make_instance(world, choices=None, mode="normal"):
    """The world as this particular game plays it: variant items placed, {placeholders} filled
    in, and the difficulty applied. The authored file is never changed."""
    choices = choices if choices is not None else pick_choices(world)
    subs = {}
    inst = copy.deepcopy(world)
    for v in world.get("variants", []):
        opt = next(o for o in v["options"] if o["at"] == choices[v["id"]])
        iid, at = v["item"], opt["at"]
        for r in inst["rooms"].values():
            if iid in r["items"]:
                r["items"].remove(iid)
        for it in inst["items"].values():
            if iid in it.get("contains", []):
                it["contains"].remove(iid)
        if at in inst["rooms"]:
            inst["rooms"][at]["items"].append(iid)
        else:
            inst["items"][at].setdefault("contains", []).append(iid)
        subs[v["id"]] = opt.get("text", "")
    if subs:
        inst = _fill(inst, subs)
    spec = modes_for(world)[mode]
    clock = inst.get("clock")
    if clock and spec["deadline"] != 1.0:
        scale = spec["deadline"]
        clock["deadline"] = max(1, round(clock["deadline"] * scale))
        for b in clock["beats"]:
            b["turn"] = max(1, min(clock["deadline"] - 1, round(b["turn"] * scale)))
    if not spec["timers"]:
        inst["timers"] = {}
    inst["_mode"], inst["_choices"] = mode, dict(choices)
    return inst


class Game:
    def __init__(self, world, choices=None, mode="normal"):
        self.base = world                      # the authored world, as loaded
        self.mode = mode if mode in MODES else "normal"
        world = make_instance(world, choices, self.mode)
        self.world = world                     # this game's copy: variants placed, difficulty applied
        self.choices = world["_choices"]
        self.rooms = copy.deepcopy(world["rooms"])
        self.items = copy.deepcopy(world["items"])
        self.room = world["start"]
        self.inv = []
        self.flags = set()
        self.visited = {self.room}
        self.seen = {self.room}
        self.taken = set()
        self.turns = 0
        self.since_progress = 0
        self.timers = {}          # flag -> turns left before it runs out
        self.ending = None
        self.ending_text = None
        self.beats_now = []
        self.said = None          # (speaker id, line) when someone spoke this turn
        self._progress = False

    @property
    def over(self):
        return self.ending is not None

    # ---------- queries ----------
    def r(self):
        return self.rooms[self.room]

    def name(self, iid):
        return self.items[iid]["name"]

    def lit_here(self):
        """A burning light source in your hands OR on the floor lights the room."""
        for iid in self.inv + self.r()["items"]:
            flag = self.items[iid].get("light_flag")
            if flag and flag in self.flags:
                return True
        return False

    def is_dark(self):
        return bool(self.r().get("dark")) and not self.lit_here()

    def visible(self):
        room_items = [] if self.is_dark() else list(self.r()["items"])
        return room_items + self.inv

    def resolve(self, words):
        """Match words against aliases of visible things; longest alias wins."""
        ws = set(words)
        best, best_len = None, 0
        for iid in self.visible():
            for alias in self.items[iid]["aliases"]:
                parts = alias.split()
                if all(p in ws for p in parts) and len(parts) > best_len:
                    best, best_len = iid, len(parts)
        return best

    def check(self, cond):
        for c in cond.split("|"):
            kind, val = c.split(":", 1)
            if kind == "has" and val in self.inv:
                return True
            if kind == "flag" and val in self.flags:
                return True
            if kind == "seen" and val in self.seen:
                return True
        return False

    def pick(self, spec, key):
        """A plain value, or a list of {if, <key>} options checked in order. The last has no 'if'."""
        if isinstance(spec, list):
            return next((e[key] for e in spec if "if" not in e or self.check(e["if"])), None)
        return spec

    def current_hint(self):
        for h in self.world.get("hints", []):
            if not self.check(h["done"]):
                return h["hint"]
        return "You've done everything there is to do."

    def describe_item(self, iid):
        it = self.items[iid]
        desc = it["desc"]
        for flag, d in it.get("desc_flags", {}).items():
            if flag in self.flags:
                desc = d
        if it.get("container") and it.get("open"):
            desc = it.get("desc_open", desc)
        return desc

    def exits(self):
        return list(self.r()["exits"].keys())

    def exit_names(self):
        out = {}
        for d, ex in self.r()["exits"].items():
            dest = ex["to"] if isinstance(ex, dict) else ex
            out[d] = self.rooms[dest]["name"]
        return out

    def room_facts(self):
        room = self.r()
        facts = [f"Location: {room['name']}."]
        if self.is_dark():
            facts.append("It is pitch black. You cannot see anything at all.")
            if room.get("dark_desc"):
                facts.append(room["dark_desc"])
            facts.append(f"You can feel a way {', '.join(self.exits())}.")
            return facts
        facts.append(room["desc"])
        loose = [self.name(i) for i in room["items"] if self.items[i].get("portable")]
        people = [self.name(i) for i in room["items"] if self.items[i].get("person")]
        if people:
            facts.append(f"The {', '.join(people)} is here.")
        if loose:
            facts.append(f"Items here: {', '.join(loose)}.")
        facts.append(f"Exits: {', '.join(self.exits())}.")
        return facts

    def status(self):
        dark = self.is_dark()
        clock = self.world.get("clock", {})
        ending = self.world["endings"][self.ending] if self.ending else None
        return {
            "room": self.r()["name"],
            "room_id": self.room,
            "visited": sorted(self.visited),
            "exits": self.exits(),
            "items": [] if dark else [self.name(i) for i in self.r()["items"] if self.items[i].get("portable")],
            "inventory": [self.name(i) for i in self.inv],
            "dark": dark,
            "turns": self.turns,
            "deadline": clock.get("deadline"),
            "flags": sorted(self.flags),
            "over": self.over,
            "mode": self.mode,
            "ending": {"id": self.ending, "title": ending["title"], "tone": ending["tone"]} if ending else None,
        }

    def verb_words(self):
        """Built-in verb words plus the ones this world adds ("push": ["shove", "press"])."""
        words = dict(VERB_SYN)
        for verb, syns in self.world.get("verbs", {}).items():
            for w in [verb] + list(syns):
                words[w] = verb
        return words

    def clone(self):
        """A cheap copy for the solver: only the parts a turn can change are copied."""
        g = Game.__new__(Game)
        g.__dict__.update(self.__dict__)
        g.rooms = {rid: {**r, "items": list(r["items"])} for rid, r in self.rooms.items()}
        g.items = {iid: ({**it, "contains": list(it["contains"])} if "contains" in it else dict(it))
                   for iid, it in self.items.items()}
        g.inv, g.flags, g.visited = list(self.inv), set(self.flags), set(self.visited)
        g.seen, g.taken, g.timers = set(self.seen), set(self.taken), dict(self.timers)
        g.beats_now = []
        return g

    # ---------- state changes ----------
    def set_flag(self, flag):
        self.flags.add(flag)
        timer = self.world.get("timers", {}).get(flag)
        if timer:
            self.timers[flag] = timer["turns"]

    def renew(self, flag):
        if flag in self.flags and flag in self.world.get("timers", {}):
            self.timers[flag] = self.world["timers"][flag]["turns"]

    def place(self, moves):
        """Move items (or people) to rooms, wherever they are now."""
        for iid, dest in moves.items():
            for room in self.rooms.values():
                if iid in room["items"]:
                    room["items"].remove(iid)
            if iid in self.inv:
                self.inv.remove(iid)
            self.rooms[dest]["items"].append(iid)

    def _signature(self):
        """Everything a turn can change. If this is unchanged, the turn was free."""
        return (self.room, tuple(self.inv), frozenset(self.flags), self.ending,
                tuple(tuple(r["items"]) for r in self.rooms.values()),
                tuple(bool(it.get("open")) for it in self.items.values()))

    # ---------- turn ----------
    def do(self, cmd):
        verb = cmd.get("verb") or "unknown"
        clean = {k: (None if v in (None, "", "none") else v) for k, v in cmd.items()}
        if verb == "help" and clean.get("object"):
            verb = "help_npc"
        self._progress = False
        self.said = None
        self.beats_now, self.ending_text = [], None  # authored text, shown verbatim
        before, sig = self.turns, self._signature()
        if verb not in FREE_VERBS:
            self.turns += 1
        handler = getattr(self, "v_" + verb, None)
        if handler is None and verb in self.world.get("verbs", {}):
            handler = lambda c, v=verb: self.interact(v, c.get("object"))  # noqa: E731  a world's own verb
        objects = clean.get("objects")
        if handler and objects and verb in MULTI_VERBS:
            facts = []
            for o in objects:
                facts += handler({**clean, "object": o})
        else:
            facts = handler(clean) if handler else ["You're not sure how to do that."]
        if self._signature() == sig:
            self.turns = before  # nothing changed (a look, a failed attempt, small talk): time doesn't pass
        if not self.is_dark():
            self.seen.add(self.room)
        if not self.over:
            facts += self.tick(before)
        if self._progress:
            self.since_progress = 0
        elif verb not in ("help", "hint"):
            self.since_progress += 1
        return facts

    def tick(self, before):
        """Advance the story clock: timers, fixed beats at fixed times, and a deadline."""
        passed = self.turns - before
        if passed <= 0:
            return []
        facts = self.run_timers(passed)
        clock = self.world.get("clock")
        if clock:
            facts += [b["fact"] for b in clock["beats"] if before < b["turn"] <= self.turns]
        self.beats_now = list(facts)
        if clock and self.turns >= clock["deadline"]:
            text = self.finish(clock["deadline_ending"])
            if text:
                facts.append(text)
        return facts

    def run_timers(self, passed):
        facts = []
        specs = self.world.get("timers", {})
        for flag, left in list(self.timers.items()):
            if flag not in self.flags:
                del self.timers[flag]
                continue
            spec, now = specs[flag], left - passed
            if now <= 0:
                del self.timers[flag]
                self.flags.discard(flag)
                if spec.get("then"):
                    self.flags.add(spec["then"])
                facts.append(spec["out_fact"])
            else:
                if spec.get("warn_fact") and left > spec.get("warn", 0) >= now:
                    facts.append(spec["warn_fact"])
                self.timers[flag] = now
        return facts

    def finish(self, spec):
        """spec is an ending id, or a list of {if, ending} checked in order."""
        spec = self.pick(spec, "ending")
        if spec not in self.world["endings"]:
            return None
        self.ending = spec
        self.ending_text = self.world["endings"][spec]["text"]
        return self.ending_text

    def progress(self):
        self._progress = True

    # ---------- verbs ----------
    def v_unknown(self, c):
        return ["The narrator doesn't understand that. Try something like 'go north', 'take lantern' or 'help'."]

    def v_help(self, c):
        return ["Commands: look, go <direction>, take <thing>, drop <thing>, examine <thing>, open <thing>, "
                "use <thing> on <thing>, light <thing>, talk to <someone>, help <someone>, inventory, hint, undo. "
                "Plain English usually works too. Time matters, but looking around is free."]

    def hints_on(self):
        return modes_for(self.base)[self.mode]["hints"]

    def v_hint(self, c):
        if not self.hints_on():
            return ["No hints in hard mode. You're on your own."]
        return [f"Hint: {self.current_hint()}"]

    def v_look(self, c):
        return self.room_facts()

    def v_inventory(self, c):
        if not self.inv:
            return ["You are empty-handed."]
        return [f"You are carrying: {', '.join(self.name(i) for i in self.inv)}."]

    def v_go(self, c):
        d = c.get("direction")
        if not d:
            return ["Go where? Try a direction like north or up."]
        ex = self.r()["exits"].get(d)
        if ex is None:
            return [f"You can't go {d} from here."]
        if isinstance(ex, dict):
            if ex.get("flag") and ex["flag"] not in self.flags:
                return [ex["blocked"]]
            ex = ex["to"]
        self.room = ex
        if ex not in self.visited:
            self.visited.add(ex)
            self.progress()
        return [f"You go {d}."] + self.room_facts()

    def v_take(self, c):
        o = c.get("object")
        if self.is_dark():
            return ["It's too dark to find anything."]
        if not o:
            return ["There's nothing here you can carry." if c.get("all") else "You don't see that here."]
        if o not in self.visible():
            return ["You don't see that here."]
        if o in self.inv:
            return [f"You already have the {self.name(o)}."]
        if not self.items[o].get("portable"):
            return [f"You can't carry the {self.name(o)}."]
        self.r()["items"].remove(o)
        self.inv.append(o)
        if o not in self.taken:
            self.taken.add(o)
            self.progress()
        return [f"You take the {self.name(o)}."]

    def v_drop(self, c):
        o = c.get("object")
        if not o:
            return ["You aren't carrying anything." if c.get("all") else "You aren't carrying that."]
        if o not in self.inv:
            return ["You aren't carrying that."]
        self.inv.remove(o)
        self.r()["items"].append(o)
        return [f"You drop the {self.name(o)}."]

    def v_examine(self, c):
        o = c.get("object")
        if not o:
            return self.room_facts()
        if o not in self.visible():
            return ["You don't see that here." if not self.is_dark() else "It's too dark to see."]
        return [self.describe_item(o)]

    def v_open(self, c):
        o = c.get("object")
        if not o or o not in self.visible():
            return ["You don't see that here."]
        it = self.items[o]
        if it.get("locked_flag"):
            if it["locked_flag"] in self.flags:
                return [f"The {it['name']} is already open."]
            return [f"The {it['name']} is locked."]
        if not it.get("container"):
            return [f"You can't open the {it['name']}."]
        if it.get("open"):
            return [f"The {it['name']} is already open."]
        it["open"] = True
        contents = it.get("contains", [])
        self.r()["items"].extend(contents)
        it["contains"] = []
        self.progress()
        facts = [it.get("open_fact", f"You open the {it['name']}.")]
        facts.append(f"Inside: {', '.join(self.name(i) for i in contents)}." if contents else "It is empty.")
        return facts

    def v_talk(self, c):
        o = c.get("object") or next((i for i in self.visible() if self.items[i].get("person")), None)
        if not o or o not in self.visible():
            if self.is_dark() and any(self.items[i].get("person") for i in self.r()["items"]):
                return ["You call out into the dark. Someone groans in reply."]
            return ["There's no one here to talk to."]
        lines = self.items[o].get("talk")
        line = next((l for l in lines or [] if "if" not in l or self.check(l["if"])), None)
        if not line:
            return [f"The {self.name(o)} has nothing to say."]
        self.said = (o, line["say"])
        if line.get("set") and line["set"] not in self.flags:
            self.set_flag(line["set"])
            self.progress()
        self.place(line.get("reveal", {}))
        return [line["say"]]

    def v_help_npc(self, c):
        return self.interact("help", c.get("object"))

    def v_board(self, c):
        return self.interact("board", c.get("object"))

    def interact(self, verb, o):
        rules = [i for i in self.world.get("interactions", []) if i["verb"] == verb]
        if o is None:
            here = [i for i in rules if i["target"] in self.visible()]
            if len(here) != 1:
                return ["You're not sure what you mean."]
            o = here[0]["target"]
        if self.is_dark():
            return ["It's too dark. You'd need a light first."]
        if o not in self.visible():
            return ["You don't see that here."]
        rule = next((i for i in rules if i["target"] == o), None)
        if not rule:
            return [f"You can't {verb} the {self.name(o)}."]
        if rule.get("unless") and rule["unless"] in self.flags:
            return [rule.get("already", "Nothing more happens.")]
        if rule.get("requires") and rule["requires"] not in self.flags:
            return [rule.get("missing", "Nothing happens.")]
        cost = self.pick(rule.get("cost", 0), "cost") or 0   # decide cost and text before flags change
        fact = self.pick(rule["fact"], "fact")
        if rule.get("set"):
            self.set_flag(rule["set"])
        self.turns += cost
        self.place(rule.get("move", {}))
        facts = [fact]
        if rule.get("move_player"):
            self.room = rule["move_player"]
            self.visited.add(self.room)
            facts += self.room_facts()
        if rule.get("ending"):
            text = self.finish(rule["ending"])
            if text:
                facts.append(text)
        self.progress()
        return facts

    # light / unlock / fill are shortcuts that become "use" with the world's default tool
    def _shortcut(self, c, verb):
        c = dict(c)
        default = self.world["verb_defaults"][verb]
        if verb == "light":
            c["target"] = c.get("target") or c.get("object")
            c["object"] = default
        else:
            c["object"] = c.get("object") or default
        if c["object"] == default and default not in self.visible():
            t = c.get("target")
            if t and t not in self.visible():
                return ["You don't see that here."]
            return [self.world.get("tool_missing", {}).get(verb) or f"You have nothing to {verb} it with."]
        return self.v_use(c)

    def v_light(self, c):
        return self._shortcut(c, "light")

    def v_unlock(self, c):
        return self._shortcut(c, "unlock")

    def v_fill(self, c):
        return self._shortcut(c, "fill")

    def v_use(self, c):
        o, t = c.get("object"), c.get("target")
        if not o:
            return ["Use what?"]
        if o not in self.inv:
            if o in self.visible():
                return [f"You need to be holding the {self.name(o)} first."]
            return ["You aren't carrying that."]
        rules = [u for u in self.world["uses"] if u["item"] == o]
        if t is None:  # infer the target if exactly one rule fits what's here
            here = [u for u in rules if u["target"] in self.visible()]
            if len(here) > 1:  # "use oil" in the lamp room means the lamp, not the lantern you're holding
                here = [u for u in here if u["target"] not in self.inv] or here
            if len(here) == 1:
                t = here[0]["target"]
            else:
                return [f"Use the {self.name(o)} on what?"]
        if t not in self.visible():
            return ["You don't see that here."]
        rule = next((u for u in rules if u["target"] == t), None)
        if not rule:
            return [f"Using the {self.name(o)} on the {self.name(t)} does nothing."]
        if rule.get("where") and rule["where"] != self.room:
            return [rule.get("wrong_place", "Not here.")]
        if rule.get("unless") and rule["unless"] in self.flags:
            return [rule.get("already", "Nothing more happens.")]
        if rule.get("requires") and rule["requires"] not in self.flags:
            return [rule.get("missing", "Nothing happens.")]
        refused = next((r["say"] for r in rule.get("refuse", []) if self.check(r["if"])), None)
        if refused:
            return [refused]
        fact = self.pick(rule["fact"], "fact")
        if rule.get("clear"):
            self.flags.discard(rule["clear"])
            self.timers.pop(rule["clear"], None)
        if rule.get("set"):
            self.set_flag(rule["set"])
        if rule.get("renew"):
            self.renew(rule["renew"])
        if rule.get("consume"):
            self.inv.remove(o)
        self.progress()
        facts = [fact]
        if rule.get("ending"):
            text = self.finish(rule["ending"])
            if text:
                facts.append(text)
        return facts

    # ---------- save / restore ----------
    def snapshot(self):
        """Plain-JSON copy of everything that changes during play."""
        return {
            "world": self.world["id"], "mode": self.mode, "choices": dict(self.choices),
            "room": self.room, "inv": list(self.inv),
            "flags": sorted(self.flags), "visited": sorted(self.visited), "seen": sorted(self.seen),
            "taken": sorted(self.taken), "turns": self.turns, "since_progress": self.since_progress,
            "timers": dict(self.timers), "ending": self.ending,
            "rooms": {rid: list(r["items"]) for rid, r in self.rooms.items()},
            "open": sorted(i for i, it in self.items.items() if it.get("open")),
            "contains": {i: list(it.get("contains", [])) for i, it in self.items.items() if it.get("container")},
        }

    @classmethod
    def restore(cls, world, s):
        """Rebuild a game from snapshot(). Saves come from the browser, so check everything."""
        def need(ok):
            if not ok:
                raise ValueError("this save doesn't match the world")

        need(isinstance(s, dict))
        mode = s.get("mode", "normal")
        need(mode in MODES)
        variants = {v["id"]: {o["at"] for o in v["options"]} for v in world.get("variants", [])}
        choices = s.get("choices") or None
        if choices is not None:
            need(isinstance(choices, dict) and set(choices) == set(variants)
                 and all(choices[k] in variants[k] for k in variants))
        g = cls(world, choices, mode)
        rooms, items = set(g.rooms), set(g.items)

        def ids(xs, valid):
            need(isinstance(xs, list) and len(xs) <= 500 and all(isinstance(x, str) and x in valid for x in xs))
            return list(xs)

        def num(x):
            need(isinstance(x, int) and 0 <= x <= 100000)
            return x

        try:
            need(isinstance(s, dict) and s.get("world") == world["id"] and s.get("room") in rooms)
            g.room = s["room"]
            g.inv = ids(s["inv"], items)
            flags = s["flags"]
            need(isinstance(flags, list) and len(flags) <= 200
                 and all(isinstance(f, str) and 0 < len(f) <= 64 for f in flags))
            g.flags = set(flags)
            g.visited, g.seen = set(ids(s["visited"], rooms)), set(ids(s["seen"], rooms))
            g.taken = set(ids(s["taken"], items))
            g.turns, g.since_progress = num(s["turns"]), num(s["since_progress"])
            timers = s.get("timers", {})
            need(isinstance(timers, dict) and all(k in g.world.get("timers", {}) for k in timers))
            g.timers = {k: num(v) for k, v in timers.items()}
            need(s.get("ending") is None or s["ending"] in world["endings"])
            g.ending = s.get("ending")
            need(isinstance(s["rooms"], dict))
            for rid, its in s["rooms"].items():
                need(rid in rooms)
                g.rooms[rid]["items"] = ids(its, items)
            for iid in ids(s["open"], items):
                g.items[iid]["open"] = True
            need(isinstance(s["contains"], dict))
            for iid, inside in s["contains"].items():
                need(iid in items)
                g.items[iid]["contains"] = ids(inside, items)
        except (KeyError, TypeError, AttributeError) as e:
            raise ValueError("this save is damaged") from e
        return g


def parse_rule(text, game):
    """Fast, exact parser for normal adventure-game phrasing. Returns None if unsure."""
    words = [w for w in re.findall(r"[a-z]+", text.lower().replace(",", " and ")) if w not in STOP]
    if not words:
        return None
    if len(words) == 1 and to_dir(words[0]):
        return {"verb": "go", "direction": to_dir(words[0])}
    verb = game.verb_words().get(words[0])
    rest = words[1:]
    if verb is None:
        return None
    if verb in game.world.get("verbs", {}):  # a world's own verb: "push the lever", "pray"
        obj = game.resolve(rest)
        if rest and not obj:
            return None
        return {"verb": verb, "object": obj}
    if verb == "look":
        if not rest:
            return {"verb": "look"}
        verb = "examine"
    if verb == "inventory" and rest:
        return None  # "i need a hint" isn't the inventory
    if verb in ("inventory", "hint"):
        return {"verb": verb}
    if verb in ("help", "talk", "board"):
        obj = game.resolve(rest)
        if rest and not obj:
            return None  # let the model + fuzzy matcher have a go
        return {"verb": verb, "object": obj}
    if verb == "go":
        # only accept short, unambiguous forms; "walk up the path to the house" goes to the model
        tail = [w for w in rest if w not in {"back", "stairs", "ladder", "tower"}]
        if len(tail) == 1 and to_dir(tail[0]):
            return {"verb": "go", "direction": to_dir(tail[0])}
        if words[0] == "climb" and not tail:
            return {"verb": "go", "direction": "up"}
        return None
    if verb == "take" and rest and rest[0] == "up":
        rest = rest[1:]
    if verb in ("take", "drop") and set(rest) in ALL_WORDS:
        if verb == "take":
            objs = [i for i in game.visible() if game.items[i].get("portable") and i not in game.inv]
        else:
            objs = list(game.inv)
        return {"verb": verb, "objects": objs, "all": True} if objs else {"verb": verb, "object": None, "all": True}
    if verb in MULTI_VERBS and "and" in rest:
        objs = split_objects(rest, game)
        if objs and len(objs) > 1:
            return {"verb": verb, "objects": objs}
    if verb == "open" and any(w in SPLIT for w in rest):  # "open the door with the key" = unlock it
        at = next(i for i, w in enumerate(rest) if w in SPLIT)
        a, b = game.resolve(rest[:at]), game.resolve(rest[at + 1:])
        if a and b:
            return {"verb": "use", "object": b, "target": a}
    if verb in ("take", "drop", "examine", "open"):
        obj = game.resolve(rest)
        if not obj:
            return None
        it = game.items[obj]
        if verb == "take" and it.get("person"):
            return {"verb": "help", "object": obj}  # "get the keeper out of here"
        if verb == "take" and not it.get("portable") and any(
                x["target"] == obj for x in game.world.get("interactions", [])):
            return None  # "get in the boat" isn't picking it up; let the smarter layers decide
        return {"verb": verb, "object": obj}

    # two-noun verbs: split around a preposition
    split_at = next((i for i, w in enumerate(rest) if w in SPLIT), None)
    before = rest if split_at is None else rest[:split_at]
    after = [] if split_at is None else rest[split_at + 1:]
    a, b = game.resolve(before), game.resolve(after)
    if verb == "use":
        if not a or game.items[a].get("person"):
            return None  # "use the pulley to lift the keeper" is about the keeper, not using them
        return {"verb": "use", "object": a, "target": b}
    if verb == "light":  # light X [with Y]
        tool = game.world["verb_defaults"]["light"]
        if a == tool:  # "strike a match and light the lantern": the thing to light isn't the matches
            parts = {p for al in game.items[tool]["aliases"] for p in al.split()}
            a = game.resolve([w for w in before if w not in parts]) or a
        if not a:
            return None
        return {"verb": "light", "object": a}
    if verb in ("unlock", "fill"):  # unlock/fill X [with Y]
        if not a:
            return None
        return {"verb": "use" if b else verb, "object": b, "target": a}
    return None


def split_objects(words, game):
    """'matches and lantern' -> [matches, lantern]. None if any part doesn't resolve.
    'block and tackle' resolves to one item twice, so it stays a single object."""
    parts, cur = [], []
    for w in words:
        if w == "and":
            parts.append(cur)
            cur = []
        else:
            cur.append(w)
    parts.append(cur)
    objs = []
    for p in parts:
        o = game.resolve(p) if p else None
        if not o:
            return None
        if o not in objs:
            objs.append(o)
    return objs


# ---------- worlds ----------
CONDITIONS = ("has", "flag", "seen")


def validate(world):
    """Return a list of problems with a world file (empty = good). Catches typo'd ids and
    missing fallbacks before a player can hit them."""
    errs = []

    def need(ok, msg):
        if not ok:
            errs.append(msg)

    for key in ("id", "title", "intro", "start", "map", "rooms", "items", "uses", "endings", "verb_defaults"):
        need(key in world, f"missing top-level '{key}'")
    if errs:
        return errs
    rooms, items, endings = world["rooms"], world["items"], world["endings"]

    def cond(c, where):
        for part in str(c).split("|"):
            kind, _, val = part.partition(":")
            if kind not in CONDITIONS:
                errs.append(f"{where}: unknown condition '{part}' (use has:, flag: or seen:)")
            elif kind == "has":
                need(val in items, f"{where}: has:{val} names an unknown item")
            elif kind == "seen":
                need(val in rooms, f"{where}: seen:{val} names an unknown room")
            else:
                need(bool(val), f"{where}: empty flag name")

    def choice(spec, key, where, check):
        if isinstance(spec, list):
            need(bool(spec) and "if" not in spec[-1], f"{where}: the last {key} option must have no 'if' (it's the fallback)")
            for e in spec:
                if "if" in e:
                    cond(e["if"], where)
                need(key in e, f"{where}: an option is missing '{key}'")
                check(e.get(key))
        else:
            check(spec)

    def ending(spec, where):
        choice(spec, "ending", where, lambda v: need(v in endings, f"{where}: unknown ending '{v}'"))

    def text(spec, where):
        choice(spec, "fact", where, lambda v: need(isinstance(v, str) and v, f"{where}: fact must be text"))

    need(world["start"] in rooms, f"start room '{world['start']}' doesn't exist")
    for rid, r in rooms.items():
        need(rid in world["map"], f"room '{rid}' has no map position")
        need("name" in r and "desc" in r, f"room '{rid}' needs a name and desc")
        for d, ex in r.get("exits", {}).items():
            need(d in DIRECTIONS, f"room '{rid}': '{d}' isn't a direction")
            dest = ex.get("to") if isinstance(ex, dict) else ex
            need(dest in rooms, f"room '{rid}': exit {d} leads to unknown room '{dest}'")
            if isinstance(ex, dict) and ex.get("flag"):
                need("blocked" in ex, f"room '{rid}': locked exit {d} needs a 'blocked' message")
        for i in r.get("items", []):
            need(i in items, f"room '{rid}' holds unknown item '{i}'")
    for rid in world["map"]:
        need(rid in rooms, f"map position for unknown room '{rid}'")

    for iid, it in items.items():
        for k in ("name", "aliases", "desc"):
            need(k in it, f"item '{iid}' is missing '{k}'")
        for i in it.get("contains", []):
            need(i in items, f"item '{iid}' contains unknown item '{i}'")
        lines = it.get("talk")
        if lines is not None:
            need(bool(lines) and "if" not in lines[-1], f"item '{iid}': the last talk line must have no 'if'")
            for line in lines:
                need("say" in line, f"item '{iid}': a talk line is missing 'say'")
                if "if" in line:
                    cond(line["if"], f"item '{iid}' talk")
                for i, dest in line.get("reveal", {}).items():
                    need(i in items and dest in rooms, f"item '{iid}' talk reveals '{i}' into '{dest}'")

    for verb in ("light", "unlock", "fill"):
        need(world["verb_defaults"].get(verb) in items, f"verb_defaults.{verb} must name an item")

    for u in world["uses"]:
        where = f"use {u.get('item')} on {u.get('target')}"
        need(u.get("item") in items and u.get("target") in items, f"{where}: unknown item")
        need("fact" in u, f"{where}: missing 'fact'")
        text(u.get("fact"), where)
        if "where" in u:
            need(u["where"] in rooms, f"{where}: unknown room '{u['where']}'")
        if "ending" in u:
            ending(u["ending"], where)
        for r in u.get("refuse", []):
            need("if" in r and "say" in r, f"{where}: refuse entries need 'if' and 'say'")
            cond(r.get("if", ""), where)

    own_verbs = world.get("verbs", {})
    for verb, syns in own_verbs.items():
        need(re.fullmatch(r"[a-z]+", verb) is not None and verb not in BUILTIN_VERBS,
             f"verbs: '{verb}' must be one lowercase word that isn't a built-in verb")
        need(isinstance(syns, list) and all(isinstance(s, str) and re.fullmatch(r"[a-z]+", s) for s in syns),
             f"verbs: synonyms of '{verb}' must be a list of single lowercase words")
        need(any(x.get("verb") == verb for x in world.get("interactions", [])),
             f"verbs: '{verb}' has no interactions, so it would never do anything")
    for v in world.get("variants", []):
        where = f"variant '{v.get('id')}'"
        need(isinstance(v.get("id"), str) and v.get("item") in items, f"{where}: needs an id and a known item")
        opts = v.get("options", [])
        need(len(opts) >= 2, f"{where}: needs at least two options")
        for o in opts:
            at = o.get("at")
            need(at in rooms or (at in items and items[at].get("container")),
                 f"{where}: option '{at}' must be a room or a container item")
    for key, val in world.get("theme", {}).items():
        need(isinstance(val, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", val) is not None,
             f"theme.{key} must be a colour like #f2b34b")
    for m, spec in world.get("modes", {}).items():
        need(m in MODES, f"modes: unknown mode '{m}' (use {', '.join(MODES)})")
        need(set(spec) <= {"deadline", "timers", "hints", "label"}, f"modes.{m}: unknown setting")
    placeholders = set(re.findall(r"\{([a-z_]+)\}", json.dumps(world)))
    need(placeholders <= {v.get("id") for v in world.get("variants", [])},
         f"text uses placeholders with no variant: {', '.join(sorted(placeholders - {v.get('id') for v in world.get('variants', [])}))}")

    for x in world.get("interactions", []):
        where = f"{x.get('verb')} {x.get('target')}"
        need(x.get("verb") in ("help", "board") or x.get("verb") in own_verbs,
             f"{where}: interaction verb must be help, board, or one of the world's own verbs")
        need(x.get("target") in items, f"{where}: unknown target")
        need("fact" in x, f"{where}: missing 'fact'")
        text(x.get("fact"), where)
        choice(x.get("cost", 0), "cost", where, lambda v: need(isinstance(v, int) and v >= 0, f"{where}: bad cost"))
        for i, dest in x.get("move", {}).items():
            need(i in items and dest in rooms, f"{where}: moves '{i}' into '{dest}'")
        if "move_player" in x:
            need(x["move_player"] in rooms, f"{where}: unknown room '{x['move_player']}'")
        if "ending" in x:
            ending(x["ending"], where)

    clock = world.get("clock")
    if clock:
        need(isinstance(clock.get("deadline"), int), "clock needs a numeric deadline")
        need(clock.get("deadline_ending") in endings, "clock.deadline_ending names an unknown ending")
    for h in world.get("hints", []):
        cond(h.get("done", ""), "hint")
    for flag, t in world.get("timers", {}).items():
        need(isinstance(t.get("turns"), int) and "out_fact" in t, f"timer '{flag}' needs turns and out_fact")
    for eid, e in endings.items():
        for k in ("title", "text", "tone"):
            need(k in e, f"ending '{eid}' is missing '{k}'")
    return errs


def load_world(path):
    """One world file, with its id (the file name). Raises with every problem listed."""
    path = Path(path)
    world = json.loads(path.read_text(encoding="utf-8"))
    world["id"] = path.stem
    errs = validate(world)
    if errs:
        raise ValueError(f"{path.name} has problems:\n  " + "\n  ".join(errs))
    return world


def load_worlds(*folders, only=None):
    """Every *.json in the folders, keyed by file name; a later folder overrides an earlier one.
    `only` limits which ids are kept. Raises if any world has problems."""
    worlds = {}
    for folder in folders:
        for path in sorted(Path(folder).glob("*.json")):
            if only and path.stem not in only:
                continue
            worlds[path.stem] = load_world(path)
    if not worlds:
        raise ValueError(f"no worlds found in {', '.join(map(str, folders))}")
    return worlds
