"""Reading and writing notes. The vault (plain Markdown) is the source of truth."""
from __future__ import annotations
import re
from pathlib import Path
from . import config, util

FOLDERS = ["00-Core", "01-Inbox", "10-Entities", "20-Events", "30-Sources", "90-Agent", "99-Archive"]
ENTITY_TYPES = {"person": "people", "project": "projects", "org": "orgs", "topic": "topics",
                "tool": "tools", "place": "places", "goal": "goals", "preference": "topics"}
EVENT_TYPES = {"event", "decision"}
KEY_ORDER = ["id", "type", "title", "aliases", "status", "scope", "summary", "date", "source",
             "updated", "pin", "last_used", "use_count_90d", "inbound_links", "archived_on"]
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
SKIP_DIRS = {".git", ".obsidian", ".trash"}
OWN_SKIP = {"Inbox", "Cards"}          # inside the helpers' own Obsidian folder: not indexed
OWN_TYPES = EVENT_TYPES | {"source"}


# ---------- frontmatter (small YAML subset, no dependencies) ----------

def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1].replace('\\"', '"')
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _split_list(inner: str):
    items, cur, quote = [], "", None
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur)
    return [_scalar(i) for i in items if i.strip()]


def parse(text: str):
    """Return (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        return {}, text
    meta, key = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"\s*-\s+(.*)$", line)
        if item and key and isinstance(meta.get(key), list):
            meta[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val == "":
            meta[key] = []
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = _split_list(val[1:-1])
        else:
            meta[key] = _scalar(val)
    for k, v in list(meta.items()):
        if v == [] and k not in ("aliases",):
            meta[k] = ""
    return meta, text[m.end():]


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\n", " ").strip()
    if s == "" or re.search(r"[:#\[\]{},\"']|^[-?&*!|>%@`]", s) or s in ("true", "false") or re.fullmatch(r"-?\d+", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def dump(meta: dict, body: str) -> str:
    keys = [k for k in KEY_ORDER if k in meta] + sorted(k for k in meta if k not in KEY_ORDER)
    lines = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, list):
            lines.append("%s: [%s]" % (k, ", ".join(_fmt(i) for i in v)))
        else:
            lines.append("%s: %s" % (k, _fmt(v)))
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


# ---------- notes ----------

class Note:
    """origin 'brain' = a file in ~/SecondBrain/vault (cards, core files).
    origin 'obs' = a file in the Obsidian vault. Only the helpers' own folder there is writable;
    every other Obsidian note is the owner's and is read-only to this code."""

    def __init__(self, path: Path, meta: dict, body: str, origin: str = "brain", rel: str = ""):
        self.path, self.meta, self.body = path, meta, body
        self.origin, self.rel, self._id = origin, rel, None

    @property
    def own(self) -> bool:
        return self.origin == "obs" and self.rel.startswith("obs/%s/" % own_folder())

    @property
    def writable(self) -> bool:
        return self.origin == "brain" or self.own

    @property
    def is_doc(self) -> bool:
        return self.origin == "obs" and not (self.own and str(self.meta.get("type")) in OWN_TYPES)

    @property
    def id(self) -> str:
        if self._id:
            return self._id
        if self.is_doc:
            return util.slugify(self.path.stem)
        return str(self.meta.get("id") or self.path.stem)

    @property
    def type(self) -> str:
        if self.is_doc:
            return "doc"
        return str(self.meta.get("type") or "note")

    @property
    def scope(self) -> str:
        if self.is_doc:
            parts = self.rel.split("/")
            by = (config.load().get("obsidian") or {}).get("scope_by_folder") or {}
            return str(by.get(parts[1], "personal")) if len(parts) > 2 else "personal"
        return str(self.meta.get("scope") or "personal")

    @property
    def tags(self):
        t = self.meta.get("tags") or []
        return [str(x) for x in (t if isinstance(t, list) else [t]) if str(x).strip()]

    def headings(self, limit: int = 12):
        return [m.strip() for m in re.findall(r"^#{1,3}\s+(.+)$", self.body, re.M)][:limit]

    def first_line(self, n: int = 140) -> str:
        for l in self.body.splitlines():
            l = l.strip()
            if l and not l.startswith(("#", "|", ">", "---", "![", "```", "Back to", "Up:", "← Back", "Path:", "Part of")):
                return re.sub(r"\s+", " ", re.sub(r"[*_`\[\]]", "", l))[:n]
        return ""

    @property
    def title(self) -> str:
        if self.is_doc:
            return self.path.stem
        if self.meta.get("title"):
            return str(self.meta["title"])
        m = re.search(r"^#\s+(.+)$", self.body, re.M)
        return m.group(1).strip() if m else self.path.stem

    @property
    def aliases(self):
        a = self.meta.get("aliases") or []
        return [str(x) for x in (a if isinstance(a, list) else [a])]

    def links(self):
        return sorted({m.strip() for m in LINK_RE.findall(self.body)})

    def save(self) -> None:
        if not self.writable:
            raise PermissionError("Refusing to change the owner's Obsidian note: %s" % self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(dump(self.meta, self.body), encoding="utf-8")

    # --- sections: "## Heading" blocks ---
    def sections(self):
        parts = re.split(r"^(## .+)$", self.body, flags=re.M)
        out = [("", parts[0])]
        for i in range(1, len(parts), 2):
            out.append((parts[i][3:].strip(), parts[i + 1]))
        return out

    def section(self, name: str) -> str:
        for h, text in self.sections():
            if h.lower() == name.lower():
                return text.strip()
        return ""

    def set_section(self, name: str, text: str) -> None:
        secs, done = self.sections(), False
        for i, (h, _) in enumerate(secs):
            if h.lower() == name.lower():
                secs[i] = (h, "\n" + text.strip() + "\n\n")
                done = True
        if not done:
            secs.append((name, "\n" + text.strip() + "\n\n"))
        self.body = "".join((("## %s" % h) if h else "") + t for h, t in secs)


def root() -> Path:
    return config.vault()


# ---------- the Obsidian vault (details, source of truth) ----------

def obs_root():
    p = (config.load().get("obsidian") or {}).get("path")
    return Path(p).expanduser() if p else None


def own_folder() -> str:
    return str((config.load().get("obsidian") or {}).get("folder") or "Second Brain")


def own_root():
    r = obs_root()
    return (r / own_folder()) if r else None


def obs_available() -> bool:
    r = obs_root()
    if not r:
        return False
    try:
        next(iter(r.iterdir()), None)
        return True
    except Exception:
        return False


def abs_path(rel: str) -> Path:
    if rel.startswith("obs/"):
        return obs_root() / rel[4:]
    return root() / rel


def show_path(rel: str) -> str:
    return rel[4:] if rel.startswith("obs/") else rel


def is_archive(rel: str) -> bool:
    return rel.startswith("99-Archive") or rel.startswith("obs/%s/Archive/" % own_folder())


def top(n: Note) -> str:
    """First folder for brain notes; 'log' / 'archive' for the helpers' Obsidian notes; 'obs' for the owner's."""
    if n.origin == "brain":
        return n.rel.split("/")[0]
    if n.own:
        return "99-Archive" if is_archive(n.rel) else "log"
    return "obs"


def events_dir(kind: str, date: str) -> Path:
    o = own_root()
    if o:
        return o / "Log" / date[:4]
    return root() / ("30-Sources" if kind == "source" else "20-Events/" + date[:4])


def archive_dir(n: Note) -> Path:
    return (own_root() / "Archive") if n.origin == "obs" else (root() / "99-Archive")


def core_file(name: str) -> Path:
    """review / report live in Obsidian when it is connected, so they can be read and ticked there."""
    o = own_root()
    if o and obs_available():
        return o / {"review": "Review.md", "report": "Librarian report.md"}[name]
    return root() / "00-Core" / {"review": "review.md", "report": "librarian-report.md"}[name]


def load(path: Path, origin: str = "brain", rel: str = "") -> Note:
    meta, body = parse(path.read_text(encoding="utf-8", errors="replace"))
    if not rel:
        rel = path.relative_to(root()).as_posix() if origin == "brain" else ""
    return Note(path, meta, body, origin, rel)


def walk(include_archive: bool = True, include_obs: bool = True):
    """Yield (path, origin, rel). rel is the stored path: vault-relative, with 'obs/' for Obsidian notes."""
    r = root()
    if r.exists():
        for p in sorted(r.rglob("*.md")):
            rel = p.relative_to(r).parts
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel):
                continue
            if rel[0] == "01-Inbox":
                continue
            if not include_archive and rel[0] == "99-Archive":
                continue
            yield p, "brain", "/".join(rel)
    o = obs_root()
    if not (include_obs and o and obs_available()):
        return
    cfg = config.load().get("obsidian") or {}
    skip = set(cfg.get("exclude") or [])
    own = own_folder()
    for p in sorted(o.rglob("*.md")):
        rel = p.relative_to(o).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel):
            continue
        posix = "/".join(rel)
        if any(posix == s or posix.startswith(s.rstrip("/") + "/") for s in skip):
            continue
        if rel[0] == own:
            if len(rel) < 3 or rel[1] in OWN_SKIP:   # Review.md, report, Inbox, card mirrors
                continue
            if not include_archive and rel[1] == "Archive":
                continue
        yield p, "obs", "obs/" + posix


