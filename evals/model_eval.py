"""Score the real model the way the server uses it: parsing, narration and voiced dialogue.

Rerun this whenever the shared model, a prompt, the sampling or a guard changes, and record the
numbers (llm/testing-and-evals.md). Every case runs --samples times (default 3): one sample per
prompt is noisy.

  python evals/model_eval.py http://127.0.0.1:18080              # all three parts, 3 samples
  python evals/model_eval.py URL --only parse --samples 1 --show   # list the parse misses
  python evals/model_eval.py URL --out result.json                 # keep every output

Parse: the server's `understand` (rules -> model -> fallback) on the tuned and held-out sets, plus
how often the model made a case worse than the no-model fallback ("model hurt").
Narrate: every story beat along the first route of each world (the facts run_turn would send).
Voice: every distinct dialogue line a visible person gives along all routes.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "tests"))


def beats(pe, parse_rule, routes):
    """(world id, step, action facts) for each narrated story beat on the first route of each world."""
    from engine import Game
    out = []
    for world, ends in routes.items():
        steps = next(iter(ends.values()))
        g = Game(pe.WORLDS[world])
        for s in steps[:14]:
            cmd = parse_rule(s, g)
            if cmd is None or g.over:
                break
            facts = g.do(cmd)
            authored = set(g.beats_now) | {g.ending_text}
            action = [f for f in facts if f not in authored]
            beat = (g._progress or any(f.startswith("Location:") for f in action)) and cmd.get("verb") != "talk"
            if action and beat:
                out.append((world, s, action))
    return out


def lines(pe, parse_rule, routes):
    """(game clone, speaker, line) for every distinct thing a person says along the routes."""
    from engine import Game
    seen, out = set(), []
    for world, ends in routes.items():
        for steps in ends.values():
            g = Game(pe.WORLDS[world])
            for s in steps:
                if g.over:
                    break
                for p in [i for i in g.visible() if g.items[i].get("person")]:
                    c = g.clone()
                    c.do({"verb": "talk", "object": p})
                    if c.said and (world, c.said[1]) not in seen:
                        seen.add((world, c.said[1]))
                        out.append((c, c.said[0], c.said[1]))
                cmd = parse_rule(s, g)
                if cmd is None:
                    break
                g.do(cmd)
    return out


async def run(args):
    os.environ["LLM_URL"] = args.url
    import parse_eval as pe
    import app
    from engine import parse_rule
    from parsing import fallback_parse
    from test_endings import ROUTES
    res = {"url": args.url, "samples": args.samples}

    if args.only in (None, "parse"):
        async def served(text, game):
            cmd, _, _ = await app.understand(text, game)
            return cmd
        for name, cases in pe.SETS.items():
            _, _, fb_miss = await pe.score(lambda t, g: parse_rule(t, g) or fallback_parse(t, g), cases=cases)
            fb_wrong = {(m["world"], m["room"], m["said"]) for m in fb_miss}
            rights, hurts, misses = [], [], []
            for _ in range(args.samples):
                right, total, miss = await pe.score(served, is_async=True, cases=cases)
                rights.append(right)
                hurts.append(sum((m["world"], m["room"], m["said"]) not in fb_wrong for m in miss))
                misses.append(miss)
            res[f"parse_{name}"] = {"right": rights, "total": total, "model_hurt": hurts, "misses": misses[-1]}
            print(f"parse {name:8} {'/'.join(map(str, rights))} of {total} "
                  f"(mean {100 * statistics.mean(rights) / total:.1f}%), model hurt {hurts}", flush=True)
            if args.show:
                for m in misses[-1]:
                    print(f"    [{m['world']}/{m['room']}] {m['said']!r:42} wanted {m['means']!r:20} got {m['got']}")

    if args.only in (None, "narrate"):
        rows, t0 = [], time.time()
        app.STATS["narrate_rejected"].clear()
        for world, step, facts in beats(pe, parse_rule, ROUTES):
            for _ in range(args.samples):
                app.SAMPLES["narrate"].clear()
                try:
                    out = await app.llm_narrate(facts, pe.WORLDS[world])
                except Exception as e:  # noqa: BLE001
                    out = None
                    print("  error", repr(e))
                rows.append({"world": world, "step": step, "facts": facts, "out": out,
                             "rejected": list(app.SAMPLES["narrate"])})
        ok = sum(1 for r in rows if r["out"])
        first = sum(1 for r in rows if r["out"] and not r["rejected"])
        res["narrate"] = {"accepted": ok, "first_try": first, "total": len(rows),
                          "rejected_by": dict(app.STATS["narrate_rejected"]), "seconds": round(time.time() - t0),
                          "rows": rows}
        print(f"narrate  accepted {ok}/{len(rows)} (first try {first}), rejects {dict(app.STATS['narrate_rejected'])}",
              flush=True)
        if args.show:
            for r in rows:
                for rej in r["rejected"]:
                    print(f"    [{r['world']} {r['step']}] {rej['problem']}: {rej['output']}")

    if args.only in (None, "voice"):
        rows = []
        app.STATS["voice_rejected"].clear()
        for game, speaker, line in lines(pe, parse_rule, ROUTES):
            for _ in range(args.samples):
                app.SAMPLES["voice"].clear()
                try:
                    out = await app.llm_voice(game, speaker, line, f"talk to the {game.name(speaker)}")
                except Exception as e:  # noqa: BLE001
                    out = None
                    print("  error", repr(e))
                rows.append({"world": game.world["id"], "speaker": speaker, "line": line, "out": out,
                             "rejected": list(app.SAMPLES["voice"])})
        ok = sum(1 for r in rows if r["out"])
        res["voice"] = {"accepted": ok, "total": len(rows), "rejected_by": dict(app.STATS["voice_rejected"]),
                        "rows": rows}
        print(f"voice    accepted {ok}/{len(rows)}, rejects {dict(app.STATS['voice_rejected'])}", flush=True)
        if args.show:
            for r in rows:
                for rej in r["rejected"]:
                    print(f"    [{r['world']} {r['speaker']}] {rej['problem']}: {rej['output']}")

    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="llama.cpp base URL, e.g. http://127.0.0.1:18080")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--only", choices=["parse", "narrate", "voice"])
    ap.add_argument("--show", action="store_true", help="list parse misses and rejected outputs")
    ap.add_argument("--out", help="write every result to this JSON file")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
