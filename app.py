"""Web server: game sessions + the tiny-LM 'director'.

The model has narrow jobs, each kept small enough for a ~0.5B model:
  1. PARSE   - when the rule parser can't understand the player, the model maps
               their words to a command. Output is grammar-constrained (JSON schema
               enums), then checked against the player's actual words.
  2. NARRATE - rewrite the engine's plain facts as short atmospheric prose.
               Output is validated; anything off falls back to the plain facts.
  3. VOICE   - rephrase a character's authored line in their voice. Guarded so it
               can't drop or invent details; otherwise the line is shown as written.
  4. NUDGE   - if the player stalls, the director surfaces the current hint.
If the model is unavailable, the game still works using the plain facts.
"""
import asyncio
import json
import os
import re
import secrets
import time
import uuid
from collections import Counter, OrderedDict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import solver
from engine import MODES, Game, load_worlds, modes_for, parse_rule, pick_choices
from party import Party, new_code
from parsing import FILLER, build_command, cue_verb, fallback_parse, mentions, place_direction  # noqa: F401  (tests use these)
from store import Store

LLM_URL = os.getenv("LLM_URL", "http://llm:8080").rstrip("/")
NARRATE = os.getenv("NARRATE", "1") == "1"
STUCK_TURNS = int(os.getenv("STUCK_TURNS", "6"))
SESSION_TTL = int(os.getenv("SESSION_TTL", "7200"))    # seconds idle before a game is dropped
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))   # oldest games are dropped past this
UNDO_DEPTH = 30
HERE = Path(__file__).parent
# Games are data: the bundled worlds/, plus any folder mounted at WORLDS_DIR (a file there with the
# same name replaces the bundled one). GAMES limits which ones this server offers.
WORLD_DIRS = [HERE / "worlds"] + ([Path(os.environ["WORLDS_DIR"])] if os.getenv("WORLDS_DIR") else [])
GAMES = {g.strip() for g in os.getenv("GAMES", "").split(",") if g.strip()} or None
WORLDS = load_worlds(*[d for d in WORLD_DIRS if d.is_dir()], only=GAMES)   # refuses to start if a world is broken
DEFAULT_WORLD = os.getenv("WORLD", "darklight")
if DEFAULT_WORLD not in WORLDS:
    DEFAULT_WORLD = next(iter(WORLDS))
DATA_DIR = os.getenv("DATA_DIR", "")                  # leaderboard + save codes; empty = memory only (set /data with a volume)
AUTHOR_TOOLS = os.getenv("AUTHOR_TOOLS", "0") == "1"  # the solver report page: for authors, off on the box
PARTY_SECONDS = int(os.getenv("PARTY_SECONDS", "20"))

app = FastAPI()
client = httpx.AsyncClient(timeout=45)
store = Store(DATA_DIR)

# How often the model's work is used vs. thrown out by the guards. See /api/stats.
STATS = {"parse": Counter(), "narrate": Counter(), "narrate_rejected": Counter(),
         "voice": Counter(), "voice_rejected": Counter()}
# Recent examples of what the guards threw out, and what nobody could parse: tune prompts from these.
SAMPLES = {"narrate": deque(maxlen=40), "voice": deque(maxlen=40), "unparsed": deque(maxlen=60)}


# ---------------- sessions ----------------
class Session:
    __slots__ = ("game", "undo", "last", "name", "scored")

    def __init__(self, game, name=None):
        self.game, self.undo, self.last = game, [], time.monotonic()
        self.name, self.scored = name, set()


sessions: "OrderedDict[str, Session]" = OrderedDict()   # least recently used first
parties: "dict[str, Party]" = {}


def prune(now=None):
    now = time.monotonic() if now is None else now
    while sessions:
        sid, s = next(iter(sessions.items()))
        if now - s.last <= SESSION_TTL and len(sessions) <= MAX_SESSIONS:
            break
        del sessions[sid]
    for code in [c for c, p in parties.items() if p.session_id not in sessions]:
        del parties[code]


def open_session(game, name=None):
    prune()
    sid = uuid.uuid4().hex
    sessions[sid] = Session(game, name)
    if len(sessions) > MAX_SESSIONS:
        prune()
    return sid


