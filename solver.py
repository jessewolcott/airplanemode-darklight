"""Exhaustive solver for a world file.

It plays every possible game (every move, take, use, talk...) through the real engine and
reports, for each layout variant:
  - the fastest route to every ending, and whether it fits inside the deadline;
  - endings that can't be reached at all;
  - dead ends: positions where no ending is possible any more (a soft-lock; always a bug);
  - traps: positions where only the bad endings are left (sometimes intended, worth knowing);
  - choices that close off an ending for good ("fill lamp" before the rescue, say).

States are compared without the turn count, so the search is finite. Time pressure is then
judged from the fastest route. Routes are written as commands the rule parser understands,
so tests can replay them.
"""
import copy
import heapq
import itertools
from collections import defaultdict

from engine import Game, modes_for

GOOD_TONES = ("best", "good", "bittersweet", "mixed")   # anything else counts as a bad ending


def alias(game, iid):
    return game.items[iid]["aliases"][0]


def actions(g, allow_drop=True):
    """Every command worth trying from here, as (command, text)."""
    out = [({"verb": "go", "direction": d}, d) for d in g.exits()]
    vis = g.visible()
    for i in vis:
        it = g.items[i]
        if it.get("portable") and i not in g.inv:
            out.append(({"verb": "take", "object": i}, f"take {alias(g, i)}"))
        if it.get("container") and not it.get("open"):
            out.append(({"verb": "open", "object": i}, f"open {alias(g, i)}"))
        if it.get("person"):
            out.append(({"verb": "talk", "object": i}, f"talk to {alias(g, i)}"))
    if allow_drop:
        out += [({"verb": "drop", "object": i}, f"drop {alias(g, i)}") for i in g.inv]
    for x in g.world.get("interactions", []):
        if x["target"] in vis:
            out.append(({"verb": x["verb"], "object": x["target"]}, f"{x['verb']} {alias(g, x['target'])}"))
    for u in g.world["uses"]:
        if u["item"] in g.inv and u["target"] in vis:
            out.append(({"verb": "use", "object": u["item"], "target": u["target"]},
                        f"use {alias(g, u['item'])} on {alias(g, u['target'])}"))
    return out


def uses_seen(world):
    """Does anything other than hints depend on which rooms you've seen?"""
    w = {k: v for k, v in world.items() if k != "hints"}
    return "seen:" in str(w)


def state_key(g, track_seen):
    return (g.room, frozenset(g.inv), frozenset(g.flags),
            tuple(tuple(sorted(r["items"])) for r in g.rooms.values()),
            frozenset(i for i, it in g.items.items() if it.get("open")),
            tuple(sorted(g.timers.items())),
            frozenset(g.seen) if track_seen else None)


