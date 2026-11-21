"""Deterministic game engine.

The engine owns ALL game state and rules: the fixed map, the clock, the branching
flags and the endings. The language model never decides what is true; it only
(a) helps turn free text into a command and (b) rewrites the engine's facts as prose.
"""
import copy
import re

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
    "board": "board", "row": "board", "sail": "board", "flee": "board", "escape": "board",
    "i": "inventory", "inv": "inventory", "inventory": "inventory",
    "hint": "hint",
}
STOP = {"the", "a", "an", "my", "please", "some", "then", "at", "to", "around", "this", "that", "away"}
SPLIT = {"with", "on", "in", "into", "onto", "using"}
FREE_VERBS = ("help", "hint", "inventory", "look")  # don't tick the clock


def to_dir(word):
    if word in DIRECTIONS:
        return word
    return DIR_SYN.get(word)


class Game:
    def __init__(self, world):
        self.world = world
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
        self.ending = None
        self.ending_text = None
        self.beats_now = []
        self._progress = False

    @property
    def over(self):
        return self.ending is not None

    # ---------- queries ----------
    def r(self):
        return self.rooms[self.room]

    def name(self, iid):
        return self.items[iid]["name"]

    def is_dark(self):
        return bool(self.r().get("dark")) and not ("lantern_lit" in self.flags and "lantern" in self.inv)

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

    def current_hint(self):
        for h in self.world["hints"]:
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
            "ending": {"id": self.ending, "title": ending["title"], "tone": ending["tone"]} if ending else None,
        }

    # ---------- turn ----------
    def do(self, cmd):
        verb = cmd.get("verb") or "unknown"
        clean = {k: (None if v in (None, "", "none") else v) for k, v in cmd.items()}
        if verb == "help" and clean.get("object"):
            verb = "help_npc"
        self._progress = False
        self.beats_now, self.ending_text = [], None  # authored text, shown verbatim
        before = self.turns
        if verb not in FREE_VERBS:
            self.turns += 1
        handler = getattr(self, "v_" + verb, None)
        facts = handler(clean) if handler else ["You're not sure how to do that."]
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
        """Advance the story clock: fixed beats at fixed times, and a deadline."""
        clock = self.world.get("clock")
        if not clock or self.turns == before:
            return []
        facts = [b["fact"] for b in clock["beats"] if before < b["turn"] <= self.turns]
        self.beats_now = list(facts)
        if self.turns >= clock["deadline"]:
            facts.append(self.finish(clock["deadline_ending"]))
        return facts

    def finish(self, spec):
        """spec is an ending id, or a list of {if, ending} checked in order."""
        if isinstance(spec, list):
            spec = next(e["ending"] for e in spec if "if" not in e or self.check(e["if"]))
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
                "use <thing> on <thing>, light <thing>, talk to <someone>, help <someone>, inventory, hint. "
                "Plain English usually works too. Time matters."]

    def v_hint(self, c):
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
        if not o or o not in self.visible():
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
        if not o or o not in self.inv:
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
            return ["There's no one here to talk to." if not self.is_dark() else "You call out into the dark. Someone groans in reply."]
        lines = self.items[o].get("talk")
        if not lines:
            return [f"The {self.name(o)} has nothing to say."]
        line = next(l for l in lines if "if" not in l or self.check(l["if"]))
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
        if rule.get("set"):
            self.flags.add(rule["set"])
        self.turns += rule.get("cost", 0)
        for iid, dest in rule.get("move", {}).items():
            for room in self.rooms.values():
                if iid in room["items"]:
                    room["items"].remove(iid)
            self.rooms[dest]["items"].append(iid)
        facts = [rule["fact"]]
        if rule.get("move_player"):
            self.room = rule["move_player"]
            self.visited.add(self.room)
            facts += self.room_facts()
        if rule.get("ending"):
            facts.append(self.finish(rule["ending"]))
        self.progress()
        return facts

    # light / unlock / fill are shortcuts that become "use"
    def _shortcut(self, c, verb):
        c = dict(c)
        default = self.world["verb_defaults"][verb]
        if verb == "light":
            c["target"] = c.get("target") or c.get("object")
            c["object"] = default
        else:
            c["object"] = c.get("object") or default
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
        self.flags.add(rule["set"])
        if rule.get("consume"):
            self.inv.remove(o)
        self.progress()
        facts = [rule["fact"]]
        if rule.get("ending"):
            facts.append(self.finish(rule["ending"]))
        return facts


def parse_rule(text, game):
    """Fast, exact parser for normal adventure-game phrasing. Returns None if unsure."""
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP]
    if not words:
        return None
    if len(words) == 1 and to_dir(words[0]):
        return {"verb": "go", "direction": to_dir(words[0])}
    verb = VERB_SYN.get(words[0])
    rest = words[1:]
    if verb is None:
        return None
    if verb == "look":
        if not rest:
            return {"verb": "look"}
        verb = "examine"
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
    if verb in ("take", "drop", "examine", "open"):
        obj = game.resolve(rest)
        return {"verb": verb, "object": obj} if obj else None

    # two-noun verbs: split around a preposition
    split_at = next((i for i, w in enumerate(rest) if w in SPLIT), None)
    before = rest if split_at is None else rest[:split_at]
    after = [] if split_at is None else rest[split_at + 1:]
    a, b = game.resolve(before), game.resolve(after)
    if verb == "use":
        if not a:
            return None
        return {"verb": "use", "object": a, "target": b}
    if verb == "light":  # light X [with Y]
        if not a:
            return None
        return {"verb": "light", "object": a}
    if verb in ("unlock", "fill"):  # unlock/fill X [with Y]
        if not a:
            return None
        return {"verb": "use" if b else verb, "object": b, "target": a}
    return None