def get_session(sid):
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "Unknown session. Start a new game.")
    s.last = time.monotonic()
    sessions.move_to_end(sid)
    return s


# ---------------- LLM helpers ----------------
async def chat(messages, max_tokens, temperature, schema=None):
    if not LLM_URL:  # no model configured (standalone runs without one): callers fall back to plain text
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
    "Use talk for speaking to someone. Use board for getting into a boat or onto a sled, or escaping. "
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
async def llm_parse(text, game):
    nouns = mentions(text, game)
    exits = game.exit_names()
    direction = place_direction(text, game)
    own = game.world.get("verbs", {})
    schema = {
        "type": "object",
        "properties": {"verb": {"enum": MODEL_VERBS + list(own)}, "direction": {"enum": ["none"] + list(exits)}},
        "required": ["verb", "direction"],
    }
    system = PARSE_SYSTEM
    if own:
        system += " This game also has these verbs: " + "; ".join(
            f"{v} ({', '.join(syns)})" if syns else v for v, syns in own.items()) + "."
    msgs = [{"role": "system", "content": system}]
    for q, a in PARSE_EXAMPLES:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps(a)}]
    named = ", ".join(game.name(n) for n in nouns) or "(nothing)"
    ex = ", ".join(f"{d} -> {n}" for d, n in exits.items())
    msgs.append({"role": "user", "content": f"Player: {text}\nMentioned: {named}\nExits: {ex}"})
    raw = json.loads(await chat(msgs, 30, 0.0, schema))
    raw["mentioned"] = nouns
    cue = cue_verb(text, game)
    if cue and (nouns or cue == "board") and raw["verb"] not in ("go", cue):
        raw["verb_cue"] = cue  # an unambiguous verb word beats the model's guess
        raw["verb"] = cue
    if raw["verb"] == "go":
        direction = direction or (raw["direction"] if raw["direction"] != "none" else None)
    return build_command(raw["verb"], nouns, direction, game), raw


# ---------- 2. NARRATE ----------
def narrate_system(world):
    return (
        f"You are the narrator of a text adventure set in {world.get('setting', 'a strange place')}. "
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
# first person, or a gendered stranger: the player is "you" and other people are "they"
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|me|my|myself|we|our|he|his|him|she|her)\b", re.I)


def tidy(text):
    text = text.strip().strip('"').strip()
    text = re.sub(r"^(Narration|Narrator)\s*:\s*", "", text, flags=re.I)
    end = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    return text[: end + 1] if end > 20 else text


def narration_problem(out, facts):
    """Why this narration can't be used, or None if it's fine."""
    if not out:
        return "empty"
    if FIRST_PERSON.search(out):
        return "first_person"
    if len(out) > 2.2 * len(" ".join(facts)) + 120:
        return "too_long"
    sents = [s.strip().lower() for s in re.split(r"[.!?]", out) if s.strip()]
    if len(sents) != len(set(sents)) or len(sents) > 5:
        return "repetitive"
    low = out.lower()
    fact_words = set(re.findall(r"[a-z]+", " ".join(facts).lower()))
    out_words = set(re.findall(r"[a-z]+", low))
    if (out_words & EXAMPLE_ONLY) - fact_words:
        return "copied_example"
    if any(out_words & group and not fact_words & group for group in CLAIMS):
        return "claimed_action"
    for f in facts:
        m = re.match(r"You go (\w+)\.$", f)  # the direction actually taken must survive
        if m and m.group(1) not in low:
            return "dropped_direction"
    for f in facts:  # every listed item must survive
        m = re.match(r"(Items here: |Inside: |The )(.*?)( is here)?\.$", f)
        if m and (m.group(1) != "The " or m.group(3)):
            for name in m.group(2).split(", "):
                if name.split()[-1].lower() not in low:
                    return "dropped_item"
    return None


def valid_narration(out, facts):
    return narration_problem(out, facts) is None


