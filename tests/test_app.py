"""Server, guards and command building, with no model (LLM_URL is empty).
Needs the app's requirements:  pip install -r requirements.txt

Run:  python tests/test_app.py
"""
import os
import sys
import traceback
from pathlib import Path

os.environ["LLM_URL"] = ""
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as server  # noqa: E402
from engine import Game  # noqa: E402

client = TestClient(server.app)
DL = server.WORLDS["darklight"]


# ---------- guards ----------
def test_narration_guard_reasons():
    facts = ["You go north.", "Location: Keeper's Cottage.", "A cramped cottage.", "Items here: tin of matches, lantern.", "Exits: south, down."]
    good = "You head north into the cramped cottage, where a tin of matches and a lantern wait. Ways lead south and down."
    assert server.narration_problem(good, facts) is None
    cases = {
        "I walk north into the cottage with the matches and lantern.": "first_person",
        "You enter the cramped cottage. A lantern sits there.": "dropped_direction",
        "You head north into the cramped cottage. A lantern waits.": "dropped_item",
        "You head north past the old mill to the matches and lantern.": "copied_example",
        "You head north and grab the matches and the lantern.": "claimed_action",
        "": "empty",
    }
    for out, why in cases.items():
        assert server.narration_problem(out, facts) == why, (out, server.narration_problem(out, facts))


def test_voice_guard():
    line = DL["items"]["keeper"]["talk"][0]["say"]   # "Fill the lamp, light the wick. Go!"
    ok = '"Stop fussing," the keeper croaks. "Fill the lamp and light the wick. Go!"'
    assert server.voice_problem(ok, line, DL) is None, server.voice_problem(ok, line, DL)
    assert server.voice_problem('"Light the wick. Go!" the keeper croaks, waving you off impatiently.', line, DL) == "dropped_detail"
    assert server.voice_problem('"Fill the lamp, light the wick, and take the rowboat," the keeper says.', line, DL) == "invented_detail"
    assert server.voice_problem('"Fill the lamp, light the wick," he says. "Go!"', line, DL) == "gendered"
    assert server.voice_problem("Fill the lamp, light the wick. Go now.", line, DL) == "not_dialogue"


# ---------- command building (the model's verb + code's nouns) ----------
def test_build_command_multi_take():
    g = Game(DL)
    g.room = "cottage"
    cmd = server.build_command("take", ["matches", "lantern"], None, g)
    assert cmd == {"verb": "take", "objects": ["matches", "lantern"]}, cmd
    cmd = server.build_command("take", ["matches", "desk"], None, g)
    assert cmd == {"verb": "take", "object": "matches"}, cmd


def test_build_command_use_finds_the_rule_pair():
    g = Game(DL)
    g.room, g.inv = "base", ["brass_key"]
    cmd = server.build_command("use", ["iron_door", "brass_key"], None, g)
    assert cmd == {"verb": "use", "object": "brass_key", "target": "iron_door"}, cmd


def test_mentions_fuzzy():
    g = Game(DL)
    g.room = "cottage"
    assert server.mentions("grab the latern and the matchs", g) == ["lantern", "matches"]


# ---------- sessions ----------
def test_sessions_expire_and_are_capped():
    server.sessions.clear()
    old_ttl, old_max = server.SESSION_TTL, server.MAX_SESSIONS
    try:
        server.SESSION_TTL, server.MAX_SESSIONS = 100, 3
        sids = [server.open_session(Game(DL)) for _ in range(5)]
        assert list(server.sessions) == sids[-3:], "oldest sessions should go past the cap"
        server.sessions[sids[-3]].last -= 1000   # the least recently used one
        server.prune()
        assert sids[-3] not in server.sessions and len(server.sessions) == 2, "idle sessions should expire"
    finally:
        server.SESSION_TTL, server.MAX_SESSIONS = old_ttl, old_max
        server.sessions.clear()


# ---------- API ----------
def new(world=None):
    r = client.post("/api/new", json={"world": world} if world else {})
    assert r.status_code == 200, r.text
    return r.json()


