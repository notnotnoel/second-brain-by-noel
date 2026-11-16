"""The helpers' model. Four ways to run, set by llm.provider in config.json:

  local        a local OpenAI-compatible server (oMLX, LM Studio, Ollama). Nothing leaves the Mac.
  claude-code  the Claude Code CLI you already sign in to ("claude -p"). Uses your Claude plan.
  anthropic    the Anthropic API with your own key (ANTHROPIC_API_KEY or llm.anthropic_api_key).
  none         rules only. Every helper still works, with plainer summaries and no new links.

"auto" (the default) picks the first that works, in that order.
Cloud providers never receive notes scoped "private", and stop after llm.cloud_daily_limit calls a day.
Every model line is still checked against the source text before it is kept (util.grounded)."""
from __future__ import annotations
import datetime, json, os, re, shutil, subprocess, tempfile, time, urllib.error, urllib.request
from . import config

_cache = {"models": None, "at": 0.0, "qvec": {}, "prov": None, "prov_at": 0.0}
CLOUD = ("claude-code", "anthropic")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _req(path: str, payload=None, timeout=None):
    cfg = config.load()["llm"]
    url = cfg["base_url"].rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout or cfg.get("timeout_sec", 180)) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def models(refresh: bool = False):
    """Model ids the server offers ([] when it is unreachable)."""
    if not refresh and _cache["models"] is not None and time.time() - _cache["at"] < 60:
        return _cache["models"]
    try:
        data = _req("/models", timeout=4)
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception:
        ids = []
    _cache["models"], _cache["at"] = ids, time.time()
    return ids


def last_error() -> str:
    prov = provider()
    if prov == "claude-code":
        return "" if claude_cli() else "Claude Code CLI not found"
    if prov == "anthropic":
        return "" if _anthropic_key() else "no ANTHROPIC_API_KEY"
    try:
        _req("/models", timeout=4)
        return ""
    except urllib.error.HTTPError as e:
        return "HTTP %s (check the API key in config.json)" % e.code if e.code in (401, 403) else "HTTP %s" % e.code
    except Exception as e:
        return str(e)


def _anthropic_key() -> str:
    cfg = config.load()["llm"]
    return os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key") or ""


def claude_cli():
    """Path to the Claude Code CLI, or None. launchd has a short PATH, so look in the usual places too."""
    want = config.load()["llm"].get("claude_cli") or "claude"
    found = shutil.which(want)
    if found:
        return found
    for c in ("~/.claude/local/claude", "~/.local/bin/claude", "/opt/homebrew/bin/claude",
              "/usr/local/bin/claude", "~/.npm-global/bin/claude"):
        c = os.path.expanduser(c)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def provider(refresh: bool = False) -> str:
    """The provider in use now: local, claude-code, anthropic or none."""
    want = (config.load()["llm"].get("provider") or "auto").lower()
    if want in ("local", "claude-code", "anthropic", "none"):
        return want
    if not refresh and _cache["prov"] and time.time() - _cache["prov_at"] < 60:
        return _cache["prov"]
    if models(refresh):
        p = "local"
    elif claude_cli():
        p = "claude-code"
    elif _anthropic_key():
        p = "anthropic"
    else:
        p = "none"
    _cache["prov"], _cache["prov_at"] = p, time.time()
    return p


def where() -> str:
    return {"local": "on this Mac", "claude-code": "Claude Code (your Claude plan)",
            "anthropic": "Anthropic API (your key)", "none": "rules only"}[provider()]


def _calls_file():
    return config.logs_dir() / "cloud_calls.json"


def _cloud_budget_ok() -> bool:
    limit = int(config.load()["llm"].get("cloud_daily_limit", 300))
    today = datetime.date.today().isoformat()
    try:
        d = json.loads(_calls_file().read_text())
    except Exception:
        d = {}
    n = d.get(today, 0)
    if n >= limit:
        return False
    _calls_file().write_text(json.dumps({today: n + 1}))
    return True


def cloud_calls_today() -> int:
    try:
        return json.loads(_calls_file().read_text()).get(datetime.date.today().isoformat(), 0)
    except Exception:
        return 0


def _is_embed(mid: str) -> bool:
    return bool(re.search(r"embed|bge|bert|e5-|gte-|minilm|rerank", mid.lower()))


def _size(mid: str) -> float:
    m = re.findall(r"(\d+(?:\.\d+)?)\s*b(?![a-z])", mid.lower())
    return float(m[0]) if m else 8.0