async def llm_narrate(facts, world):
    msgs = [{"role": "system", "content": narrate_system(world)}]
    for q, a in NARRATE_EXAMPLES:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": "\n".join(f"- {f}" for f in facts)})
    for temp in (0.3, 0.0):  # one retry, cooler
        out = tidy(await chat(msgs, 110, temp))
        problem = narration_problem(out, facts)
        if not problem:
            return out
        STATS["narrate_rejected"][problem] += 1
        SAMPLES["narrate"].append({"problem": problem, "output": out, "facts": facts})
    return None


# ---------- 3. VOICE ----------
VOICE_EXAMPLE = (
    "Player: what happened here?\nLINE: \"The bridge is out,\" the ferryman says. \"Take the north road.\"",
    "\"Bridge washed away,\" the ferryman mutters. \"You'll want the north road.\"",
)
VOICE_EXAMPLE_ONLY = {"bridge", "ferryman", "washed"}
GENDERED = re.compile(r"\b(he|his|him|himself|she|her|hers|herself)\b", re.I)


def voice_system(world, speaker):
    return (
        f"You voice a character in a text adventure set in {world.get('setting', 'a strange place')}. "
        f"Rewrite the LINE as the {speaker} would say it: dialogue in double quotes, with a few words saying "
        f"who speaks. Keep every detail and instruction in the LINE. Add no new places, objects or information. "
        f"Refer to the {speaker} as 'the {speaker}' or 'they'."
    )


def detail_words(world):
    """Names of things and places in this world. A voiced line must keep exactly the ones it had."""
    names = [a for it in world["items"].values() if not it.get("person") for a in it["aliases"]]
    names += [room["name"] for room in world["rooms"].values()]
    return {w for n in names for w in re.findall(r"[a-z]+", n.lower()) if len(w) >= 3} - FILLER


def voice_problem(out, line, world):
    if not out or '"' not in out:
        return "not_dialogue"
    if GENDERED.search(out):
        return "gendered"
    if len(out) > 1.8 * len(line) + 60:
        return "too_long"
    if len(out) < 0.35 * len(line):
        return "too_short"
    sents = [s.strip().lower() for s in re.split(r"[.!?]", out) if s.strip()]
    if len(sents) != len(set(sents)):
        return "repetitive"
    terms = detail_words(world)
    said = set(re.findall(r"[a-z]+", line.lower()))
    got = set(re.findall(r"[a-z]+", out.lower()))
    if (got & VOICE_EXAMPLE_ONLY) - said:
        return "copied_example"
    if (said & terms) - got:
        return "dropped_detail"
    if (got & terms) - said:
        return "invented_detail"
    return None


async def llm_voice(game, speaker, line, player_text):
    name = game.name(speaker)
    msgs = [{"role": "system", "content": voice_system(game.world, name)},
            {"role": "user", "content": VOICE_EXAMPLE[0]}, {"role": "assistant", "content": VOICE_EXAMPLE[1]},
            {"role": "user", "content": f"Player: {player_text}\nLINE: {line}"}]
    for temp in (0.5, 0.2):
        out = (await chat(msgs, 120, temp)).strip()
        problem = voice_problem(out, line, game.world)
        if not problem:
            return out
        STATS["voice_rejected"][problem] += 1
        SAMPLES["voice"].append({"problem": problem, "output": out, "line": line})
    return None


async def voice(game, speaker, line, player_text):
    if not NARRATE or not LLM_URL:
        return None
    STATS["voice"]["requested"] += 1
    try:
        out = await llm_voice(game, speaker, line, player_text)
    except Exception:
        STATS["voice"]["error"] += 1
        return None
    STATS["voice"]["accepted" if out else "fell_back"] += 1
    return out


# ---------------- API ----------------
class NewGame(BaseModel):
    world: str | None = None
    mode: str | None = None
    name: str | None = None


class Turn(BaseModel):
    session: str
    input: str


class SessionRef(BaseModel):
    session: str


class Resume(BaseModel):
    save: dict
    name: str | None = None


class Named(BaseModel):
    session: str
    name: str


class Join(BaseModel):
    name: str | None = None


class Propose(BaseModel):
    player: str
    text: str


class Vote(BaseModel):
    player: str
    proposal: str