def doc_id(stem: str, taken) -> str:
    base = util.slugify(stem)
    if base in taken:
        base += "-note"
    return unique_id(base, taken)


def all_notes(include_archive: bool = True, include_obs: bool = True):
    out, taken = [], set()
    for p, origin, rel in walk(include_archive, include_obs):
        try:
            n = load(p, origin, rel)
        except Exception:
            continue
        if n.is_doc:
            n._id = doc_id(p.stem, taken)
        taken.add(n.id)
        out.append(n)
    return out


def find(note_id: str):
    for n in all_notes():
        if n.id == note_id:
            return n
    return None


def unique_id(base: str, taken) -> str:
    if base not in taken:
        return base
    i = 2
    while "%s-%d" % (base, i) in taken:
        i += 1
    return "%s-%d" % (base, i)


def entity_path(note_id: str, typ: str) -> Path:
    return root() / "10-Entities" / ENTITY_TYPES.get(typ, "topics") / (note_id + ".md")


CARD_BODY = """## Now
{now}

## Key facts
{facts}

## Timeline

## Links
"""


def create_card(title: str, typ: str = "topic", aliases=None, summary: str = "", scope: str = "personal",
                facts=None, now_text: str = "", taken=None) -> Note:
    taken = taken if taken is not None else {n.id for n in all_notes()}
    nid = unique_id(util.slugify(title), taken)
    typ = typ if typ in ENTITY_TYPES else "topic"
    meta = {"id": nid, "type": typ, "title": title, "aliases": list(aliases or []), "status": "active",
            "scope": scope, "summary": summary or title, "updated": util.today(), "pin": False}
    body = CARD_BODY.format(now=now_text or summary or "", facts="\n".join("- " + f for f in (facts or [])))
    n = Note(entity_path(nid, typ), meta, body)
    n.rel = n.path.relative_to(root()).as_posix()
    n.save()
    return n