def turn(sid, text):
    r = client.post("/api/turn", json={"session": sid, "input": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_worlds_and_new_game():
    w = client.get("/api/worlds").json()
    assert {x["id"] for x in w["worlds"]} == {"darklight", "frozenpass"} and w["default"] == "darklight"
    d = new("frozenpass")
    assert d["world"] == "frozenpass" and d["title"] == "The Frozen Pass" and d["status"]["room_id"] == "trail"
    assert d["endings_total"] == len(d["endings"]) == 4 and all("hint" in e for e in d["endings"])
    assert d["narrated"] is False  # no model configured
    assert client.post("/api/new", json={"world": "atlantis"}).status_code == 404


def test_turn_and_undo():
    sid = new()["session"]
    turn(sid, "n")
    d = turn(sid, "take matches")
    assert d["status"]["inventory"] == ["tin of matches"]
    turn(sid, "look")  # free, so not an undo step
    d = turn(sid, "undo")
    assert d["undone"] and d["status"]["inventory"] == [] and d["status"]["room_id"] == "cottage"
    d = client.post("/api/undo", json={"session": sid}).json()
    assert d["status"]["room_id"] == "shore"
    assert client.post("/api/undo", json={"session": sid}).json()["text"] == "There's nothing to undo."


def test_undo_an_ending():
    sid = new()["session"]
    d = turn(sid, "board boat")
    assert d["status"]["over"] and d["ending_text"]
    assert "over" in turn(sid, "n")["text"]
    d = turn(sid, "undo")
    assert not d["status"]["over"]


def test_resume_from_save():
    sid = new()["session"]
    turn(sid, "n")
    save = turn(sid, "take lantern")["save"]
    server.sessions.clear()  # the server restarted
    assert client.post("/api/turn", json={"session": sid, "input": "look"}).status_code == 404
    d = client.post("/api/resume", json={"save": save}).json()
    assert d["status"]["room_id"] == "cottage" and d["status"]["inventory"] == ["lantern"]
    d = turn(d["session"], "take matches")
    assert d["status"]["inventory"] == ["lantern", "tin of matches"]
    bad = dict(save, room="moon")
    assert client.post("/api/resume", json={"save": bad}).status_code == 400
    assert client.post("/api/resume", json={"save": dict(save, world="atlantis")}).status_code == 400


def test_stats_and_health():
    sid = new()["session"]
    turn(sid, "n")
    s = client.get("/api/stats").json()
    assert s["parse"].get("rules", 0) >= 1 and "rejected_by" in s["narrate"]
    assert client.get("/api/health").json() == {"llm": "none", "model": None}


def test_fake_model_voice_and_fallbacks():
    """A stand-in model: voiced dialogue is used when it passes the guards; bad narration falls back."""
    replies = []

    async def fake_chat(messages, max_tokens, temperature, schema=None):
        return replies.pop(0)

    real_chat, real_url = server.chat, server.LLM_URL
    server.chat, server.LLM_URL = fake_chat, "http://fake"
    for c in server.STATS.values():
        c.clear()
    try:
        replies[:] = ["You step onto the shore."]               # intro narration: drops the items -> rejected
        replies += ["You step onto the shore."]                  # retry: rejected again -> plain text
        d = new()
        assert d["narrated"] is False
        sid = d["session"]
        g = server.sessions[sid].game
        g.room, g.inv, g.flags = "cottage", [], {"keeper_rescued"}
        g.place({"keeper": "cottage"})
        replies[:] = ['"Quit fussing," the keeper growls. "Fill the lamp, light the wick. Go!"']
        d = turn(sid, "talk to keeper")
        assert d["narrated"] and d["text"].startswith('"Quit fussing,"'), d["text"]
        replies[:] = ['"Take the rowboat and go," he says.', '"Go," the keeper says.']   # both rejected
        d = turn(sid, "talk to keeper")
        assert not d["narrated"] and "Fill the lamp, light the wick" in d["text"], d["text"]
        s = client.get("/api/stats").json()
        assert s["narrate"]["fell_back"] == 1 and s["narrate"]["rejected_by"]["dropped_item"] == 2, s
        assert s["voice"]["accepted"] == 1 and s["voice"]["fell_back"] == 1, s
        assert set(s["voice"]["rejected_by"]) == {"gendered", "too_short"}, s
    finally:
        server.chat, server.LLM_URL = real_chat, real_url


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200 and "api/worlds" in r.text


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