def plain(facts):
    out = []
    for f in facts:
        if f.startswith("Location:") or f.startswith("Exits:"):
            continue  # shown in the status bar
        out.append(re.sub(r"^Items here: (.*)\.$", r"You see: \1.", f))
    return " ".join(out)


async def narrate(facts, world, use_model=True):
    if use_model and NARRATE and LLM_URL:
        STATS["narrate"]["requested"] += 1
        try:
            out = await llm_narrate(facts, world)
            if out:
                STATS["narrate"]["accepted"] += 1
                return out, True
            STATS["narrate"]["fell_back"] += 1
        except Exception:
            STATS["narrate"]["error"] += 1
    return plain(facts), False


def build_map(world):
    """The fixed map, as nodes + edges, for the UI to draw."""
    nodes = [{"id": rid, "name": world["rooms"][rid]["name"], "x": x, "y": y}
             for rid, (x, y) in world["map"].items()]
    edges, seen = [], set()
    for rid, room in world["rooms"].items():
        for d, ex in room["exits"].items():
            dest = ex["to"] if isinstance(ex, dict) else ex
            key = tuple(sorted((rid, dest)))
            if key not in seen:
                seen.add(key)
                edges.append({"a": rid, "b": dest, "vertical": d in ("up", "down"),
                              "lock": ex.get("flag") if isinstance(ex, dict) else None})
    return {"nodes": nodes, "edges": edges}


MAPS = {wid: build_map(w) for wid, w in WORLDS.items()}


def world_info(world):
    """Everything the page needs to draw this world's frame."""
    return {
        "world": world["id"], "title": world["title"], "map": MAPS[world["id"]], "ui": world.get("ui", {}),
        "theme": world.get("theme", {}),
        "modes": [{"id": m, "label": spec["label"]} for m, spec in modes_for(world).items()],
        "endings": [{"id": eid, "title": e["title"], "tone": e["tone"], "hint": e.get("hint", "")}
                    for eid, e in world["endings"].items()],
        "endings_total": len(world["endings"]),
        "persistent": store.persistent,
    }


def clean_name(name):
    name = " ".join(str(name or "").split())[:20]
    return "".join(ch for ch in name if ch.isprintable()) or None


@app.get("/api/worlds")
async def worlds():
    return {"default": DEFAULT_WORLD,
            "worlds": [{"id": wid, "title": w["title"], "blurb": w.get("blurb", "")} for wid, w in WORLDS.items()]}


@app.post("/api/new")
async def new_game(req: NewGame | None = None):
    req = req or NewGame()
    wid = req.world or DEFAULT_WORLD
    if wid not in WORLDS:
        raise HTTPException(404, f"Unknown world '{wid}'.")
    mode = req.mode if req.mode in MODES else "normal"
    world = WORLDS[wid]
    game = Game(world, pick_choices(world, secrets.randbits(32)), mode)   # a fresh layout each game
    sid = open_session(game, clean_name(req.name))
    text, narrated = await narrate(game.room_facts(), game.world)
    return {"session": sid, "intro": game.world["intro"], "text": text, "facts": game.room_facts(),
            "narrated": narrated, "status": game.status(), "save": game.snapshot(), **world_info(world)}


def resume_game(save, name=None):
    world = WORLDS.get(save.get("world")) if isinstance(save, dict) else None
    if not world:
        raise HTTPException(400, "That save is for a game this server doesn't have.")
    try:
        game = Game.restore(world, save)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't load that save: {e}.")
    sid = open_session(game, clean_name(name))
    return {"session": sid, "text": plain(game.room_facts()), "facts": game.room_facts(), "narrated": False,
            "status": game.status(), "save": game.snapshot(), **world_info(world)}


@app.post("/api/resume")
async def resume(req: Resume):
    """Pick up a game from a save the browser kept (the server may have restarted)."""
    return resume_game(req.save, req.name)


@app.post("/api/name")
async def set_name(req: Named):
    s = get_session(req.session)
    s.name = clean_name(req.name)
    return {"name": s.name}


@app.post("/api/savecode")
async def make_save_code(req: SessionRef):
    """A short code that carries this game to another device on the box."""
    s = get_session(req.session)
    return {"code": store.put_save(s.game.snapshot()), "persistent": store.persistent}


