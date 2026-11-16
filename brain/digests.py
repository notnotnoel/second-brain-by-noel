"""What the local model has read out of each Obsidian note (summary, tags, facts).
Kept beside the index, never inside the owner's notes."""
from __future__ import annotations
import json, threading
from . import config

_lock = threading.Lock()


def _path():
    return config.home() / "digests.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(d: dict) -> None:
    with _lock:
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_path())
