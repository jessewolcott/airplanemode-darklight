"""The reusable parts: variants, modes, a world's own verbs, the solver, the no-model parser,
and the author CLI. No web server or model needed.

Run:  python tests/test_kit.py
"""
import asyncio
import copy
import json
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))
import solver  # noqa: E402
from engine import Game, load_world, load_worlds, parse_rule, pick_choices, validate  # noqa: E402
from parsing import fallback_parse  # noqa: E402

WORLDS = load_worlds(ROOT / "worlds")
DL, FB = WORLDS["darklight"], WORLDS["fogbell"]


def play(world, cmds, choices=None, mode="normal"):
    g = Game(world, choices, mode)
    for text in cmds:
        cmd = parse_rule(text, g) or fallback_parse(text, g)
        g.do(cmd)
    return g


# ---------- variants ----------
def test_variant_moves_the_item_and_fills_the_text():
    g = Game(DL, {"key_spot": "sea_chest"})
    assert "brass_key" in g.items["sea_chest"]["contains"] and "brass_key" not in g.items["drawer"]["contains"]
    assert "in the old sea chest in the cellar" in g.items["logbook"]["desc"]
    assert "{key_spot}" not in json.dumps(g.world)
    assert "in my desk" in Game(DL).items["logbook"]["desc"], "no choices = the authored layout"
    assert "{key_spot}" in DL["items"]["logbook"]["desc"], "the loaded world itself must not change"


def test_random_layouts_cover_every_option():
    seen = {pick_choices(DL, seed)["key_spot"] for seed in range(40)}
    assert seen == {"drawer", "sea_chest"}


def test_variant_layout_survives_save_and_restore():
    g = play(DL, ["n"], {"key_spot": "sea_chest"})
    h = Game.restore(DL, g.snapshot())
    assert h.choices == {"key_spot": "sea_chest"} and "brass_key" in h.items["sea_chest"]["contains"]
    bad = dict(g.snapshot(), choices={"key_spot": "moon"})
    try:
        Game.restore(DL, bad)
        raise AssertionError("accepted an unknown variant option")
    except ValueError:
        pass


# ---------- modes ----------
def test_modes_change_the_clock_timers_and_hints():
    normal, relaxed, hard = Game(DL), Game(DL, mode="relaxed"), Game(DL, mode="hard")
    assert relaxed.world["clock"]["deadline"] == 57 and hard.world["clock"]["deadline"] == 32
    assert normal.world["clock"]["deadline"] == 38
    assert relaxed.world["timers"] == {} and normal.world["timers"]
    assert all(b["turn"] < hard.world["clock"]["deadline"] for b in hard.world["clock"]["beats"])
    assert hard.do({"verb": "hint"}) == ["No hints in hard mode. You're on your own."]
    assert normal.do({"verb": "hint"})[0].startswith("Hint:")
    g = play(DL, ["n", "take matches", "take lantern", "light lantern"] + ["s", "n"] * 15, mode="relaxed")
    assert "lantern_lit" in g.flags, "the lantern never burns out in relaxed mode"
    assert Game.restore(DL, hard.snapshot()).mode == "hard"


# ---------- a world's own verbs and requires ----------
def test_world_verbs_parse_and_run():
    g = play(FB, ["e"])
    assert parse_rule("shout", g) == {"verb": "shout", "object": None}
    assert parse_rule("yell at the fog", g) == {"verb": "shout", "object": "fog"}
    assert fallback_parse("call out to the children", g)["verb"] == "shout"
    out = g.do({"verb": "shout", "object": None})
    assert "children_heard" in g.flags, out


def test_interaction_requires():
    g = play(FB, ["n", "e", "take ladder", "w", "use ladder on hatch", "u"])
    assert g.do({"verb": "ring", "object": "bell"}) == ["You slap the bell with your palm. It barely hums. It needs a rope to swing it."]
    assert not g.over


def test_validate_world_verbs_and_variants():
    w = copy.deepcopy(FB)
    w["verbs"]["take"] = []                          # clashes with a built-in
    w["verbs"]["dance"] = []                         # nothing uses it
    w["variants"][0]["options"][1]["at"] = "pews"   # not a container
    w["items"]["sexton"]["desc"] += " {nowhere}"     # placeholder with no variant
    errs = "\n".join(validate(w))
    for needle in ("'take' must be one lowercase word that isn't a built-in", "'dance' has no interactions",
                   "must be a room or a container", "placeholders with no variant: nowhere"):
        assert needle in errs, f"missing {needle!r} in:\n{errs}"


# ---------- solver ----------
def test_solver_finds_every_ending_in_every_world():
    for wid, world in WORLDS.items():
        reports = solver.solve(world)
        assert solver.problems(reports) == [], (wid, solver.problems(reports))
        for r in reports:
            assert set(r["endings"]) == set(world["endings"]), wid


