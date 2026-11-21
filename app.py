"""Web server: game sessions + the tiny-LM 'director'.

The model has three narrow jobs, each kept small enough for a ~0.5B model:
  1. PARSE   - when the rule parser can't understand the player, the model maps
               their words to a command. Output is grammar-constrained (JSON schema
               enums), then checked against the player's actual words.
  2. NARRATE - rewrite the engine's plain facts as short atmospheric prose.
               Output is validated; anything off falls back to the plain facts.
  3. NUDGE   - if the player stalls, the director surfaces the current hint.
If the model is unavailable, the game still works using the plain facts.
"""
import difflib
import json
import os
import re
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import Game, parse_rule

LLM_URL = os.getenv("LLM_URL", "http://llm:8080").rstrip("/")
NARRATE = os.getenv("NARRATE", "1") == "1"
STUCK_TURNS = int(os.getenv("STUCK_TURNS", "6"))
HERE = Path(__file__).parent
WORLD = json.loads((HERE / "world.json").read_text(encoding="utf-8"))

app = FastAPI()
sessions: dict[str, Game] = {}
client = httpx.AsyncClient(timeout=45)


# ---------------- LLM helpers ----------------
async def chat(messages, max_tokens, temperature, schema=None):
    if not LLM_URL:  # no model on this box (e.g. the GL.iNet profile): callers fall back to plain text
        raise RuntimeError("no LLM configured")
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "repeat_penalty": 1.15, "cache_prompt": True}
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "command", "schema": schema}}
    r = await client.post(f"{LLM_URL}/v1/chat/completions", json=body)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ---------- 1. PARSE ----------
# Division of labour: code finds WHICH things/places the player named (fuzzy string
# matching is exact and cheap); the model only decides WHAT they want to do (the verb),
# which is a small classification task a 0.5B model handles well.
MODEL_VERBS = ["unknown", "go", "take", "drop", "examine", "open", "use", "light", "unlock", "fill",
               "talk", "board", "look", "inventory", "hint", "help"]
PARSE_SYSTEM = (
    "You classify a player's text-adventure command. Pick the single verb that best matches what "
    "the player wants to do. Use go for moving somewhere, and give the exit direction that leads there. "
    "Use examine for looking at or reading something. Use help for helping, rescuing or carrying a person. "
    "Use talk for speaking to someone. Use board for getting into a boat or escaping by sea. "
    "Use \"unknown\" if the command is not a game action."
)
PARSE_EXAMPLES = [
    ("Player: could you snag that rope\nMentioned: rope\nExits: west -> Old Mill", {"verb": "take", "direction": "none"}),
    ("Player: pry the crate open with my knife\nMentioned: crate, knife\nExits: north -> Road", {"verb": "use", "direction": "none"}),
    ("Player: wander over to the mill\nMentioned: (nothing)\nExits: west -> Old Mill, down -> Cellar", {"verb": "go", "direction": "west"}),
    ("Player: what's written on the sign\nMentioned: sign\nExits: east -> Garden", {"verb": "examine", "direction": "none"}),
    ("Player: set fire to the torch\nMentioned: torch\nExits: east -> Garden", {"verb": "light", "direction": "none"}),
    ("Player: haul the old miner out of the pit\nMentioned: miner\nExits: east -> Garden", {"verb": "help", "direction": "none"}),
    ("Player: ask the guard what happened\nMentioned: guard\nExits: east -> Garden", {"verb": "talk", "direction": "none"}),
    ("Player: paddle off in the canoe\nMentioned: canoe\nExits: east -> Garden", {"verb": "board", "direction": "none"}),
    ("Player: sing a song about cheese\nMentioned: (nothing)\nExits: east -> Garden", {"verb": "unknown", "direction": "none"}),
]
NEEDS_OBJECT = {"take", "drop", "examine", "open", "use", "light", "unlock", "fill"}
OPTIONAL_OBJECT = {"help", "talk", "board"}
CUE_VERBS = {"open": "open", "read": "examine", "alight": "light", "ignite": "light", "kindle": "light",
             "unlock": "unlock", "refill": "fill", "talk": "talk", "speak": "talk", "ask": "talk",
             "rescue": "help", "save": "help", "row": "board", "paddle": "board", "flee": "board", "escape": "board"}
FILLER = {"the", "and", "you", "your", "with", "into", "onto", "from", "that", "this", "over", "back", "there", "toward", "towards"}


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
    """'go to the lighthouse' -> the exit whose destination name the player used."""
    said = words_of(text)
    hits = [d for d, name in game.exit_names().items() if any(fuzzy_in(p, said) for p in words_of(name))]
    return hits[0] if len(hits) == 1 else None


