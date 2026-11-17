"""SQLite index over the vault: notes, aliases, link graph, full-text search, optional vectors.
It is disposable: `brain reindex --full` rebuilds it from the Markdown files."""
from __future__ import annotations
import functools, json, math, re, sqlite3, struct
from . import config, digests, events, util, vault

SCHEMA_VERSION = "3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, path TEXT, type TEXT, status TEXT, scope TEXT,
  title TEXT, summary TEXT, date TEXT, updated TEXT, mtime REAL, hash TEXT, tokens INT, pin INT, body TEXT,
  origin TEXT, tags TEXT, outline TEXT);
CREATE TABLE IF NOT EXISTS names(name TEXT, squashed TEXT, note_id TEXT);
CREATE INDEX IF NOT EXISTS ix_name ON names(name);
CREATE INDEX IF NOT EXISTS ix_name_sq ON names(squashed);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT, squashed TEXT, note_id TEXT);
CREATE INDEX IF NOT EXISTS ix_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS ix_squashed ON aliases(squashed);
CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, kind TEXT DEFAULT 'link', w INT DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS vectors(note_id TEXT PRIMARY KEY, hash TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS secs(note_id TEXT, ord INT, heading TEXT, path TEXT, level INT, tokens INT,
  start INT, end INT);
CREATE INDEX IF NOT EXISTS ix_secs ON secs(note_id, ord);
"""


def connect() -> sqlite3.Connection:
    config.home().mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(config.home() / "index.db"), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    r = db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    if not r or r["v"] != SCHEMA_VERSION:      # the index is disposable: rebuild it on a layout change
        for t in ("notes", "aliases", "edges", "names", "fts", "secs", "sfts"):
            db.execute("DROP TABLE IF EXISTS %s" % t)
        db.execute("INSERT OR REPLACE INTO meta VALUES('schema',?)", (SCHEMA_VERSION,))
    db.executescript(SCHEMA)
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, title, body)")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sfts USING fts5(note_id UNINDEXED, ord UNINDEXED, heading, body, "
            "tokenize='porter unicode61')")
        db.execute("INSERT OR REPLACE INTO meta VALUES('fts','1')")
    except sqlite3.OperationalError:
        db.execute("INSERT OR REPLACE INTO meta VALUES('fts','0')")
    db.commit()
    return db


def has_fts(db) -> bool:
    r = db.execute("SELECT v FROM meta WHERE k='fts'").fetchone()
    return bool(r and r["v"] == "1")


def _drop(db, nid):
    db.execute("DELETE FROM notes WHERE id=?", (nid,))
    db.execute("DELETE FROM aliases WHERE note_id=?", (nid,))
    db.execute("DELETE FROM names WHERE note_id=?", (nid,))
    db.execute("DELETE FROM edges WHERE src=?", (nid,))
    db.execute("DELETE FROM secs WHERE note_id=?", (nid,))
    if has_fts(db):
        db.execute("DELETE FROM fts WHERE id=?", (nid,))
        db.execute("DELETE FROM sfts WHERE note_id=?", (nid,))


_last_scan = 0.0


def reindex(full: bool = False, quiet: bool = False, max_age: float = 0.0) -> dict:
    """Index new/changed notes, drop removed ones. Returns counts.
    max_age: skip the scan if one finished this many seconds ago (reads use it; sync and write do not)."""
    global _last_scan
    import time
    if max_age and not full and time.time() - _last_scan < max_age:
        return {"added": [], "changed": [], "removed": []}
    db = connect()
    if full:
        for t in ("notes", "aliases", "edges", "names", "secs"):
            db.execute("DELETE FROM %s" % t)
        if has_fts(db):
            db.execute("DELETE FROM fts")
            db.execute("DELETE FROM sfts")
    known = {r["path"]: (r["id"], r["mtime"]) for r in db.execute("SELECT id, path, mtime FROM notes")}
    seen, added, changed = set(), [], []
    obs_on = vault.obs_root() is not None
    if obs_on and not vault.obs_available():
        # The Obsidian folder is briefly unreachable (iCloud, permissions). Keep what is indexed.
        seen.update(rel for rel in known if rel.startswith("obs/"))
        _warn_once("Cannot read the Obsidian vault at %s. Using the last index until it is back." % vault.obs_root())
    dg = digests.load()
    for p, origin, rel in vault.walk():
        seen.add(rel)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if rel in known and abs(known[rel][1] - mtime) < 1e-6:
            continue
        try:
            n = vault.load(p, origin, rel)
        except Exception:
            continue
        if n.is_doc:
            if rel in known:
                n._id = known[rel][0]
            else:
                taken = {r["id"] for r in db.execute("SELECT id FROM notes WHERE path != ?", (rel,))}
                n._id = vault.doc_id(p.stem, taken)
        if rel in known:
            _drop(db, known[rel][0])
            changed.append(n.id)
        else:
            added.append(n.id)
        _drop(db, n.id)
        h = util.sha(n.body)
        d = dg.get(rel) or {}
        fresh = d.get("hash") == h
        if vault.is_archive(rel):
            status = "archived"
        else:
            status = str(n.meta.get("status") or "active") if n.writable else "active"
        summary = str(n.meta.get("summary") or "") if not n.is_doc else ""
        if n.is_doc:
            summary = (d.get("summary") if fresh else "") or n.first_line()
        tags = list(dict.fromkeys(n.tags + (d.get("tags") or [] if fresh else [])))
        date = str(n.meta.get("date") or "")
        updated = str(n.meta.get("updated") or "")
        if n.is_doc and not updated:
            updated = util.dt.date.fromtimestamp(mtime).isoformat()
        db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (n.id, rel, n.type, status, n.scope, n.title, summary, date, updated, mtime, h,
                    util.tokens(n.body), 1 if (n.meta.get("pin") and n.writable) else 0, n.body,
                    origin, " ".join(tags), " · ".join(n.headings())))
        names = {n.id, n.title, n.path.stem} | (set(n.aliases) if n.writable else set())
        if n.type in vault.ENTITY_TYPES and origin == "brain":
            for a in names:
                if util.norm(a):
                    db.execute("INSERT INTO aliases VALUES(?,?,?)", (util.norm(a), util.squash(a), n.id))
        else:
            for a in names:
                if util.norm(a):
                    db.execute("INSERT INTO names VALUES(?,?,?)", (util.norm(a), util.squash(a), n.id))
        for dst in ([] if rel.startswith("00-Core") else n.links()):
            db.execute("INSERT INTO edges VALUES(?,?,'link',1)", (n.id, dst))
        if has_fts(db):
            db.execute("INSERT INTO fts VALUES(?,?,?)",
                       (n.id, " ".join([n.title] + (n.aliases if n.writable else []) + tags), n.body))
            if n.is_doc:          # one search row per heading section, so recall can point at the section
                for i, sec in enumerate(vault.split_sections(n.body)):
                    text = n.body[sec["start"]:sec["end"]]
                    if len(text.strip()) < 25:
                        continue
                    db.execute("INSERT INTO secs VALUES(?,?,?,?,?,?,?,?)",
                               (n.id, i, sec["heading"], sec["path"], sec["level"], util.tokens(text),
                                sec["start"], sec["end"]))
                    db.execute("INSERT INTO sfts VALUES(?,?,?,?)", (n.id, i, sec["path"], text))
    if obs_on and not any(r.startswith("obs/") for r in seen) and any(r.startswith("obs/") for r in known):
        # The folder lists but no note can be read (macOS privacy block). Keep what is indexed.
        seen.update(rel for rel in known if rel.startswith("obs/"))
        _warn_once("Cannot read the notes in the Obsidian vault at %s. Using the last index." % vault.obs_root())
    removed = []
    for rel, (nid, _) in known.items():
        if rel not in seen:
            still = db.execute("SELECT path FROM notes WHERE id=?", (nid,)).fetchone()
            if still and still["path"] == rel:
                _drop(db, nid)
                removed.append(nid)
    if added or changed or removed or full:
        _derive(db)
    db.commit()
    db.close()
    _last_scan = time.time()
    if not quiet and (added or changed or removed):
        events.emit("indexed", "watcher", "Indexed %d new, %d changed, %d removed" %
                    (len(added), len(changed), len(removed)), notes=(added + changed)[:40], removed=removed[:40])
    return {"added": added, "changed": changed, "removed": removed}


_warned = {}


def _warn_once(msg: str, every_sec: int = 1800) -> None:
    import time
    if time.time() - _warned.get(msg, 0) > every_sec:
        _warned[msg] = time.time()
        events.emit("error", "watcher", msg)


def alias_maps(db, skip=()):
    amap, sq = {}, {}
    for r in db.execute("SELECT alias, squashed, note_id FROM aliases"):
        if r["note_id"] in skip:
            continue
        amap.setdefault(r["alias"], set()).add(r["note_id"])
        if len(r["squashed"]) >= 5:
            sq.setdefault(r["squashed"], set()).add(r["note_id"])
    return amap, sq


def count_mentions(text: str, amap: dict, sq: dict) -> dict:
    """How often each card is named in a text. Longest name wins; no model."""
    words = util.norm(text).split()
    counts, i, n = {}, 0, len(words)
    while i < n:
        step = 1
        for size in range(min(5, n - i), 0, -1):
            gram = " ".join(words[i:i + size])
            if size == 1 and len(gram) < 4 and not any(ch.isdigit() for ch in gram):
                continue
            ids = amap.get(gram) or sq.get(gram.replace(" ", ""))
            if ids:
                for x in ids:
                    counts[x] = counts.get(x, 0) + 1
                step = size
                break
        i += step
    return counts


def _derive(db) -> None:
    """Links nobody wrote down: which Obsidian notes talk about which cards ('mention'),
    and which cards keep turning up in the same notes ('co')."""
    db.execute("DELETE FROM edges WHERE kind IN ('mention','co')")
    skip = set((config.load().get("obsidian") or {}).get("no_auto_link") or [])
    # The owner's own card would link to everything, so it is left out of automatic links.
    skip |= {r["id"] for r in db.execute("SELECT id FROM notes WHERE id='preferences' OR (type='person' AND pin=1)")}
    amap, sq = alias_maps(db, skip)
    if not amap:
        return
    explicit = set()
    for r in db.execute("SELECT src, dst FROM edges WHERE kind='link'"):
        t = resolve_link(db, r["dst"])
        if t:
            explicit.add((r["src"], t))
    co = {}
    for r in db.execute("SELECT id, title, body, type FROM notes WHERE origin='obs' AND status != 'archived'"):
        counts = count_mentions(r["title"] + "\n" + r["body"][:80000], amap, sq)
        for cid, c in counts.items():
            if (r["id"], cid) not in explicit:
                db.execute("INSERT INTO edges VALUES(?,?,'mention',?)", (r["id"], cid, c))
        strong = sorted(c for c in counts if counts[c] >= 2)
        for i, a in enumerate(strong):
            for b in strong[i + 1:]:
                co[(a, b)] = co.get((a, b), 0) + 1
    for (a, b), c in co.items():
        if c >= 2 and (a, b) not in explicit and (b, a) not in explicit:
            db.execute("INSERT INTO edges VALUES(?,?,'co',?)", (a, b, c))


# ---------- lookups ----------

def resolve_link(db, target: str):
    """A [[link]] target -> note id (by id, then card alias, then note name)."""
    r = db.execute("SELECT id FROM notes WHERE id=?", (target,)).fetchone()
    if r:
        return r["id"]
    nm, sqv = util.norm(target), util.squash(target)
    r = db.execute("SELECT note_id FROM aliases WHERE alias=? OR squashed=?", (nm, sqv)).fetchone()
    if r:
        return r["note_id"]
    r = db.execute("SELECT note_id FROM names WHERE name=? OR squashed=?", (nm, sqv)).fetchone()
    return r["note_id"] if r else None


def match_names(db, text: str):
    """Obsidian notes named in free text (by file name)."""
    words = util.norm(text).split()
    found, used = [], set()
    for size in range(min(6, len(words)), 0, -1):
        for i in range(0, len(words) - size + 1):
            if any(j in used for j in range(i, i + size)):
                continue
            gram = " ".join(words[i:i + size])
            if size == 1 and len(gram) < 5:
                continue
            rows = db.execute("SELECT DISTINCT note_id FROM names WHERE name=?", (gram,)).fetchall()
            if rows:
                used.update(range(i, i + size))
                for r in rows:
                    if r["note_id"] not in found:
                        found.append(r["note_id"])
    return found


def docs_for(db, card_id: str, scopes, limit: int = 4):
    """The Obsidian notes that hold the details for a card, best first."""
    card = db.execute("SELECT title FROM notes WHERE id=?", (card_id,)).fetchone()
    names = {util.squash(card["title"])} if card else set()
    names |= {r["squashed"] for r in db.execute("SELECT squashed FROM aliases WHERE note_id=?", (card_id,))}
    rows = {}
    for r in db.execute("SELECT n.*, e.w AS w FROM edges e JOIN notes n ON n.id=e.src "
                        "WHERE e.dst=? AND e.kind='mention' AND n.type='doc'", (card_id,)):
        rows[r["id"]] = (r, r["w"])
    for e in db.execute("SELECT src, dst FROM edges WHERE kind='link' AND (src=? OR dst=?)", (card_id, card_id)):
        other = e["dst"] if e["src"] == card_id else e["src"]
        oid = resolve_link(db, other)
        r = db.execute("SELECT * FROM notes WHERE id=? AND type='doc'", (oid,)).fetchone() if oid else None
        if r:
            rows[r["id"]] = (r, 999)
    scored = []
    for r, w in rows.values():
        if r["scope"] not in scopes or r["status"] == "archived":
            continue
        st = util.squash(r["title"])
        exact = st in names
        in_title = any(s and s in st for s in names)
        density = w / max(1.0, math.sqrt(max(1, r["tokens"]) / 300.0))
        scored.append(((1 if exact else 0, 1 if in_title else 0, density), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def set_summary(note_id: str, summary: str, tags) -> None:
    db = connect()
    r = db.execute("SELECT title, body, tags FROM notes WHERE id=?", (note_id,)).fetchone()
    if r:
        alltags = " ".join(dict.fromkeys((r["tags"] or "").split() + list(tags or [])))
        db.execute("UPDATE notes SET summary=?, tags=? WHERE id=?", (summary, alltags, note_id))
        if has_fts(db):
            db.execute("DELETE FROM fts WHERE id=?", (note_id,))
            db.execute("INSERT INTO fts VALUES(?,?,?)", (note_id, r["title"] + " " + alltags, r["body"]))
        db.commit()
    db.close()


def match_aliases(db, text: str):
    """Find entity cards named in free text. Longest names first; no LLM."""
    words = util.norm(text).split()
    found, used = [], set()
    for size in range(min(5, len(words)), 0, -1):
        for i in range(0, len(words) - size + 1):
            if any(j in used for j in range(i, i + size)):
                continue
            gram = " ".join(words[i:i + size])
            if size == 1 and len(gram) < 2:
                continue
            rows = db.execute("SELECT DISTINCT note_id FROM aliases WHERE alias=? OR squashed=?",
                              (gram, gram.replace(" ", ""))).fetchall()
            if rows:
                used.update(range(i, i + size))
                for r in rows:
                    if r["note_id"] not in found:
                        found.append(r["note_id"])
    return found


def neighbours(db, nid: str):
    out = []
    for r in db.execute("SELECT dst FROM edges WHERE src=?", (nid,)):
        t = resolve_link(db, r["dst"])
        if t and t != nid and t not in out:
            out.append(t)
    for r in db.execute("SELECT src FROM edges WHERE dst=?", (nid,)):
        if r["src"] != nid and r["src"] not in out:
            out.append(r["src"])
    return out


_STOP_Q = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "what", "when",
            "did", "do", "i", "my", "me", "about", "with", "how", "that", "this", "it", "have", "has", "been", "done",
            "so", "far", "which", "who", "where", "why", "does", "status", "latest", "recent", "last", "any", "all",
            "be", "at", "by", "from", "as", "not", "we", "you", "he", "she", "his", "her", "their", "there",
            "many", "much", "now", "big", "large", "long", "should", "behind", "cover", "covers", "every",
            "list", "help", "tell", "show", "give", "find", "need", "want"}


def query_terms(text: str):
    return [w for w in util.norm(text).split() if w not in _STOP_Q and len(w) > 1][:12]


def _fts_query(text: str) -> str:
    return " OR ".join('"%s"' % t for t in query_terms(text))


def search(db, query: str, scopes, limit: int = 12, types=None, with_scores: bool = False):
    """Keyword search (BM25) with optional vector boost. Dormant notes rank lower."""
    q = _fts_query(query)
    rows = []
    if q and has_fts(db):
        try:
            rows = db.execute(
                "SELECT n.*, bm25(fts, 0, 4.0, 1.0) AS score FROM fts JOIN notes n ON n.id = fts.id "
                "WHERE fts MATCH ? ORDER BY score LIMIT 60", (q,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    elif q:
        terms = [t.strip('"') for t in q.split(" OR ")]
        cand = db.execute("SELECT *, 0 AS score FROM notes").fetchall()
        rows = [r for r in cand if any(t in (r["title"] + " " + r["body"]).lower() for t in terms)]
    scored = {}
    for r in rows:
        scored[r["id"]] = [r, -float(r["score"]) if r["score"] else 1.0]
    qv = _query_vector(query)
    if qv:
        for r in db.execute("SELECT v.vec, n.* FROM vectors v JOIN notes n ON n.id=v.note_id"):
            sim = _cos(qv, _unpack(r["vec"]))
            if r["id"] in scored:
                scored[r["id"]][1] += 6 * sim
            elif sim > 0.55:
                scored[r["id"]] = [r, 6 * sim]
    out = []
    for nid, (r, s) in scored.items():
        if r["scope"] not in scopes or r["status"] == "archived":
            continue
        if types and r["type"] not in types:
            continue
        if r["status"] == "dormant":
            s *= 0.5
        age = util.days_since(r["date"] or r["updated"])
        if age is not None:
            s *= 1.0 + 0.3 * math.exp(-age / 60.0)
        out.append((s, r))
    out.sort(key=lambda x: -x[0])
    if with_scores:
        return [(r, s) for s, r in out[:limit]]
    return [r for _, r in out[:limit]]


@functools.lru_cache(maxsize=200000)
def _stem(w: str) -> str:
    for suf, keep in (("ing", 4), ("ed", 3), ("es", 3), ("s", 3)):
        if len(w) > keep + len(suf) - 1 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _stems(words):
    out = set()
    for w in words:
        out.add(w)
        out.add(_stem(w))
    return out


def _best_line(text: str, terms, n: int = 170) -> str:
    """The line or sentence of a section that best matches the query terms, shortened at a word boundary."""
    want = _stems(terms)
    best, score = "", -1
    for raw in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z0-9*])", text):
        line = re.sub(r"[*_`>#]|^\s*[-+]\s+|\[\[|\]\]", "", raw.replace("|", " / ")).strip()
        if len(line) < 12:
            continue
        have = _stems(util.norm(line).split())
        s = len(want & have) + 0.5 * sum(1 for a, b in zip(terms, terms[1:]) if (a + " " + b) in util.norm(line))
        if s > score:
            best, score = line, s
    return util.clip(best, n)


def search_sections(db, query: str, scopes, limit: int = 3, per_note: int = 2):
    """Heading sections of the owner's notes that best match the question: (note row, section row, snippet).
    A section needs at least half of the question's words; phrases in the same order rank higher."""
    terms = query_terms(query)
    if not terms or not has_fts(db):
        return []
    try:
        rows = db.execute("SELECT note_id, ord, bm25(sfts, 0, 0, 5.0, 1.0) AS sc FROM sfts WHERE sfts MATCH ? "
                          "ORDER BY sc LIMIT 40", (" OR ".join('"%s"' % t for t in terms),)).fetchall()
    except sqlite3.OperationalError:
        return []
    # Only words that occur in some section can count: "headcount" or "abuse" appearing nowhere must not
    # dilute the match for the words that do.
    present = []
    for t in terms:
        try:
            if db.execute("SELECT 1 FROM sfts WHERE sfts MATCH ? LIMIT 1", ('"%s"' % t,)).fetchone():
                present.append(t)
        except sqlite3.OperationalError:
            pass
    if not present:
        return []
    scored = []
    for r in rows:
        # slice the section out of the note inside SQLite: some notes are hundreds of KB
        sec = db.execute("SELECT s.*, n.title, n.type, n.scope, n.status, "
                         "substr(n.body, s.start + 1, s.end - s.start) AS text "
                         "FROM secs s JOIN notes n ON n.id = s.note_id WHERE s.note_id=? AND s.ord=?",
                         (r["note_id"], r["ord"])).fetchone()
        if not sec or sec["type"] != "doc" or sec["scope"] not in scopes or sec["status"] == "archived":
            continue
        n, text = {"id": sec["note_id"], "title": sec["title"]}, sec["text"]
        flat = util.norm(n["title"] + " " + sec["path"] + " " + text)
        words = _stems(flat.split()) | {util.squash(n["title"])} | {util.squash(h) for h in sec["path"].split(" › ")}
        headw = _stems(util.norm(sec["path"]).split()) | {util.squash(n["title"])}
        cover = sum(1 for t in present if t in words or _stem(t) in words) / len(present)
        if cover < 0.5:
            continue
        phrase = sum(1 for a, b in zip(terms, terms[1:]) if (a + " " + b) in flat)
        head = sum(1 for t in present if t in headw or _stem(t) in headw) / len(present)
        scored.append((cover * 10 + phrase * 3 + head * 4 - r["sc"] * 0.15, n, sec, text))
    scored.sort(key=lambda x: -x[0])
    out, per = [], {}
    for s, n, sec, text in scored:
        if per.get(n["id"], 0) >= per_note:
            continue
        per[n["id"]] = per.get(n["id"], 0) + 1
        out.append((n, sec, _best_line(text, terms)))
        if len(out) >= limit:
            break
    return out


# ---------- optional vectors (served by oMLX) ----------

def _pack(v):
    return struct.pack("%df" % len(v), *v)


def _unpack(b):
    return struct.unpack("%df" % (len(b) // 4), b)


def _cos(a, b):
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _query_vector(text):
    try:
        from . import llm
        return llm.embed_cached_query(text)
    except Exception:
        return None


def embed_pending(max_notes: int = 200) -> int:
    """Embed notes whose text changed. Does nothing when no embedding model is available."""
    from . import llm
    if not llm.embed_model():
        return 0
    db = connect()
    rows = db.execute("SELECT n.id, n.title, n.summary, n.body, n.hash FROM notes n LEFT JOIN vectors v "
                      "ON v.note_id=n.id WHERE v.hash IS NULL OR v.hash != n.hash LIMIT ?", (max_notes,)).fetchall()
    done = 0
    for i in range(0, len(rows), 16):
        batch = rows[i:i + 16]
        vecs = llm.embed([(r["title"] + "\n" + r["summary"] + "\n" + r["body"])[:2000] for r in batch])
        if not vecs:
            break
        for r, v in zip(batch, vecs):
            db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?)", (r["id"], r["hash"], _pack(v)))
            done += 1
        db.commit()
    db.execute("DELETE FROM vectors WHERE note_id NOT IN (SELECT id FROM notes)")
    db.commit()
    db.close()
    return done


def graph(scopes=None) -> dict:
    """Nodes and edges for the dashboard."""
    db = connect()
    nodes, ids, kinds = [], set(), {}
    for r in db.execute("SELECT id, type, status, scope, title, summary, date, updated, pin, origin, path FROM notes"):
        if scopes and r["scope"] not in scopes:
            continue
        if r["id"] in ("brief", "review", "librarian-report", "preferences") or r["status"] == "archived":
            continue
        ids.add(r["id"])
        kinds[r["id"]] = r["type"]
        nodes.append(dict(r))
    edges, seen = [], set()
    for r in db.execute("SELECT src, dst, kind FROM edges ORDER BY kind"):
        t = resolve_link(db, r["dst"])
        if t and r["src"] in ids and t in ids and t != r["src"] and (r["src"], t) not in seen:
            seen.add((r["src"], t))
            # obsidian = a [[link]] written in one of the owner's notes; helper = written by the helpers on a
            # card or Log note; found = worked out by the helpers (a note names a card, cards appear together)
            k = "found" if r["kind"] != "link" else ("obsidian" if kinds.get(r["src"]) == "doc" else "helper")
            edges.append({"s": r["src"], "t": t, "k": k})
    db.close()
    return {"nodes": nodes, "edges": edges}