@app.get("/api/savecode/{code}")
async def load_save_code(code: str, name: str | None = None):
    save = store.get_save(code)
    if not save:
        raise HTTPException(404, "No game saved under that code (codes last 30 days).")
    return resume_game(save, name)


@app.get("/api/leaderboard")
async def leaderboard(world: str = DEFAULT_WORLD):
    if world not in WORLDS:
        raise HTTPException(404, f"Unknown world '{world}'.")
    return {"world": world, **store.leaderboard(world, ("best",)), "persistent": store.persistent}


def undo_turn(s):
    if not s.undo:
        return {"text": "There's nothing to undo.", "facts": [], "parsed_by": "none", "narrated": False,
                "status": s.game.status(), "save": s.game.snapshot()}
    s.game = Game.restore(s.game.base, s.undo.pop())
    facts = s.game.room_facts()
    return {"text": "You take that back. " + plain(facts), "facts": facts, "parsed_by": "undo", "narrated": False,
            "undone": True, "status": s.game.status(), "save": s.game.snapshot()}


@app.post("/api/undo")
async def undo(req: SessionRef):
    return undo_turn(get_session(req.session))


async def understand(text, game):
    """Rules first, then the model, then keywords with no model. Returns (command, raw model output, by)."""
    cmd = parse_rule(text, game)
    if cmd is not None:
        return cmd, None, "rules"
    if LLM_URL:
        try:
            cmd, raw = await llm_parse(text, game)
            if cmd.get("verb") != "unknown":
                return cmd, raw, "model"
            fb = fallback_parse(text, game)
            return (fb, raw, "fallback") if fb.get("verb") != "unknown" else (cmd, raw, "model")
        except Exception:
            pass
    return fallback_parse(text, game), None, "fallback"


def same_state(a, b):
    return {k: v for k, v in a.items() if k != "since_progress"} == {k: v for k, v in b.items() if k != "since_progress"}


def record_score(s):
    """Scores come from the server's own game, never from the browser."""
    g = s.game
    if not (s.name and g.ending) or g.ending in s.scored:
        return
    s.scored.add(g.ending)
    store.add_score({"name": s.name, "world": g.world["id"], "ending": g.ending,
                     "tone": g.world["endings"][g.ending]["tone"], "turns": g.turns, "mode": g.mode,
                     "at": int(time.time())})


async def run_turn(s, text):
    text = text.strip()[:200]
    if text.lower() in ("undo", "take back", "go back in time"):
        return undo_turn(s)
    game = s.game
    if game.over:
        return {"text": "This story is over. Start a new game to try a different path, or type undo.",
                "facts": [], "parsed_by": "none", "narrated": False, "status": game.status(), "save": game.snapshot()}

    cmd, raw, parsed_by = await understand(text, game)
    STATS["parse"][parsed_by] += 1
    if cmd.get("verb") == "unknown":
        SAMPLES["unparsed"].append({"world": game.world["id"], "room": game.room, "text": text, "by": parsed_by})

    before = game.snapshot()
    facts = game.do(cmd)
    if not same_state(before, game.snapshot()):
        s.undo = (s.undo + [before])[-UNDO_DEPTH:]
    director = None
    if game.since_progress >= STUCK_TURNS and not game.over and game.hints_on():
        director = game.current_hint()
        game.since_progress = 0
    record_score(s)

    # Authored text (clock beats, endings) is shown verbatim; only the action itself is narrated,
    # and only when it's a story beat: moving, discovering, succeeding. Dialogue keeps its authored
    # content, though the model may voice it if the guards accept the result.
    authored = set(game.beats_now) | {game.ending_text}
    action = [f for f in facts if f not in authored]
    voiced = None
    if game.said and game.said[1] in action:
        voiced = await voice(game, game.said[0], game.said[1], text)
        if voiced:
            action = [voiced if f == game.said[1] else f for f in action]
    story_beat = (game._progress or any(f.startswith("Location:") for f in action)) and cmd.get("verb") != "talk"
    out, narrated = await narrate(action, game.world, use_model=story_beat) if action else ("", False)
    return {"text": out, "facts": facts, "command": cmd, "model_raw": raw, "parsed_by": parsed_by,
            "narrated": narrated or bool(voiced), "director": director, "beats": game.beats_now,
            "ending_text": game.ending_text, "status": game.status(), "save": game.snapshot()}