def solve_layout(world, choices, mode="normal", max_states=300_000, allow_drop=False):
    """Search one layout. The deadline is switched off while searching so that every
    position is explored; whether each ending fits in the real deadline is checked after."""
    real_deadline = Game(world, choices, mode).world.get("clock", {}).get("deadline")
    free = copy.deepcopy(world)
    if free.get("clock"):
        free["clock"]["deadline"] = 10 ** 6
        free["clock"]["beats"] = []
    start = Game(free, choices, mode)
    track_seen = uses_seen(world)
    k0 = state_key(start, track_seen)
    dist, parent, games = {k0: 0}, {k0: None}, {k0: start}
    edges = defaultdict(set)          # state -> next states
    ends = defaultdict(set)           # state -> endings reachable in one move
    best_end = {}                     # ending -> (turns, state, text)
    heap, tie = [(0, 0, k0)], itertools.count(1)
    done, truncated = set(), False
    while heap:
        turns, _, k = heapq.heappop(heap)
        if k in done:
            continue
        done.add(k)
        g = games.pop(k)
        for cmd, text in actions(g, allow_drop):
            h = g.clone()
            h.do(cmd)
            if h.turns == g.turns and not h.over:
                continue                       # nothing changed
            if h.over:
                ends[k].add(h.ending)
                if h.ending not in best_end or h.turns < best_end[h.ending][0]:
                    best_end[h.ending] = (h.turns, k, text)
                continue
            nk = state_key(h, track_seen)
            edges[k].add((nk, text))
            if nk not in dist or h.turns < dist[nk]:
                if nk not in dist and len(dist) >= max_states:
                    truncated = True
                    continue
                dist[nk], parent[nk], games[nk] = h.turns, (k, text), h
                heapq.heappush(heap, (h.turns, next(tie), nk))

    def route(k):
        steps = []
        while parent.get(k):
            k, text = parent[k]
            steps.append(text)
        return steps[::-1]

    # Which endings can still happen from each position? Propagate backwards to a fixed point.
    eids = list(world["endings"])
    bit = {e: 1 << i for i, e in enumerate(eids)}
    reach = {k: sum(bit[e] for e in ends[k]) for k in dist}
    back = defaultdict(set)
    for k, outs in edges.items():
        for nk, _ in outs:
            back[nk].add(k)
    work = [k for k in dist if reach[k]]
    while work:
        k = work.pop()
        for pk in back[k]:
            if reach[k] & ~reach[pk]:
                reach[pk] |= reach[k]
                work.append(pk)

    deadline_end = world.get("clock", {}).get("deadline_ending")
    good = sum(bit[e] for e in eids if world["endings"][e]["tone"] in GOOD_TONES)
    endings = {}
    for e in eids:
        if e in best_end:
            t, k, text = best_end[e]
            endings[e] = {"turns": t, "route": route(k) + [text],
                          "in_time": real_deadline is None or t <= real_deadline}
        elif e == deadline_end and real_deadline:
            endings[e] = {"turns": real_deadline, "route": ["(let the clock run out)"], "in_time": True}
    by_dist = sorted(dist, key=dist.get)
    dead = [k for k in by_dist if not truncated and not reach[k]]
    traps = [k for k in by_dist if not truncated and reach[k] and good and not reach[k] & good]

    # A move "closes" an ending if it makes it impossible from then on, in most places it's played
    # (so a move that just happens to coincide with the lantern burning out doesn't get blamed).
    closes = defaultdict(set)
    if not truncated:
        played, lost_n = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
        for k, outs in edges.items():
            for nk, text in outs:
                lost = reach[k] & ~reach[nk]
                for e in eids:
                    if reach[k] & bit[e]:
                        played[text][e] += 1
                        if lost & bit[e]:
                            lost_n[text][e] += 1
        for text, per in lost_n.items():
            for e, n in per.items():
                if n / played[text][e] >= 0.5:
                    closes[text].add(e)
    return {
        "choices": choices, "mode": mode, "states": len(dist), "truncated": truncated,
        "deadline": real_deadline, "endings": endings,
        "unreachable": [e for e in eids if e not in endings],
        "dead_ends": [{"route": route(k), "room": k[0]} for k in dead[:3]], "dead_end_count": len(dead),
        "traps": [{"route": route(k), "room": k[0]} for k in traps[:3]], "trap_count": len(traps),
        "closes": {t: sorted(es) for t, es in sorted(closes.items())},
    }


def layouts(world):
    """Every combination of variant options."""
    vs = world.get("variants", [])
    if not vs:
        return [{}]
    return [dict(zip([v["id"] for v in vs], combo))
            for combo in itertools.product(*[[o["at"] for o in v["options"]] for v in vs])]


def solve(world, mode="normal", **kw):
    return [solve_layout(world, c, mode, **kw) for c in layouts(world)]


def problems(reports):
    """The findings that should fail a check: unreachable endings, endings that can't be
    reached in time, dead ends, and a search too big to finish."""
    out = []
    for r in reports:
        where = f"layout {r['choices']}" if r["choices"] else "world"
        if r["truncated"]:
            out.append(f"{where}: search stopped at {r['states']} positions; results are incomplete")
        for e in r["unreachable"]:
            out.append(f"{where}: ending '{e}' can't be reached")
        for e, info in r["endings"].items():
            if not info["in_time"]:
                out.append(f"{where}: ending '{e}' needs {info['turns']} turns but the deadline is {r['deadline']}")
        if r["dead_end_count"]:
            out.append(f"{where}: {r['dead_end_count']} dead ends (no ending possible), e.g. after: "
                       + ", ".join(r["dead_ends"][0]["route"]))
    return out


def summary(world, reports):
    """Human-readable report."""
    lines = [f"{world['title']} ({world['id']})"]
    for r in reports:
        lines.append("")
        lines.append(f"  layout {r['choices'] or '(only one)'}, mode {r['mode']}: "
                     f"{r['states']} positions{' (INCOMPLETE)' if r['truncated'] else ''}, deadline {r['deadline']}")
        for e, info in sorted(r["endings"].items(), key=lambda kv: kv[1]["turns"]):
            flag = "" if info["in_time"] else "  <-- too slow for the deadline"
            lines.append(f"    {e:16} {info['turns']:3} turns: {', '.join(info['route'])}{flag}")
        for e in r["unreachable"]:
            lines.append(f"    {e:16} UNREACHABLE")
        if r["dead_end_count"]:
            lines.append(f"    DEAD ENDS: {r['dead_end_count']}, e.g. after: {', '.join(r['dead_ends'][0]['route'])}")
        if r["trap_count"]:
            lines.append(f"    traps (only bad endings left): {r['trap_count']}, e.g. after: {', '.join(r['traps'][0]['route'])}")
        if r["closes"]:
            lines.append("    moves that close off an ending for good:")
            for text, es in list(r["closes"].items())[:12]:
                lines.append(f"      {text:28} -> {', '.join(es)}")
    return "\n".join(lines)


def modes(world):
    return list(modes_for(world))
