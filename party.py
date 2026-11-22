"""Party mode: one game on a shared screen, everyone on the box's Wi-Fi steering it from a phone.

Each round, players propose commands (or vote for one already proposed). When the round ends,
the top proposal is played as a normal turn. Rounds are settled lazily, whenever anyone polls,
so there's no background task to manage. Everything lives in memory with its game session.
"""
import asyncio
import secrets
import time

ROUND_SECONDS = 20
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ"
MAX_PLAYERS = 40
MAX_PROPOSALS = 12
LOG_KEEP = 60


def clean_name(name, fallback="Player"):
    name = " ".join(str(name or "").split())[:20]
    return "".join(ch for ch in name if ch.isprintable()) or fallback


class Party:
    def __init__(self, session_id, round_seconds=ROUND_SECONDS):
        self.session_id = session_id
        self.round_seconds = round_seconds
        self.players = {}           # player id -> name
        self.proposals = []         # [{id, text, key, by, voters: set}]
        self.round_ends = None      # set when the first proposal arrives
        self.round_no = 1
        self.log = []               # [{n, said, text, by, ending}]
        self.lock = asyncio.Lock()
        self.last = time.monotonic()

    def join(self, name):
        if len(self.players) >= MAX_PLAYERS:
            raise ValueError("This party is full.")
        pid = secrets.token_hex(8)
        self.players[pid] = clean_name(name, f"Player {len(self.players) + 1}")
        return pid

    def propose(self, pid, text, now=None):
        if pid not in self.players:
            raise KeyError("Join the party first.")
        text = " ".join(str(text).split())[:120]
        if not text:
            raise ValueError("Say something to do.")
        key = text.lower().strip(" .!?")
        for p in self.proposals:
            p["voters"].discard(pid)
        existing = next((p for p in self.proposals if p["key"] == key), None)
        if existing:
            existing["voters"].add(pid)
            return existing["id"]
        if len(self.proposals) >= MAX_PROPOSALS:
            raise ValueError("Plenty of ideas this round. Vote for one.")
        prop = {"id": secrets.token_hex(4), "text": text, "key": key, "by": self.players[pid], "voters": {pid}}
        self.proposals.append(prop)
        if self.round_ends is None:
            self.round_ends = (now or time.monotonic()) + self.round_seconds
        return prop["id"]

    def vote(self, pid, proposal_id):
        if pid not in self.players:
            raise KeyError("Join the party first.")
        target = next((p for p in self.proposals if p["id"] == proposal_id), None)
        if not target:
            raise ValueError("That idea has already been played.")
        for p in self.proposals:
            p["voters"].discard(pid)
        target["voters"].add(pid)

    def winner(self):
        live = [p for p in self.proposals if p["voters"]]
        if not live:
            return None
        return max(live, key=lambda p: (len(p["voters"]), -self.proposals.index(p)))

    def due(self, now=None):
        return self.round_ends is not None and (now or time.monotonic()) >= self.round_ends

    def close_round(self):
        win = self.winner()
        self.proposals, self.round_ends = [], None
        self.round_no += 1
        return win

    def record(self, said, by, result):
        self.log.append({"n": (self.log[-1]["n"] + 1) if self.log else 1, "said": said, "by": by,
                         "text": result.get("text", ""), "beats": result.get("beats") or [],
                         "ending": result.get("ending_text"), "status": result.get("status")})
        del self.log[:-LOG_KEEP]

    def view(self, pid=None, since=0, now=None):
        now = now or time.monotonic()
        return {
            "round": self.round_no,
            "ends_in": max(0, round(self.round_ends - now, 1)) if self.round_ends else None,
            "round_seconds": self.round_seconds,
            "players": len(self.players),
            "you": self.players.get(pid),
            "proposals": [{"id": p["id"], "text": p["text"], "by": p["by"], "votes": len(p["voters"]),
                           "mine": pid in p["voters"]} for p in self.proposals],
            "log": [e for e in self.log if e["n"] > since],
        }


def new_code(taken):
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    while code in taken:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    return code
