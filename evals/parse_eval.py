"""Score how well player text is understood, so parser and model changes are measured, not guessed.

Each case is something a player might type, and the command it should mean. A parse is right
when it has the same effect in the game (same state afterwards, same text), so "unlock door"
and "use key on door" both count.

  python evals/parse_eval.py                       # rules, then rules + no-model fallback
  python evals/parse_eval.py --llm http://localhost:8080   # also rules + model (+ fallback), as the server does
  python evals/parse_eval.py --show                # list every miss
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import Game, load_worlds, parse_rule  # noqa: E402
from parsing import fallback_parse  # noqa: E402

WORLDS = load_worlds(ROOT / "worlds")
SETS = {name: json.loads((ROOT / "evals" / f"parse_{name}.json").read_text(encoding="utf-8"))
        for name in ("cases", "heldout")}
CASES = SETS["cases"]


def game_at(world, steps):
    g = Game(WORLDS[world])
    for s in steps:
        cmd = parse_rule(s, g)
        assert cmd is not None, f"setup step {s!r} doesn't parse"
        g.do(cmd)
    return g


def effect(game, cmd):
    g = game.clone()
    facts = g.do(cmd)
    snap = g.snapshot()
    snap.pop("since_progress")
    return snap, facts


def expected(game, means):
    if means == "unknown":
        return effect(game, {"verb": "unknown"})
    cmd = parse_rule(means, game)
    assert cmd is not None, f"expected command {means!r} doesn't parse"
    return effect(game, cmd)


def rules_only(text, game):
    return parse_rule(text, game) or {"verb": "unknown"}


def rules_then_fallback(text, game):
    return parse_rule(text, game) or fallback_parse(text, game)


async def score(parser, is_async=False, cases=None):
    """parser(text, game) -> command. Returns (right, total, misses)."""
    right, total, misses = 0, 0, []
    for group in cases or CASES:
        base = game_at(group["world"], group["at"])
        for text, means in group["cases"]:
            total += 1
            cmd = (await parser(text, base.clone())) if is_async else parser(text, base.clone())
            if effect(base, cmd) == expected(base, means):
                right += 1
            else:
                misses.append({"world": group["world"], "room": base.room, "said": text, "means": means, "got": cmd})
    return right, total, misses


def report(name, result, show):
    right, total, misses = result
    print(f"{name:28} {right:3}/{total} = {100 * right / total:5.1f}%")
    if show:
        for m in misses:
            print(f"    [{m['world']}/{m['room']}] {m['said']!r:40} wanted {m['means']!r:22} got {m['got']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", help="llama.cpp base URL; also scores the model the way the server uses it")
    ap.add_argument("--show", action="store_true", help="list every miss")
    args = ap.parse_args()
    for name, cases in SETS.items():
        print(f"-- {name} ({'tuned against' if name == 'cases' else 'never tuned against: the honest number'})")
        report("rules only", asyncio.run(score(rules_only, cases=cases)), args.show)
        report("rules + fallback (no model)", asyncio.run(score(rules_then_fallback, cases=cases)), args.show)
    if args.llm:
        os.environ["LLM_URL"] = args.llm
        import app  # noqa: E402  reads LLM_URL at import

        async def served(text, game):
            cmd, _, _ = await app.understand(text, game)
            return cmd
        for name, cases in SETS.items():
            report(f"{name}: rules + model", asyncio.run(score(served, is_async=True, cases=cases)), args.show)


if __name__ == "__main__":
    main()
