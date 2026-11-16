"""Activity log. The dashboard animates these; the librarian reads usage from them."""
from __future__ import annotations
import json, threading
from . import config, util

_lock = threading.Lock()


def _path(name):
    return config.logs_dir() / name


def emit(kind: str, actor: str, msg: str, notes=None, **extra) -> None:
    rec = {"ts": util.stamp(), "kind": kind, "actor": actor, "msg": msg, "notes": list(notes or [])}
    rec.update(extra)
    with _lock:
        with open(_path("events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read(after: int = 0, limit: int = 300):
    p = _path("events.jsonl")
    if not p.exists():
        return [], 0
    lines = p.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    if after < 0 or after > total:
        after = max(0, total - 60)
    out = []
    for i, line in enumerate(lines[after:after + limit], start=after):
        try:
            rec = json.loads(line)
            rec["n"] = i + 1
            out.append(rec)
        except Exception:
            pass
    return out, min(total, after + limit)


def use(note_id: str, kind: str = "recall") -> None:
    with _lock:
        with open(_path("usage.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": util.today(), "id": note_id, "kind": kind}) + "\n")


def usage():
    p = _path("usage.jsonl")
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out