@app.post("/api/turn")
async def turn(t: Turn):
    return await run_turn(get_session(t.session), t.input)


# ---------- party mode ----------
def get_party(code):
    p = parties.get(code.upper())
    if not p or p.session_id not in sessions:
        raise HTTPException(404, "No party with that code. Ask the host for the code on their screen.")
    return p


async def settle(p):
    """Play the winning idea if the round is over. Safe to call from every poll."""
    if not p.due():
        return
    async with p.lock:
        if not p.due():
            return
        win = p.close_round()
        if win:
            s = get_session(p.session_id)
            p.record(win["text"], win["by"], await run_turn(s, win["text"]))


@app.post("/api/party")
async def start_party(req: SessionRef):
    get_session(req.session)
    code = new_code(parties)
    parties[code] = Party(req.session, PARTY_SECONDS)
    return {"code": code, "round_seconds": PARTY_SECONDS}


@app.post("/api/party/{code}/join")
async def join_party(code: str, req: Join):
    p = get_party(code)
    try:
        pid = p.join(req.name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    g = get_session(p.session_id).game
    return {"player": pid, "name": p.players[pid], "title": g.world["title"], "theme": g.world.get("theme", {})}


@app.get("/api/party/{code}")
async def party_state(code: str, player: str | None = None, since: int = 0):
    p = get_party(code)
    await settle(p)
    g = get_session(p.session_id).game
    return {**p.view(player, since), "title": g.world["title"], "status": g.status()}


@app.post("/api/party/{code}/propose")
async def party_propose(code: str, req: Propose):
    p = get_party(code)
    await settle(p)
    try:
        p.propose(req.player, req.text)
    except KeyError as e:
        raise HTTPException(403, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return p.view(req.player)


@app.post("/api/party/{code}/vote")
async def party_vote(code: str, req: Vote):
    p = get_party(code)
    await settle(p)
    try:
        p.vote(req.player, req.proposal)
    except KeyError as e:
        raise HTTPException(403, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return p.view(req.player)


# ---------- health, stats, author tools ----------
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


@app.get("/api/stats")
async def stats(samples: int = 0):
    """How the model is doing since start-up: how often its output is used or thrown out, and why.
    ?samples=1 adds recent rejected outputs and text nobody could parse."""
    def rate(c):
        return round(c["accepted"] / c["requested"], 3) if c["requested"] else None
    out = {"sessions": len(sessions), "parties": len(parties),
           "parse": dict(STATS["parse"]),
           "narrate": {**STATS["narrate"], "accept_rate": rate(STATS["narrate"]),
                       "rejected_by": dict(STATS["narrate_rejected"])},
           "voice": {**STATS["voice"], "accept_rate": rate(STATS["voice"]),
                     "rejected_by": dict(STATS["voice_rejected"])}}
    if samples:
        out["samples"] = {k: list(v) for k, v in SAMPLES.items()}
    return out


AUTHOR_CACHE = {}


@app.get("/api/author/{world}")
async def author_report(world: str):
    """Validator + solver report for one world, for the author page. Off unless AUTHOR_TOOLS=1."""
    if not AUTHOR_TOOLS:
        raise HTTPException(404, "Author tools are off. Set AUTHOR_TOOLS=1 (the dev compose does).")
    if world not in WORLDS:
        raise HTTPException(404, f"Unknown world '{world}'.")
    if world not in AUTHOR_CACHE:
        w = WORLDS[world]
        reports = await asyncio.to_thread(solver.solve, w)
        AUTHOR_CACHE[world] = {"world": world, "title": w["title"], "map": MAPS[world], "summary": solver.summary(w, reports),
                               "problems": solver.problems(reports), "reports": json.loads(json.dumps(reports, default=list))}
    return AUTHOR_CACHE[world]


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/party")
async def party_page():
    return FileResponse(HERE / "static" / "party.html")


@app.get("/author")
async def author_page():
    return FileResponse(HERE / "static" / "author.html")