def init_vault() -> list:
    """Create folders and core files. Never overwrites anything that exists."""
    r, made = root(), []
    for f in FOLDERS:
        (r / f).mkdir(parents=True, exist_ok=True)
    for sub in sorted(set(ENTITY_TYPES.values())):
        (r / "10-Entities" / sub).mkdir(parents=True, exist_ok=True)
    core = {
        "00-Core/brief.md": "# Brief\n\n(The librarian rebuilds this file every night.)\n",
        "00-Core/preferences.md": "---\nid: preferences\ntype: preference\ntitle: Preferences\nstatus: active\nscope: personal\npin: true\nsummary: Standing preferences every agent should follow\n---\n# Preferences\n\nWrite one preference per line. Agents receive these in every brief.\n\n- \n",
        "00-Core/review.md": "# Review queue\n\nTick a box to approve. The librarian applies ticked items on its next run.\n\n## New cards proposed\n\n## Merge proposals\n\n## Ready to delete\n",
        "00-Core/librarian-report.md": "# Librarian report\n\n(No runs yet.)\n",
        "01-Inbox/README.txt": "Drop any .md or .txt file here. The filer turns it into a linked note.\n(This README is ignored.)\n",
    }
    for rel, text in core.items():
        p = r / rel
        if not p.exists():
            p.write_text(text, encoding="utf-8")
            made.append(rel)
    return made
