from __future__ import annotations
import datetime as dt, hashlib, re, unicodedata


def now() -> dt.datetime:
    return dt.datetime.now()


def today() -> str:
    return now().strftime("%Y-%m-%d")


def stamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%S")


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (text[:max_len].rstrip("-")) or "note"


def norm(text: str) -> str:
    """Normalise a name for alias matching: 'Acme Co.' -> 'acme co'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def squash(text: str) -> str:
    """'acme co' and 'acmeco' both -> 'acmeco' (catches spacing variants)."""
    return norm(text).replace(" ", "")


def tokens(text: str) -> int:
    """Cheap token estimate (about 4 characters per token)."""
    return (len(text) + 3) // 4


def trim_tokens(text: str, cap: int) -> str:
    if tokens(text) <= cap:
        return text
    cut = text[: cap * 4]
    nl = cut.rfind("\n")
    if nl > cap * 2:
        cut = cut[:nl]
    return cut.rstrip() + "\n… (trimmed; call expand for more)"


def clip(text: str, n: int) -> str:
    """Shorten to about n characters at a word boundary, never mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-.") + "…"


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


def parse_date(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def days_since(s) -> int | None:
    d = parse_date(s)
    return None if d is None else (dt.date.today() - d).days


_STOP = set("the a an of to in on for and or is are was were be been by with at from as it its this that his her "
            "their he she they has have had not no will would can could should than then also into over under "
            "about after before during which who whom what when where why how".split())


def grounded(claim: str, source: str, ratio: float = 0.75) -> bool:
    """True when a model-written sentence is supported by the source text: every number and every
    capitalised name in it appears in the source, and most of its content words do too."""
    claim = str(claim or "")
    src = " " + norm(source) + " "
    src_sq = src.replace(" ", "")
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.'&+-]*", claim)
    if not words:
        return False
    content, hits = 0, 0
    for i, w in enumerate(words):
        n = norm(w)
        if not n or n in _STOP:
            continue
        present = (" " + n + " ") in src or (len(n) > 5 and n.replace(" ", "") in src_sq)
        if not present and len(n) > 4 and n.endswith("s"):
            present = (" " + n[:-1] + " ") in src
        strict = bool(re.search(r"\d", w)) or (w[0].isupper() and i > 0)
        if strict and not present:
            return False
        content += 1
        hits += 1 if present else 0
    return content > 0 and hits / content >= ratio
