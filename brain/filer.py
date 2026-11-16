"""Filer: turns raw inbox items into dated, linked notes. Uses the helpers' fast model
(local, Claude Code or the Anthropic API); falls back to rule-based filing when none is reachable."""
from __future__ import annotations
import re
from . import events, index, llm, util, vault

SYSTEM = """You file notes into a personal knowledge base. Reply with ONE JSON object and nothing else:
{"title": "max 8 words", "date": "YYYY-MM-DD", "kind": "event" | "decision" | "source",
 "summary": "one plain sentence, max 30 words",
 "entities": ["ids from KNOWN ENTITIES that this note is about"],
 "new_entities": [{"name": "exact name as written in the note", "type": "person|project|org|topic|tool|place|goal"}],
 "facts": [{"entity": "id from KNOWN ENTITIES", "fact": "one durable fact stated in the note"}]}
Rules: use only ids listed under KNOWN ENTITIES. Only propose a new entity when the note names a specific
person, organisation, project, tool or place that is not listed. Never invent names, dates or facts.
kind is "decision" when a choice was made, "source" for articles or reference material, else "event".
facts are lasting statements (not one-off actions); use [] when there are none."""


def inbox_items():
    dirs = [vault.root() / "01-Inbox"]
    own = vault.own_root()
    if own and vault.obs_available():
        dirs.append(own / "Inbox")
    out = []
    for d in dirs:
        if d.exists():
            out += sorted(p for p in d.iterdir()
                          if p.is_file() and p.suffix.lower() in (".md", ".txt", ".markdown")
                          and not p.name.startswith(".") and p.name != "README.txt")
    return out


def add_review(section: str, line: str) -> bool:
    p = vault.core_file("review")
    text = p.read_text(encoding="utf-8") if p.exists() else "# Review queue\n"
    key = line.split(" · ")[0].replace("- [ ] ", "").strip()
    if key in text:
        return False
    head = "## " + section
    if head not in text:
        text = text.rstrip() + "\n\n" + head + "\n"
    i = text.index(head) + len(head)
    nxt = text.find("\n## ", i)
    block = text[i:nxt if nxt != -1 else len(text)].rstrip()
    rest = text[nxt:] if nxt != -1 else ""
    p.write_text(text[:i] + block + "\n" + line + "\n" + rest, encoding="utf-8")
    return True


def _fallback(body: str, hint_kind: str):
    first = next((l.strip() for l in body.splitlines() if l.strip()), "Note")
    first = re.sub(r"^[#>\-\s]+", "", first)
    title = re.sub(r"^\d{4}-\d{2}-\d{2}[:,\s]*", "", first)[:60].strip() or "Note"
    summary = re.sub(r"^\d{4}-\d{2}-\d{2}[:,\s]*", "", re.sub(r"\s+", " ", body).strip())[:200]
    return {"title": title, "kind": hint_kind or "event", "summary": summary, "entities": [],
            "new_entities": [], "facts": []}