def pick(role: str):
    """Resolve the model for 'fast' (filer) or 'deep' (librarian) on the provider in use."""
    cfg = config.load()["llm"]
    prov = provider()
    if prov == "none":
        return None
    if prov == "anthropic":
        return cfg.get("anthropic_model_" + role) or ("claude-haiku-5-5" if role == "fast" else "claude-sonnet-5-5")
    if prov == "claude-code":
        return cfg.get("claude_model_" + role) or ("haiku" if role == "fast" else "sonnet")
    want = cfg.get("model_" + role, "auto")
    ids = models()
    if want and want != "auto":
        return want
    chat = [m for m in ids if not _is_embed(m)]
    if not chat:
        return None
    qwen = [m for m in chat if "qwen" in m.lower()] or chat
    if role == "fast":
        ok = [m for m in qwen if 4 <= _size(m) <= 14] or qwen
        return sorted(ok, key=_size)[0]
    ok = [m for m in qwen if _size(m) <= 30] or qwen
    return sorted(ok, key=_size)[-1]


def embed_model():
    if provider() != "local":
        return None              # vector search needs a local embedding model; keyword search still works
    cfg = config.load()["llm"]
    want = cfg.get("embed_model", "auto")
    if want == "off":
        return None
    if want and want != "auto":
        return want
    em = [m for m in models() if _is_embed(m) and "rerank" not in m.lower()]
    return em[0] if em else None


def available() -> bool:
    """True when some model can take a call now."""
    prov = provider()
    if prov == "local":
        return bool(models()) and pick("fast") is not None
    if prov == "claude-code":
        return claude_cli() is not None
    if prov == "anthropic":
        return bool(_anthropic_key())
    return False


def status() -> dict:
    prov = provider()
    ok = available()
    return {"provider": prov, "where": where(), "reachable": ok,
            "fast": pick("fast") if ok else None, "deep": pick("deep") if ok else None,
            "embed": embed_model() if ok else None, "base_url": config.load()["llm"]["base_url"],
            "cloud_calls_today": cloud_calls_today() if prov in CLOUD else 0}


def _extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _finish(text: str, want_json: bool):
    text = text or ""
    if not want_json:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or None
    obj = _extract_json(text)
    return obj if isinstance(obj, dict) else None


def _anthropic(system, user, model, want_json, max_tokens):
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.1, "system": system,
            "messages": [{"role": "user", "content": user + ("\n\nReply with one JSON object only." if want_json else "")}]}
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(), method="POST", headers={
        "content-type": "application/json", "x-api-key": _anthropic_key(), "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=config.load()["llm"].get("timeout_sec", 180)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _finish(text, want_json)


def _claude_code(system, user, model, want_json, max_tokens):
    cli = claude_cli()
    if not cli:
        return None
    prompt = "%s\n\n%s%s" % (system, user, "\n\nReply with one JSON object only. No tools, no prose." if want_json else "")
    try:
        # Run in an empty folder so Claude Code does not read any project files.
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run([cli, "-p", "--model", model, "--output-format", "json"], input=prompt,
                               capture_output=True, text=True, cwd=tmp,
                               timeout=config.load()["llm"].get("timeout_sec", 180))
    except Exception:
        return None
    out = p.stdout.strip()
    try:
        out = json.loads(out).get("result", out)
    except Exception:
        pass
    return _finish(out if isinstance(out, str) else "", want_json)


def blocked(scope: str) -> bool:
    """True when text of this scope must not go to the model in use (private text, cloud provider)."""
    return provider() in CLOUD and scope == "private" and not config.load()["llm"].get("cloud_private")


def chat(system: str, user: str, role: str = "fast", want_json: bool = True, max_tokens: int = 900,
         scope: str = ""):
    """One call to the helpers' model. Returns a dict (want_json) or text; None on any failure.
    scope: the scope of the text being sent. Private text never goes to a cloud provider."""
    model = pick(role)
    if not model:
        return None
    prov = provider()
    if prov in CLOUD:
        if blocked(scope):
            return None
        if not _cloud_budget_ok():
            return None
        fn = _anthropic if prov == "anthropic" else _claude_code
        return fn(system, user, model, want_json, max_tokens)
    base = {"model": model, "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    attempts = [dict(base, chat_template_kwargs={"enable_thinking": False},
                     **({"response_format": {"type": "json_object"}} if want_json else {})), base]
    for payload in attempts:
        try:
            data = _req("/chat/completions", payload)
            text = data["choices"][0]["message"].get("content") or ""
        except Exception:
            continue
        if not want_json:
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        obj = _extract_json(text)
        if isinstance(obj, dict):
            return obj
    return None


def embed(texts):
    model = embed_model()
    if not model:
        return None
    try:
        data = _req("/embeddings", {"model": model, "input": texts}, timeout=120)
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
    except Exception:
        return None


def embed_cached_query(text: str):
    if text in _cache["qvec"]:
        return _cache["qvec"][text]
    v = embed([text])
    vec = v[0] if v else None
    if len(_cache["qvec"]) > 200:
        _cache["qvec"].clear()
    _cache["qvec"][text] = vec
    return vec
