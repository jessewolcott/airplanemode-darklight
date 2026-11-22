"""Turning player text into engine commands, without the web server.

Three layers, tried in order by the server:
  1. engine.parse_rule  - exact adventure phrasing ("take key", "n"). Instant.
  2. the model          - classifies the verb (app.py); code here finds the nouns and places.
  3. fallback_parse     - no model at all: keyword verbs plus the same fuzzy noun and place
                          matching. Used on boxes without a model, or when it's down.
"""
import difflib
import re

from engine import to_dir

NEEDS_OBJECT = {"take", "drop", "examine", "open", "use", "light", "unlock", "fill"}
OPTIONAL_OBJECT = {"help", "talk", "board"}
# Words that mean one verb wherever they appear in a sentence.
CUE_VERBS = {"open": "open", "read": "examine", "alight": "light", "ignite": "light", "kindle": "light",
             "unlock": "unlock", "refill": "fill", "talk": "talk", "speak": "talk", "ask": "talk",
             "rescue": "help", "save": "help", "row": "board", "paddle": "board", "flee": "board", "escape": "board",
             "ride": "board", "sled": "board", "toboggan": "board"}
# Extra verb words the fallback understands (the rule parser only looks at the first word).
FALLBACK_VERBS = {
    "grab": "take", "snag": "take", "pocket": "take", "fetch": "take", "nab": "take",
    "drop": "drop", "leave": "drop", "ditch": "drop",
    "look": "examine", "study": "examine", "view": "examine", "observe": "examine", "inspect": "examine",
    "examine": "examine", "check": "examine", "search": "examine",
    "go": "go", "walk": "go", "head": "go", "run": "go", "climb": "go", "enter": "go", "wander": "go",
    "travel": "go", "return": "go", "stroll": "go", "hurry": "go", "descend": "go", "ascend": "go",
    "light": "light", "burn": "light", "strike": "light",
    "use": "use", "pour": "use", "put": "use", "stick": "use", "chop": "use", "hack": "use", "cut": "use",
    "fill": "fill", "fuel": "fill", "stuff": "fill", "load": "fill", "pack": "fill",
    "help": "help", "carry": "help", "haul": "help", "lift": "help", "drag": "help",
    "board": "board", "sail": "board", "launch": "board",
    "take": "take", "get": "take", "pick": "take", "collect": "take",
    "inventory": "inventory", "carrying": "inventory", "holding": "inventory", "hint": "hint",
    "gather": "take", "try": "use", "top": "fill",
    "tie": "use", "knot": "use", "attach": "use", "fasten": "use", "hook": "use", "prop": "use", "lean": "use",
}
# Two-word phrases the single words above would get wrong.
PHRASES = [({"set", "fire"}, "light"), ({"on", "fire"}, "light"), ({"top", "up"}, "fill"),
           ({"cut", "steps"}, "use"), ({"hack", "steps"}, "use")]
FILLER = {"the", "and", "you", "your", "with", "into", "onto", "from", "that", "this", "over", "back", "there", "toward", "towards"}
QUESTION_STARTS = {"what", "whats", "who", "where", "why", "how"}


def words_of(text):
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in FILLER]


def fuzzy_in(word, said):
    return any(w == word or difflib.SequenceMatcher(None, w, word).ratio() >= 0.8 for w in said)


def mentions(text, game):
    """Items the player named, in the order they named them. Visible things win ties."""
    said = words_of(text)
    found = []
    order = game.visible() + [i for i in game.items if i not in game.visible()]
    for iid in order:
        for alias in game.items[iid]["aliases"]:
            if all(fuzzy_in(p, said) for p in alias.split()):
                first = min(i for i, w in enumerate(said) if any(fuzzy_in(p, [w]) for p in alias.split()))
                found.append((first, iid))
                break
    found.sort()
    return [iid for _, iid in found]


def place_direction(text, game):
    """'go to the lighthouse' -> the exit whose destination the player named (by name or alias)."""
    said = words_of(text)
    hits = []
    for d, ex in game.r()["exits"].items():
        room = game.rooms[ex["to"] if isinstance(ex, dict) else ex]
        names = [room["name"]] + room.get("aliases", [])
        if any(fuzzy_in(p, said) for n in names for p in words_of(n)):
            hits.append(d)
    return hits[0] if len(hits) == 1 else None


