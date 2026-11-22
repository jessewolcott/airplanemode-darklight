"""Engine rules: regressions for fixed bugs and the newer mechanics. No model needed.

Run:  python tests/test_engine.py
"""
import copy
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import Game, load_worlds, parse_rule, validate  # noqa: E402

WORLDS = load_worlds(ROOT / "worlds")
DL = WORLDS["darklight"]


def play(cmds, world=DL):
    g = Game(world)
    out = []
    for text in cmds:
        cmd = parse_rule(text, g)
        assert cmd is not None, f"rule parser could not parse {text!r}"
        out = g.do(cmd)
    return g, out


IN_CELLAR = ["n", "take matches", "take lantern", "light lantern", "d"]


def test_dropped_lantern_still_lights_the_room():
    g, _ = play(IN_CELLAR + ["drop lantern"])
    assert not g.is_dark(), "a lit lantern on the floor should light the cellar"
    g, out = play(IN_CELLAR + ["drop lantern", "take lantern"])
    assert "lantern" in g.inv, out


def test_missing_tool_messages():
    _, out = play(["e", "unlock door"])
    assert out == ["You need a key for that."], out
    _, out = play(["n", "take lantern", "s", "light lantern"])
    assert out == ["You have nothing to light it with."], out
    _, out = play(["n", "take lantern", "light lantern"])  # the matches are right there
    assert out == ["You need to be holding the tin of matches first."], out
    g, _ = play(["n"])  # no door here either (the model's parse can produce this)
    assert g.do({"verb": "unlock", "target": "iron_door"}) == ["You don't see that here."]


def test_looking_and_failing_are_free():
    g, _ = play(["n", "x desk", "look", "read logbook", "take desk", "w", "i", "talk"])
    assert g.turns == 1, f"only the move north should cost a turn, got {g.turns}"


def test_talking_with_news_costs_a_turn():
    g, _ = play(IN_CELLAR + ["talk keeper"])
    assert g.turns == 6 and "knows_tackle" in g.flags
    g2, _ = play(IN_CELLAR + ["talk keeper", "talk keeper"])
    assert g2.turns == 6, "repeating a line shouldn't cost time"


def test_multiple_objects():
    g, _ = play(["n", "take matches and lantern"])
    assert g.inv == ["matches", "lantern"] and g.turns == 2
    g, _ = play(["n", "take matches, lantern"])
    assert g.inv == ["matches", "lantern"]
    g, _ = play(["n", "take all"])
    assert set(g.inv) == {"matches", "lantern"}
    g, _ = play(["n", "take all", "drop all"])
    assert g.inv == []
    g, out = play(["take all"])
    assert set(g.inv) == {"driftwood"}, out


def test_block_and_tackle_is_one_object():
    g, _ = play(IN_CELLAR + ["talk keeper", "take block and tackle"])
    assert g.inv.count("tackle") == 1


def test_keepers_tip_shortens_the_rescue():
    slow, _ = play(IN_CELLAR + ["help keeper"])
    fast, _ = play(IN_CELLAR + ["talk keeper", "take tackle", "help keeper"])
    assert slow.turns == 5 + 5, slow.turns          # 1 + cost 4
    assert fast.turns == 5 + 1 + 1 + 2, fast.turns  # talk, take, 1 + cost 1
    assert "pulley" in " ".join(play(IN_CELLAR + ["talk keeper", "take tackle", "help keeper"])[1])


def test_tackle_hidden_until_the_keeper_mentions_it():
    g, _ = play(IN_CELLAR)
    assert "tackle" not in g.visible()
    g, _ = play(IN_CELLAR + ["talk keeper"])
    assert "tackle" in g.visible()


def test_lantern_burns_out_and_can_be_refilled():
    g, _ = play(IN_CELLAR + ["take oil", "u", "d"] * 1)
    burn = ["u", "d"] * 10
    g, out = play(IN_CELLAR + burn)
    assert "lantern_lit" not in g.flags and "lantern_dry" in g.flags, sorted(g.flags)
    assert g.is_dark()
    assert "Your lantern sputters and goes out. Its oil is gone." in g.beats_now or "lantern_dry" in g.flags
    g, out = play(IN_CELLAR + ["take oil"] + burn + ["light lantern"])
    assert "needs oil" in out[0], out
    g, out = play(IN_CELLAR + ["take oil"] + burn + ["fill lantern", "light lantern"])
    assert "lantern_lit" in g.flags, out


def test_refill_renews_a_burning_lantern():
    g, _ = play(IN_CELLAR + ["take oil", "u", "d", "u", "d", "fill lantern"])
    assert g.timers["lantern_lit"] == DL["timers"]["lantern_lit"]["turns"]


def test_use_prefers_the_room_over_your_pockets():
    route = ["n", "take matches", "take lantern", "light lantern", "open drawer", "take key", "d", "take oil",
             "u", "s", "e", "unlock door", "u", "u", "use oil"]
    g, out = play(route)
    assert "lamp_fueled" in g.flags, out


def test_snapshot_round_trip():
    g, _ = play(IN_CELLAR + ["talk keeper", "take tackle", "take oil"])
    snap = g.snapshot()
    h = Game.restore(DL, snap)
    assert h.snapshot() == snap
    assert h.room == "cellar" and "tackle" in h.inv and h.timers == g.timers


def test_restore_rejects_tampered_saves():
    g, _ = play(["n"])
    bad = []
    for k, v in [("room", "moon"), ("inv", ["unicorn"]), ("turns", -5), ("world", "frozenpass"),
                 ("rooms", {"cottage": ["dragon"]}), ("flags", "notalist"), ("ending", "made_up")]:
        s = copy.deepcopy(g.snapshot())
        s[k] = v
        bad.append(s)
    bad.append({"world": "darklight"})
    bad.append("not even a dict")
    for s in bad:
        try:
            Game.restore(DL, s)
        except ValueError:
            continue
        raise AssertionError(f"restore accepted a bad save: {s!r:.120}")


def test_validate_catches_mistakes():
    assert validate(DL) == []
    w = copy.deepcopy(DL)
    w["rooms"]["shore"]["exits"]["north"] = "cottag"
    w["uses"][0]["ending"] = [{"if": "flag:x", "ending": "keeper_light"}]   # no fallback
    w["items"]["keeper"]["talk"][-1]["if"] = "flag:x"                       # no fallback line
    w["hints"].append({"done": "hsa:matches", "hint": "typo"})
    errs = "\n".join(validate(w))
    for needle in ("unknown room 'cottag'", "fallback", "last talk line", "unknown condition"):
        assert needle in errs, f"expected {needle!r} in:\n{errs}"


def test_missing_fallbacks_dont_crash():
    w = copy.deepcopy(DL)
    w["items"]["keeper"]["talk"] = [{"if": "flag:never", "say": "..."}]
    g = Game(w)
    g.room, g.flags = "cellar", {"lantern_lit"}
    g.inv = ["lantern"]
    assert g.do({"verb": "talk", "object": "keeper"}) == ["The keeper has nothing to say."]
    assert g.finish([{"if": "flag:never", "ending": "wreck"}]) is None and not g.over


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
