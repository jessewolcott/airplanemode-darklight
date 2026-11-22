"""Every ending in every world must stay reachable. Plays scripted routes through the engine
(no model needed).

Run:  python tests/test_endings.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import Game, load_worlds, parse_rule  # noqa: E402

WORLDS = load_worlds(ROOT / "worlds")

DL_BASE = ["n", "take matches", "take lantern", "light lantern", "open drawer", "take key", "d", "take oil"]
FP_BASE = ["e", "open cupboard", "take all", "unlock door", "e", "take firewood and axe", "w"]
ROUTES = {
    "darklight": {
        "keeper_light": DL_BASE + ["talk to keeper", "help keeper", "s", "e", "unlock door", "u", "u", "fill lamp", "light lamp"],
        "lone_light": DL_BASE + ["u", "s", "e", "unlock door", "u", "u", "fill lamp", "light lamp"],
        "bonfire_keeper": ["n", "take matches", "take lantern", "light lantern", "d", "help keeper", "s", "light driftwood"],
        "bonfire": ["n", "take matches", "s", "light driftwood"],
        "flight_keeper": ["n", "take matches", "take lantern", "light lantern", "d", "help keeper", "s", "board boat"],
        "flight": ["board boat"],
        # looking is free now, so burn the clock by walking back and forth
        "wreck": ["n", "take matches", "s", "e"] + ["w", "e"] * 20,
    },
    "frozenpass": {
        "warm_together": FP_BASE + ["w", "use axe on ice", "u", "help climber", "fill stove", "light stove"],
        "warm_alone": FP_BASE + ["fill stove", "light stove"],
        "downhill": ["ride sled"],
        "white_silence": ["e", "w"] * 16,
    },
    "fogbell": {
        "all_home": ["e", "shout fog", "w", "n", "e", "take ladder", "open chest", "take rope", "w",
                     "use ladder on hatch", "u", "use rope on bell", "ring bell"],
        "two_home": ["n", "e", "take ladder", "open chest", "take rope", "w", "use ladder on hatch", "u",
                     "use rope on bell", "ring bell"],
        "one_hand": ["e", "wade fog"],
        "tide": ["e", "w"] * 14,
    },
}


def play(world, cmds):
    g = Game(WORLDS[world])
    for text in cmds:
        if g.over:
            break
        cmd = parse_rule(text, g)
        assert cmd is not None, f"{world}: rule parser could not parse {text!r}"
        g.do(cmd)
    return g


def main():
    failed = []
    assert set(ROUTES) == set(WORLDS), "every world needs routes here"
    for world, routes in ROUTES.items():
        assert set(routes) == set(WORLDS[world]["endings"]), f"{world}: every ending needs a route here"
        for want, cmds in routes.items():
            g = play(world, cmds)
            ok = g.ending == want
            print(f"{'ok  ' if ok else 'FAIL'} {world:11} {want:15} got={g.ending} turns={g.turns}")
            if not ok:
                failed.append(f"{world}/{want}")
    # the bonfire only works on the shore
    g = play("darklight", ["take driftwood", "n", "take matches", "light driftwood"])
    assert g.ending is None, "bonfire should not light away from the shore"
    if failed:
        print("failed:", ", ".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
