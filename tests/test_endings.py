"""Every ending must stay reachable. Plays scripted routes through the engine (no model needed).

Run:  python tests/test_endings.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import Game, parse_rule  # noqa: E402

WORLD = json.loads((ROOT / "world.json").read_text(encoding="utf-8"))
BASE = ["n", "take matches", "take lantern", "light lantern", "open drawer", "take key", "d", "take oil"]
ROUTES = {
    "keeper_light": BASE + ["talk to keeper", "help keeper", "s", "e", "unlock door", "u", "u", "fill lamp", "light lamp"],
    "lone_light": BASE + ["u", "s", "e", "unlock door", "u", "u", "fill lamp", "light lamp"],
    "bonfire": ["n", "take matches", "s", "light driftwood"],
    "flight": ["board boat"],
    "wreck": ["n", "take matches", "s", "e"] + ["examine door"] * 40,
}


def play(cmds):
    g = Game(WORLD)
    for text in cmds:
        if g.over:
            break
        cmd = parse_rule(text, g)
        assert cmd is not None, f"rule parser could not parse {text!r}"
        g.do(cmd)
    return g


def main():
    failed = []
    for want, cmds in ROUTES.items():
        g = play(cmds)
        ok = g.ending == want
        print(f"{'ok  ' if ok else 'FAIL'} {want:14} got={g.ending} turns={g.turns}")
        if not ok:
            failed.append(want)
    assert set(ROUTES) == set(WORLD["endings"]), "every ending in world.json needs a route here"
    # the bonfire only works on the shore
    g = play(["take driftwood", "n", "take matches", "light driftwood"])
    assert g.ending is None, "bonfire should not light away from the shore"
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
