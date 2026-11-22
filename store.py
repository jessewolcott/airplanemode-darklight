"""Small persistent data: the leaderboard and save codes.

The box stores to eMMC or SD cards, so this stays small and writes rarely: one JSON file per
kind in DATA_DIR (a named volume on the box), capped in size, written atomically. With no
DATA_DIR, or one that isn't writable, everything still works and just lives in memory.
"""
import json
import os
import secrets
import time
from pathlib import Path

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
SAVE_TTL = 30 * 24 * 3600
MAX_SAVES = 2000
MAX_SCORES = 3000


class JsonFile:
    def __init__(self, folder, name, default):
        self.path = Path(folder) / name if folder else None
        self.data = default
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass   # a damaged file shouldn't stop the game; it's rebuilt on the next write

    def save(self):
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            self.path = None   # read-only or full: carry on in memory


def usable_dir(path):
    if not path:
        return None
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        probe = Path(path) / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return path
    except OSError:
        return None


class Store:
    def __init__(self, folder):
        folder = usable_dir(folder)
        self.persistent = folder is not None
        self.saves = JsonFile(folder, "saves.json", {})
        self.scores = JsonFile(folder, "scores.json", [])

    # ---- save codes ----
    def put_save(self, snapshot, now=None):
        now = now or time.time()
        saves = self.saves.data
        for code in [c for c, v in saves.items() if now - v["at"] > SAVE_TTL]:
            del saves[code]
        while len(saves) >= MAX_SAVES:
            del saves[min(saves, key=lambda c: saves[c]["at"])]
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        while code in saves:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        saves[code] = {"at": now, "save": snapshot}
        self.saves.save()
        return code

    def get_save(self, code, now=None):
        entry = self.saves.data.get(code.strip().upper().replace("-", ""))
        if not entry or (now or time.time()) - entry["at"] > SAVE_TTL:
            return None
        return entry["save"]

    # ---- leaderboard ----
    def add_score(self, entry):
        self.scores.data.append(entry)
        del self.scores.data[:-MAX_SCORES]
        self.scores.save()

    def leaderboard(self, world, good_tones, limit=10):
        rows = [r for r in self.scores.data if r["world"] == world]
        fastest = {}
        for mode in ("relaxed", "normal", "hard"):
            best = {}
            for r in rows:
                if r["mode"] == mode and r["tone"] in good_tones[:1]:
                    if r["name"] not in best or r["turns"] < best[r["name"]]["turns"]:
                        best[r["name"]] = r
            fastest[mode] = sorted(best.values(), key=lambda r: (r["turns"], r["at"]))[:limit]
        found = {}
        for r in rows:
            found.setdefault(r["name"], set()).add(r["ending"])
        collectors = sorted(({"name": n, "endings": len(e)} for n, e in found.items()),
                            key=lambda r: -r["endings"])[:limit]
        return {"fastest": fastest, "collectors": collectors}