def file_one(path, db, known: dict) -> dict:
    meta, body = vault.parse(path.read_text(encoding="utf-8", errors="replace"))
    body = body.strip()
    if not body:
        path.unlink()
        return {}
    hinted = []
    for h in (meta.get("entities") or []) if isinstance(meta.get("entities"), list) else [meta.get("entities")]:
        nid = index.resolve_link(db, str(h)) if h else None
        if nid and nid in known:
            hinted.append(nid)
    matched = [m for m in index.match_aliases(db, body) if m in known]
    cand = list(dict.fromkeys(hinted + matched))
    others = [k for k in known if k not in cand][:80]
    listing = "\n".join("- %s | %s | %s" % (k, known[k]["title"], known[k]["type"]) for k in cand + others)
    out = None
    model = llm.pick("fast")
    if model and llm.available():
        out = llm.chat(SYSTEM, "TODAY: %s\n\nKNOWN ENTITIES (id | name | type):\n%s\n\nNOTE:\n%s"
                       % (util.today(), listing or "(none yet)", body[:6000]), role="fast",
                       scope=str(meta.get("scope") or "personal"))
    used_model = bool(out)
    if not out:
        out = _fallback(body, str(meta.get("kind") or ""))

    title = str(out.get("title") or "Note").strip()[:80]
    date = util.parse_date(out.get("date")) or util.parse_date(body[:10]) or util.parse_date(util.today())
    if abs((date - util.parse_date(util.today())).days) > 3660:
        date = util.parse_date(util.today())
    date = date.isoformat()
    kind = out.get("kind") if out.get("kind") in ("event", "decision", "source") else "event"
    if meta.get("kind") in ("event", "decision", "source") and not used_model:
        kind = meta["kind"]
    summary = re.sub(r"\s+", " ", str(out.get("summary") or "")).strip()[:300] or title
    if used_model and not util.grounded(summary, body, 0.7):
        fb = _fallback(body, "")              # the model drifted from the text: use the text itself
        summary = fb["summary"]
        if not util.grounded(title, body, 0.6):
            title = fb["title"]

    # A note is linked only to cards it actually names (or that the sender named). The model cannot add links.
    ents = list(cand)
    proposals, text_norm = [], util.norm(body)
    for ne in out.get("new_entities") or []:
        if not isinstance(ne, dict) or not ne.get("name"):
            continue
        name = str(ne["name"]).strip()
        if len(name) < 2 or util.norm(name) not in text_norm:
            continue  # the model invented it
        existing = index.resolve_link(db, name)
        if existing:
            if existing in known and existing not in ents:
                ents.append(existing)
            continue
        typ = ne.get("type") if ne.get("type") in vault.ENTITY_TYPES else "topic"
        proposals.append((name, typ))
    facts = []
    for f in out.get("facts") or []:
        if isinstance(f, dict) and f.get("fact"):
            e = index.resolve_link(db, str(f.get("entity") or ""))
            fact = re.sub(r"\s+", " ", str(f["fact"])).strip()[:240]
            if e in ents and util.grounded(fact, body):
                facts.append((e, fact))

    taken = {r["id"] for r in db.execute("SELECT id FROM notes")}
    nid = vault.unique_id("%s-%s" % (date, util.slugify(title, 32)), taken)
    lines = ["# " + title, "", summary, ""]
    if ents:
        lines += ["Mentions: " + " ".join("[[%s]]" % e for e in ents), ""]
    if facts:
        lines += ["## Facts"] + ["- [[%s]]: %s" % (e, f) for e, f in facts] + [""]
    if util.norm(body) != util.norm(summary):
        lines += ["## Original", body, ""]
    dest = vault.events_dir(kind, date) / (nid + ".md")
    in_obs = vault.own_root() is not None and vault.obs_available()
    note = vault.Note(dest,
                      {"id": nid, "type": kind, "title": title, "status": "active",
                       "scope": str(meta.get("scope") or "personal"), "summary": summary, "date": date,
                       "source": str(meta.get("source") or "manual"), "updated": util.today()},
                      "\n".join(lines), origin="obs" if in_obs else "brain")
    note.rel = ("obs/" + dest.relative_to(vault.obs_root()).as_posix()) if in_obs else \
        dest.relative_to(vault.root()).as_posix()
    if in_obs:
        note.meta["tags"] = ["second-brain/log"]
    note.save()
    for name, typ in proposals:
        add_review("New cards proposed", "- [ ] Create card: %s (%s) · from [[%s]]" % (name, typ, nid))
    path.unlink()
    msg = "Filed “%s”" % title
    if ents:
        msg += " → " + ", ".join(known[e]["title"] for e in ents[:4])
    if proposals:
        msg += " · proposed: " + ", ".join(n for n, _ in proposals)
    events.emit("filed", "filer", msg, notes=[nid] + ents, model=(model if used_model else "rules"),
                proposed=[n for n, _ in proposals])
    return {"id": nid, "entities": ents, "proposed": proposals, "model": used_model}


def run(limit: int = 25) -> dict:
    items = inbox_items()[:limit]
    if not items:
        return {"filed": 0}
    index.reindex(quiet=True)
    events.emit("working", "filer", "Filing %d inbox item%s" % (len(items), "" if len(items) == 1 else "s"))
    db = index.connect()
    known = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, title, type FROM notes WHERE status != 'archived' AND origin='brain' ORDER BY updated DESC")
        if r["type"] in vault.ENTITY_TYPES and r["id"] != "preferences"}
    done, errors = [], 0
    for p in items:
        try:
            r = file_one(p, db, known)
            if r:
                done.append(r)
        except Exception as e:
            errors += 1
            events.emit("error", "filer", "Could not file %s: %s" % (p.name, e))
    db.close()
    index.reindex()
    return {"filed": len(done), "errors": errors, "items": done}