def build_command(verb, nouns, direction, game):
    """A verb (from the model or keywords) plus the nouns code found -> an engine command."""
    defaults = game.world["verb_defaults"]
    if verb == "go":
        return {"verb": "go", "direction": direction} if direction else {"verb": "unknown"}
    if verb in OPTIONAL_OBJECT or verb in game.world.get("verbs", {}):
        return {"verb": verb, "object": nouns[0] if nouns else None}
    if verb not in NEEDS_OBJECT:
        return {"verb": verb}
    if not nouns:
        return {"verb": "unknown"}
    if verb in ("take", "drop") and len(nouns) > 1:  # "grab the matches and the lantern"
        if verb == "take":
            keep = [n for n in nouns if game.items[n].get("portable") and n not in game.inv]
        else:
            keep = [n for n in nouns if n in game.inv]
        if len(keep) > 1:
            return {"verb": verb, "objects": keep}
        return {"verb": verb, "object": keep[0] if keep else nouns[0]}
    if verb == "light":
        rest = [n for n in nouns if n != defaults["light"]] or nouns
        return {"verb": "light", "object": rest[0]}
    if verb in ("unlock", "fill"):
        rest = [n for n in nouns if n != defaults[verb]] or nouns
        return {"verb": verb, "object": None, "target": rest[0]}
    if verb == "use":
        n0 = nouns[0]
        if len(nouns) == 1 and n0 not in game.inv:
            tool = next((u["item"] for u in game.world["uses"] if u["target"] == n0 and u["item"] in game.inv), None)
            if tool:
                return {"verb": "use", "object": tool, "target": n0}  # "cut steps in the ice": with what you hold
        if len(nouns) == 1 and n0 not in game.inv and game.items[n0].get("portable"):
            return {"verb": "take", "object": n0}  # "pocket the key", "scoop up the oil"
        if len(nouns) == 1 and (game.items[n0].get("container") or game.items[n0].get("locked_flag")):
            return {"verb": "open", "object": n0}
        # pick the (object, target) pair the world actually has a rule for, in either order
        for a in nouns:
            for b in nouns:
                if a != b and any(u["item"] == a and u["target"] == b for u in game.world["uses"]):
                    return {"verb": "use", "object": a, "target": b}
        held = [n for n in nouns if n in game.inv]
        obj = held[0] if held else nouns[0]
        others = [n for n in nouns if n != obj]
        return {"verb": "use", "object": obj, "target": others[0] if others else None}
    return {"verb": verb, "object": nouns[0]}


def cue_verb(text, game):
    """An unambiguous verb word anywhere in the text (the world's own verbs count)."""
    own = {w: v for v, syns in game.world.get("verbs", {}).items() for w in [v] + list(syns)}
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in own:
            return own[w]
        if w in CUE_VERBS:
            return CUE_VERBS[w]
    return None


def fallback_parse(text, game):
    """Understand free text with no model: keyword verbs plus fuzzy nouns and places.
    Errs on the side of harmless commands: a bare noun is examined, not used."""
    words = re.findall(r"[a-z]+", text.lower())
    if not words:
        return {"verb": "unknown"}
    nouns = mentions(text, game)
    seen_now = [n for n in nouns if n in game.visible()]
    nouns = seen_now or nouns          # people mean what's in front of them ("pull" isn't the hidden pulley)
    direction = place_direction(text, game)
    dir_word = next((to_dir(w) for w in words if to_dir(w) and w not in ("u", "d", "e", "w", "n", "s")), None)
    verb = next((v for need, v in PHRASES if need <= set(words)), None) or cue_verb(text, game)
    if verb is None:
        own = game.verb_words()
        for i, w in enumerate(words):
            if w in FALLBACK_VERBS:
                verb = FALLBACK_VERBS[w]
                break
            if w in own and len(w) >= 3:   # "i", "x", "l" only count alone, and the rule parser handles that
                verb = own[w]
                break
    if words[0] == "where" and verb in (None, "go"):
        return {"verb": "look"}            # "where can I go?"
    if direction is None and (verb == "go" or not nouns):
        direction = dir_word               # "up" in "top up the lamp" isn't a direction
    if verb == "go" and not direction and "climb" in words and "up" in game.exits():
        direction = "up"
    people = [n for n in nouns if game.items[n].get("person")]
    if verb in ("use", "take") and people and any(
            x["verb"] == "help" and x["target"] in people for x in game.world.get("interactions", [])):
        verb = "help"                      # "use the pulley to lift the keeper"
    if verb == "help" and not nouns:
        here = [i for i in game.visible() if game.items[i].get("person")]
        if len(here) == 1:
            nouns = here                   # "rescue them"
    if verb in ("help", "take") and any(game.items[n].get("person") for n in nouns):
        verb = "help"                      # "carry the keeper", "get the climber out"
    if verb == "help" and not any(game.items[n].get("person") for n in nouns) and nouns:
        verb = "take"                      # "carry the logs" means take them
    if verb == "help":
        nouns = [n for n in nouns if game.items[n].get("person")] + [n for n in nouns if not game.items[n].get("person")]
    if verb == "examine" and not nouns:
        verb = "look"
    if verb is None:
        if words[0] in QUESTION_STARTS and nouns:
            verb = "examine"               # "what's in the drawer?"
        elif direction:
            verb = "go"
        elif nouns:
            person = next((n for n in nouns if game.items[n].get("person")), None)
            return {"verb": "talk", "object": person} if person and len(nouns) == 1 else {"verb": "examine", "object": nouns[0]}
        else:
            return {"verb": "unknown"}
    if verb == "go" and not direction:
        return {"verb": "go"}               # the engine asks "Go where?"
    return build_command(verb, nouns, direction, game)
