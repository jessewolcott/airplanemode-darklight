"""Author tools for adventures: make, check, solve and play world files.

  python cli.py new  <id> [--title "The Sunken Bell"]   start a world from the template
  python cli.py check [worlds/x.json ...]               validate + solve (all worlds if none given)
  python cli.py solve <id|path> [--mode hard] [--drops] [--json]
  python cli.py play  <id|path> [--mode relaxed] [--layout 3]   play in the terminal, no server
  python cli.py map   <id|path>                         draw the map as text
  python cli.py draft "<premise>" --id <id>             draft a world with Claude (dev machine only)

check exits non-zero on any problem, so it can run in CI or a pre-commit hook.
"""
import argparse
import json
import sys
from pathlib import Path

from engine import MODES, Game, load_world, parse_rule, pick_choices, validate
from parsing import fallback_parse
import solver

ROOT = Path(__file__).resolve().parent
WORLDS = ROOT / "worlds"
TEMPLATE = ROOT / "templates" / "starter.json"


def find(ref):
    p = Path(ref)
    if p.suffix == ".json" and p.exists():
        return p
    p = WORLDS / f"{ref}.json"
    if p.exists():
        return p
    sys.exit(f"no world '{ref}' (looked for {ref} and {p})")


def cmd_new(args):
    dest = WORLDS / f"{args.id}.json"
    if dest.exists():
        sys.exit(f"{dest} already exists")
    world = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    if args.title:
        world["title"] = args.title
    dest.write_text(json.dumps(world, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {dest}. Next: edit it, then  python cli.py check {dest}  and  python cli.py play {args.id}")


def cmd_check(args):
    paths = [find(r) for r in args.worlds] or sorted(WORLDS.glob("*.json"))
    bad = 0
    for path in paths:
        try:
            world = load_world(path)
        except ValueError as e:
            print(f"FAIL {e}")
            bad += 1
            continue
        found = []
        for mode in (["normal"] if not args.all_modes else list(MODES)):
            found += [f"[{mode}] {p}" for p in solver.problems(solver.solve(world, mode))]
        print(("FAIL " if found else "ok   ") + f"{path.name}")
        for p in found:
            print(f"     {p}")
        bad += bool(found)
    sys.exit(1 if bad else 0)


def cmd_solve(args):
    world = load_world(find(args.world))
    reports = solver.solve(world, args.mode, allow_drop=args.drops, max_states=args.max_states)
    if args.json:
        print(json.dumps(reports, indent=2, default=list))
    else:
        print(solver.summary(world, reports))
        probs = solver.problems(reports)
        print("\nproblems:" if probs else "\nno problems found")
        for p in probs:
            print(f"  {p}")


def cmd_map(args):
    world = load_world(find(args.world))
    pos = world["map"]
    cols = max(x for x, _ in pos.values()) + 1
    rows = max(y for _, y in pos.values()) + 1
    w = max(len(world["rooms"][r]["name"]) for r in pos) + 2
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    for rid, (x, y) in pos.items():
        grid[y][x] = rid
    for y in range(rows):
        line, below = "", ""
        for x in range(cols):
            rid = grid[y][x]
            name = world["rooms"][rid]["name"] if rid else ""
            exits = world["rooms"][rid]["exits"] if rid else {}
            line += f"[{name:^{w}}]" if rid else " " * (w + 2)
            east = exits.get("east")
            line += ("-" if not isinstance(east, dict) else "x") if east else " "
            south = exits.get("south")
            below += (" " * ((w + 2) // 2) + ("|" if not isinstance(south, dict) else "x")).ljust(w + 3) if south else " " * (w + 3)
        print(line)
        if below.strip():
            print(below)
    vertical = [(r, d, (e["to"] if isinstance(e, dict) else e)) for r, room in world["rooms"].items()
                for d, e in room["exits"].items() if d in ("up", "down")]
    if vertical:
        print("\nup/down:", ", ".join(f"{world['rooms'][a]['name']} {d} to {world['rooms'][b]['name']}" for a, d, b in vertical))
    print("x = locked until something happens")


def cmd_play(args):
    world = load_world(find(args.world))
    choices = pick_choices(world, args.layout) if args.layout is not None else None
    g = Game(world, choices, args.mode)
    print(f"\n{world['title']}  ({args.mode}, layout {g.choices or 'fixed'})\n\n{world['intro']}\n")
    print(" ".join(f for f in g.room_facts() if not f.startswith(("Location:",))))
    while not g.over:
        try:
            text = input(f"\n[{g.r()['name']} | turn {g.turns}/{g.world.get('clock', {}).get('deadline', '-')}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text in ("quit", "q!"):
            return
        if not text:
            continue
        cmd = parse_rule(text, g) or fallback_parse(text, g)
        facts = g.do(cmd)
        if args.debug:
            print(f"  ({cmd})")
        print("\n".join(facts))
    print(f"\n*** {world['endings'][g.ending]['title']} ({g.turns} turns) ***")


DRAFT_RULES = """You write world files for a small deterministic text-adventure engine. The engine owns all
state; a tiny on-device language model only narrates. Reply with ONE complete world as JSON inside a
```json fenced block, and nothing after the block.

Design rules:
- 4 to 8 rooms on a small grid ("map": room id -> [x, y]). Exits are north/south/east/west/up/down and
  should agree with the grid. Every room needs a map position.
- A clear goal, a clock ("clock": deadline in turns, 3-4 "beats" of rising pressure, "deadline_ending"),
  and 4 to 7 endings with different "tone"s: best, good, bittersweet, mixed, dark, worst. Every ending
  needs a "hint". At least one real choice with a cost (like rescuing someone, which takes turns).
- Only these mechanics exist: rooms, items (portable, person, container, locked_flag, light_flag,
  desc_flags), uses (item on target), interactions (help/board or the world's own verbs), talk lines,
  timers, variants, verb_defaults (light/unlock/fill tools), hints. Don't invent other keys.
- Conditions are "has:item", "flag:name", "seen:room", joined with | for "or". Anything that varies
  (ending, fact, cost) can be a list of {"if": ..., "<key>": ...} options; the LAST has no "if".
- Talk lists end with a line that has no "if". People are "they", never he/she.
- Item aliases are lowercase words a player would type; keep them unambiguous between items that
  can be seen at the same time.
- Write vivid but short prose. Present tense, second person. No first person in descriptions.
- The best ending must be reachable well inside the deadline (aim for about half of it).
"""


def cmd_draft(args):
    """Draft a world with Claude, then check it with the validator and solver and send the problems
    back until it's clean. Needs the dev extras (pip install -r requirements-dev.txt) and an
    Anthropic credential (ANTHROPIC_API_KEY or `ant auth login`). Never part of the box image."""
    try:
        import anthropic
    except ImportError:
        sys.exit("draft needs the Anthropic SDK: pip install -r requirements-dev.txt")
    dest = WORLDS / f"{args.id}.json"
    if dest.exists() and not args.force:
        sys.exit(f"{dest} exists (use --force to overwrite)")
    guide = (ROOT / "docs" / "writing-a-game.md").read_text(encoding="utf-8")
    example = (WORLDS / "darklight.json").read_text(encoding="utf-8")
    # Stable content first and cached: rules, the authoring guide and a full example world.
    system = [{"type": "text", "text": f"{DRAFT_RULES}\n\n# Authoring guide\n\n{guide}\n\n# A complete example world (darklight.json)\n\n```json\n{example}\n```",
               "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": f"Premise: {args.premise}\n\nWorld id: {args.id}. Write the world."}]
    client = anthropic.Anthropic()
    world = None
    for round_no in range(1, args.rounds + 1):
        print(f"round {round_no}: asking {args.model}...", flush=True)
        with client.beta.messages.stream(
            model=args.model,
            max_tokens=64000,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",          # if the request is declined, the API retries on a suitable model
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            sys.exit("the model declined this premise; try rewording it")
        text = "".join(b.text for b in response.content if b.type == "text")
        messages.append({"role": "assistant", "content": response.content})   # keep thinking blocks as-is
        problems = []
        try:
            body = text.split("```json", 1)[1].split("```", 1)[0]
            world = json.loads(body)
            world["id"] = args.id
            problems = validate(world)
            if not problems:
                problems = solver.problems(solver.solve(world))
        except (IndexError, json.JSONDecodeError) as e:
            problems = [f"couldn't read a ```json block from the reply: {e}"]
        if response.stop_reason == "max_tokens":
            problems.append("the reply was cut off; write a more compact world")
        if not problems:
            out = {k: v for k, v in world.items() if k != "id"}
            dest.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"clean after {round_no} round(s): wrote {dest}")
            print(solver.summary(world, solver.solve(world)))
            print(f"\nNext: read it, play it (python cli.py play {args.id}), and add its routes to tests/test_endings.py")
            return
        print(f"  {len(problems)} problem(s):")
        for p in problems[:15]:
            print(f"    {p}")
        messages.append({"role": "user", "content": "The validator and solver found these problems. "
                         "Reply with the complete corrected world:\n- " + "\n- ".join(problems[:40])})
    if world is not None:
        rough = WORLDS.parent / "templates" / f"{args.id}.draft.json"
        rough.write_text(json.dumps(world, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        sys.exit(f"still had problems after {args.rounds} rounds; last attempt saved to {rough}")
    sys.exit("no usable draft")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="start a world from the template")
    p.add_argument("id")
    p.add_argument("--title")
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("check", help="validate and solve; non-zero exit on problems")
    p.add_argument("worlds", nargs="*")
    p.add_argument("--all-modes", action="store_true", help="also check relaxed and hard")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("solve", help="fastest route to every ending, dead ends, traps, closing moves")
    p.add_argument("world")
    p.add_argument("--mode", choices=list(MODES), default="normal")
    p.add_argument("--drops", action="store_true", help="also try dropping things (slower, finds more traps)")
    p.add_argument("--max-states", type=int, default=300_000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_solve)
    p = sub.add_parser("play", help="play in the terminal")
    p.add_argument("world")
    p.add_argument("--mode", choices=list(MODES), default="normal")
    p.add_argument("--layout", type=int, help="seed for the variant layout (default: the authored one)")
    p.add_argument("--debug", action="store_true", help="show each parsed command")
    p.set_defaults(fn=cmd_play)
    p = sub.add_parser("map", help="draw the map as text")
    p.add_argument("world")
    p.set_defaults(fn=cmd_map)
    p = sub.add_parser("draft", help="draft a world with Claude (dev machine only)")
    p.add_argument("premise")
    p.add_argument("--id", required=True)
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_draft)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