def test_solver_routes_replay_through_the_parser():
    for wid, world in WORLDS.items():
        for r in solver.solve(world):
            for eid, info in r["endings"].items():
                if info["route"][0].startswith("("):
                    continue   # "let the clock run out"
                g = Game(world, r["choices"])
                for text in info["route"]:
                    cmd = parse_rule(text, g)
                    assert cmd is not None, f"{wid}: solver wrote {text!r}, which doesn't parse"
                    g.do(cmd)
                assert g.ending == eid and g.turns == info["turns"], (wid, eid, g.ending, g.turns)


def test_solver_catches_a_dead_end():
    w = copy.deepcopy(WORLDS["frozenpass"])
    w["interactions"] = [x for x in w["interactions"] if x["verb"] != "board"]       # no sled escape
    w["endings"].pop("downhill")
    w["uses"].insert(0, {"item": "firewood", "target": "ice_slope", "set": "wood_lost", "consume": True,
                         "fact": "You hurl the firewood down the slope."})          # throw away the only wood
    reports = solver.solve(w)
    assert reports[0]["dead_end_count"] > 0, "throwing away the only firewood should be a dead end"
    assert any("dead ends" in p for p in solver.problems(reports))
    assert "use firewood on ice" in reports[0]["closes"]


def test_solver_flags_endings_too_slow_for_the_deadline():
    w = copy.deepcopy(FB)
    w["clock"]["deadline"] = 9
    w["clock"]["beats"] = [b for b in w["clock"]["beats"] if b["turn"] < 9]
    probs = solver.problems(solver.solve(w))
    assert any("all_home" in p and "deadline" in p for p in probs), probs


# ---------- parsing quality floor ----------
def test_parse_eval_floor():
    """The no-model parser must not get worse. Raise these floors as it improves."""
    import parse_eval
    right, total, misses = asyncio.run(parse_eval.score(parse_eval.rules_then_fallback, cases=parse_eval.SETS["cases"]))
    assert right / total >= 0.95, f"tuned set fell to {right}/{total}: {misses[:5]}"
    right, total, _ = asyncio.run(parse_eval.score(parse_eval.rules_then_fallback, cases=parse_eval.SETS["heldout"]))
    assert right / total >= 0.5, f"held-out set fell to {right}/{total}"


# ---------- the CLI ----------
def run_cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "cli.py"), *args], capture_output=True, text=True, cwd=ROOT)


def test_cli_check_and_template():
    r = run_cli("check")
    assert r.returncode == 0 and r.stdout.count("ok") == len(WORLDS), r.stdout + r.stderr
    template = json.loads((ROOT / "templates" / "starter.json").read_text(encoding="utf-8"))
    template["id"] = "starter"
    assert validate(template) == [] and solver.problems(solver.solve(template)) == []


def test_cli_check_fails_on_a_broken_world(tmp=ROOT / "templates" / "_broken_test.json"):
    w = json.loads((ROOT / "worlds" / "fogbell.json").read_text(encoding="utf-8"))
    w["rooms"]["green"]["exits"]["north"] = "chapl"
    tmp.write_text(json.dumps(w), encoding="utf-8")
    try:
        r = run_cli("check", str(tmp))
        assert r.returncode == 1 and "chapl" in r.stdout, r.stdout
    finally:
        tmp.unlink()


def test_cli_play_reaches_an_ending():
    r = subprocess.run([sys.executable, str(ROOT / "cli.py"), "play", "fogbell"], input="e\nwade into the fog\n",
                       capture_output=True, text=True, cwd=ROOT)
    assert "One Small Hand" in r.stdout, r.stdout[-400:]


def test_schema_accepts_every_world():
    try:
        import jsonschema
    except ImportError:
        print("     (jsonschema not installed: skipped)")
        return
    schema = json.loads((ROOT / "schemas" / "world.schema.json").read_text(encoding="utf-8"))
    for path in list((ROOT / "worlds").glob("*.json")) + [ROOT / "templates" / "starter.json"]:
        errs = list(jsonschema.Draft7Validator(schema).iter_errors(json.loads(path.read_text(encoding="utf-8"))))
        assert not errs, f"{path.name}: {errs[0].message} at {list(errs[0].path)}"


def test_load_world_reports_every_problem():
    bad = ROOT / "templates" / "_bad_load.json"
    w = json.loads((ROOT / "templates" / "starter.json").read_text(encoding="utf-8"))
    w["start"] = "nowhere"
    w["uses"][0]["target"] = "ghost"
    bad.write_text(json.dumps(w), encoding="utf-8")
    try:
        load_world(bad)
        raise AssertionError("loaded a broken world")
    except ValueError as e:
        assert "nowhere" in str(e) and "unknown item" in str(e), e
    finally:
        bad.unlink()


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