def build_command(verb, nouns, direction, game):
    defaults = game.world["verb_defaults"]
    if verb == "go":
        return {"verb": "go", "direction": direction} if direction else {"verb": "unknown"}
    if verb in OPTIONAL_OBJECT:
        return {"verb": verb, "object": nouns[0] if nouns else None}
    if verb not in NEEDS_OBJECT:
        return {"verb": verb}
    if not nouns:
        return {"verb": "unknown"}
    if verb == "light":
        rest = [n for n in nouns if n != defaults["light"]] or nouns
        return {"verb": "light", "object": rest[0]}
    if verb in ("unlock", "fill"):
        rest = [n for n in nouns if n != defaults[verb]] or nouns
        return {"verb": verb, "object": None, "target": rest[0]}
    if verb == "use":
        n0 = nouns[0]
        if len(nouns) == 1 and n0 not in game.inv and game.items[n0].get("portable"):
            return {"verb": "take", "object": n0}  # "pocket the key", "scoop up the oil"
        if len(nouns) == 1 and (game.items[nouns[0]].get("container") or game.items[nouns[0]].get("locked_flag")):
            return {"verb": "open", "object": nouns[0]}
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


async def llm_parse(text, game):
    nouns = mentions(text, game)
    exits = game.exit_names()
    direction = place_direction(text, game)
    schema = {
        "type": "object",
        "properties": {"verb": {"enum": MODEL_VERBS}, "direction": {"enum": ["none"] + list(exits)}},
        "required": ["verb", "direction"],
    }
    msgs = [{"role": "system", "content": PARSE_SYSTEM}]
    for q, a in PARSE_EXAMPLES:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps(a)}]
    named = ", ".join(game.name(n) for n in nouns) or "(nothing)"
    ex = ", ".join(f"{d} -> {n}" for d, n in exits.items())
    msgs.append({"role": "user", "content": f"Player: {text}\nMentioned: {named}\nExits: {ex}"})
    raw = json.loads(await chat(msgs, 30, 0.0, schema))
    raw["mentioned"] = nouns
    cue = next((CUE_VERBS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in CUE_VERBS), None)
    if cue and (nouns or cue == "board") and raw["verb"] not in ("go", cue):
        raw["verb_cue"] = cue  # an unambiguous verb word beats the model's guess
        raw["verb"] = cue
    if raw["verb"] == "go":
        direction = direction or (raw["direction"] if raw["direction"] != "none" else None)
    return build_command(raw["verb"], nouns, direction, game), raw


# ---------- 2. NARRATE ----------
NARRATE_SYSTEM = (
    "You are the narrator of a text adventure set in a lighthouse during a storm. "
    "Rewrite the FACTS as 1 to 3 short sentences. Always say 'you', never 'I'. Present tense. "
    "Keep every fact, including every item and every exit. "
    "Never add objects, people, or events that are not in the FACTS. Do not give advice."
)
NARRATE_EXAMPLES = [
    ("- You go west.\n- Location: Old Mill.\n- A dusty mill with a broken waterwheel.\n- Items here: sack of flour.\n- Exits: east, up.",
     "You make your way west into the old mill, where dust hangs around the broken waterwheel. A sack of flour slumps against the wall. Ways lead east and up."),
    ("- You take the rope.",
     "You coil the rope and sling it over your shoulder."),
]
# words that only exist in the examples: if they show up in output, the model is copying
EXAMPLE_ONLY = {"mill", "waterwheel", "flour", "sack", "rope", "coil", "shoulder", "dust"}
# The narrator may not claim an action the engine didn't report ("you pocket the key" when you didn't).
CLAIMS = [
    {"take", "takes", "took", "grab", "grabs", "pick", "picks", "pocket", "pockets", "retrieve", "retrieves", "snatch"},
    {"light", "lights", "lit", "ignite", "ignites", "strike", "strikes", "kindle"},
    {"unlock", "unlocks", "unlocked"},
    {"pour", "pours", "fill", "fills"},
]
# first person, or a gendered stranger: the player is "you" and the keeper is "they"
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|me|my|myself|we|our|he|his|him|she|her)\b", re.I)


def tidy(text):
    text = text.strip().strip('"').strip()
    text = re.sub(r"^(Narration|Narrator)\s*:\s*", "", text, flags=re.I)
    end = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    return text[: end + 1] if end > 20 else text


