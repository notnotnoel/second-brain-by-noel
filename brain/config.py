from __future__ import annotations
import json, os
from pathlib import Path

DEFAULTS = {
    "vault": "",
    "llm": {
        # auto | local | claude-code | anthropic | none  (see llm.py)
        "provider": "auto",
        "base_url": "http://localhost:8000/v1",
        "api_key": "",
        "model_fast": "auto",     # filer: small Qwen (e.g. Qwen3.5 9B 4-bit)
        "model_deep": "auto",     # librarian: largest chat model that is loaded
        "embed_model": "auto",    # optional; "off" disables vector search
        "timeout_sec": 180,
        "claude_cli": "claude", "claude_model_fast": "haiku", "claude_model_deep": "sonnet",
        "anthropic_api_key": "", "anthropic_model_fast": "claude-haiku-5-5",
        "anthropic_model_deep": "claude-sonnet-5-5",
        "cloud_private": False,      # private-scoped notes never go to a cloud model
        "cloud_daily_limit": 300,    # a cap on cloud calls per day, so a big vault cannot run up a bill
    },
    "dashboard_port": 8765,
    "schedule": {"poll_sec": 10, "nightly_hour": 3, "weekly_day": 6},
    "caps": {"card_tokens": 800, "recall_tokens": 500, "expand_tokens": 500, "brief_tokens": 400},
    "lifecycle": {"dormant_days": 90, "archive_days": 365, "agent_scratch_days": 30,
                  "delete_after_archived_days": 30},
    "default_scopes": ["personal", "work"],
    # The Obsidian vault holds the details and is the source of truth. Cards and the index live here.
    "obsidian": {"path": "", "folder": "Second Brain", "exclude": [],
                 "scope_by_folder": {}, "no_auto_link": []},
    "digest": {"per_tick": 2, "max_chars": 6000},
}


def home() -> Path:
    return Path(os.environ.get("BRAIN_HOME", "~/SecondBrain")).expanduser()


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    cfg = DEFAULTS
    p = home() / "config.json"
    if p.exists():
        try:
            cfg = _merge(DEFAULTS, json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            cfg = DEFAULTS
    cfg = _merge(cfg, {})
    if not cfg["vault"]:
        cfg["vault"] = str(home() / "vault")
    cfg["vault"] = str(Path(cfg["vault"]).expanduser())
    key = os.environ.get("OMLX_API_KEY")
    if key:
        cfg["llm"]["api_key"] = key
    return cfg


def save(cfg: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    (home() / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def vault() -> Path:
    return Path(load()["vault"])


def logs_dir() -> Path:
    d = home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