def valid_narration(out, facts):
    if not out or FIRST_PERSON.search(out):
        return False
    if len(out) > 2.2 * len(" ".join(facts)) + 120:
        return False
    sents = [s.strip().lower() for s in re.split(r"[.!?]", out) if s.strip()]
    if len(sents) != len(set(sents)) or len(sents) > 5:
        return False
    low = out.lower()
    fact_words = set(re.findall(r"[a-z]+", " ".join(facts).lower()))
    out_words = set(re.findall(r"[a-z]+", low))
    if (out_words & EXAMPLE_ONLY) - fact_words:
        return False
    if any(out_words & group and not fact_words & group for group in CLAIMS):
        return False
    for f in facts:
        m = re.match(r"You go (\w+)\.$", f)  # the direction actually taken must survive
        if m and m.group(1) not in low:
            return False
    for f in facts:  # every listed item must survive
        m = re.match(r"(Items here: |Inside: |The )(.*?)( is here)?\.$", f)
        if m and (m.group(1) != "The " or m.group(3)):
            for name in m.group(2).split(", "):
                if name.split()[-1].lower() not in low:
                    return False
    return True


async def llm_narrate(facts):
    msgs = [{"role": "system", "content": NARRATE_SYSTEM}]
    for q, a in NARRATE_EXAMPLES:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": "\n".join(f"- {f}" for f in facts)})
    for temp in (0.3, 0.0):  # one retry, cooler
        out = tidy(await chat(msgs, 110, temp))
        if valid_narration(out, facts):
            return out
    return None


# ---------------- API ----------------
class Turn(BaseModel):
    session: str
    input: str


def plain(facts):
    out = []
    for f in facts:
        if f.startswith("Location:") or f.startswith("Exits:"):
            continue  # shown in the status bar
        out.append(re.sub(r"^Items here: (.*)\.$", r"You see: \1.", f))
    return " ".join(out)


async def narrate(facts, use_model=True):
    if use_model and NARRATE:
        try:
            out = await llm_narrate(facts)
            if out:
                return out, True
        except Exception:
            pass
    return plain(facts), False


@app.post("/api/new")
async def new_game():
    sid = uuid.uuid4().hex
    game = sessions[sid] = Game(WORLD)
    text, narrated = await narrate(game.room_facts())
    return {"session": sid, "title": WORLD["title"], "intro": WORLD["intro"], "text": text,
            "facts": game.room_facts(), "narrated": narrated, "status": game.status(),
            "map": MAP, "endings_total": len(WORLD["endings"])}


def build_map():
    """The fixed map, as nodes + edges, for the UI to draw."""
    nodes = [{"id": rid, "name": WORLD["rooms"][rid]["name"], "x": x, "y": y}
             for rid, (x, y) in WORLD["map"].items()]
    edges, seen = [], set()
    for rid, room in WORLD["rooms"].items():
        for d, ex in room["exits"].items():
            dest = ex["to"] if isinstance(ex, dict) else ex
            key = tuple(sorted((rid, dest)))
            if key not in seen:
                seen.add(key)
                edges.append({"a": rid, "b": dest, "vertical": d in ("up", "down"),
                              "lock": ex.get("flag") if isinstance(ex, dict) else None})
    return {"nodes": nodes, "edges": edges}


MAP = build_map()


@app.post("/api/turn")
async def turn(t: Turn):
    game = sessions.get(t.session)
    if not game:
        raise HTTPException(404, "Unknown session. Start a new game.")
    text = t.input.strip()[:200]
    if game.over:
        return {"text": "This story is over. Start a new game to try a different path.",
                "facts": [], "parsed_by": "none", "narrated": False, "status": game.status()}

    raw = None
    cmd, parsed_by = parse_rule(text, game), "rules"
    if cmd is None:
        try:
            (cmd, raw), parsed_by = await llm_parse(text, game), "model"
        except Exception:
            cmd, parsed_by = {"verb": "unknown"}, "none"

    facts = game.do(cmd)
    director = None
    if game.since_progress >= STUCK_TURNS and not game.over:
        director = game.current_hint()
        game.since_progress = 0

    # Authored text (clock beats, endings) is shown verbatim; only the action itself is narrated,
    # and only when it's a story beat: moving, discovering, succeeding. Dialogue stays verbatim.
    authored = set(game.beats_now) | {game.ending_text}
    action = [f for f in facts if f not in authored]
    story_beat = (game._progress or any(f.startswith("Location:") for f in action)) and cmd.get("verb") != "talk"
    out, narrated = await narrate(action, use_model=story_beat) if action else ("", False)
    return {"text": out, "facts": facts, "command": cmd, "model_raw": raw, "parsed_by": parsed_by,
            "narrated": narrated, "director": director, "beats": game.beats_now,
            "ending_text": game.ending_text, "status": game.status()}


@app.get("/api/health")
async def health():
    try:
        if not LLM_URL:
            return {"llm": "none", "model": None}
        r = await client.get(f"{LLM_URL}/v1/models", timeout=3)
        r.raise_for_status()
        model = r.json()["data"][0]["id"]
        return {"llm": "ready", "model": model}
    except Exception:
        return {"llm": "offline", "model": None}


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")
