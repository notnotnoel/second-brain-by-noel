"""The bridge to the Obsidian vault. The vault holds the details and is the source of truth;
this module lets the local model read what changed there and patch the cards to match.
It never edits the owner's notes: it writes only inside its own folder (default "Second Brain")."""
from __future__ import annotations
import re, shutil
from . import config, digests, events, filer, index, llm, util, vault

DIGEST = """You index one note from a personal Obsidian vault. Reply with ONE JSON object and nothing else:
{"summary": "one plain sentence, max 30 words, saying what this note contains",
 "tags": ["3 to 6 short lowercase topic tags"],
 "new_entities": [{"name": "exact name as written in the note", "type": "person|project|org|topic|tool|place|goal"}],
 "facts": [{"entity": "id from KNOWN ENTITIES", "fact": "one durable fact about that entity, stated in the note"}]}
Rules: copy names, titles, numbers and dates exactly as the note writes them. Never invent or guess.
facts: at most 5, only for ids listed under KNOWN ENTITIES, only lasting statements. Use [] when unsure.
new_entities: at most 2, only a specific named person, organisation, project or tool that the note is mainly about."""

ABOUT = """# About this folder

This folder belongs to the Second Brain helpers on the Mac mini. Everything else in this vault is yours and
the helpers only read it.

- **Inbox**: drop a note here (from any device). The filer turns it into a dated note in Log.
- **Log**: dated notes the filer wrote from "remember this" and from agents' work logs.
- **Notes**: longer write-ups that agents saved here when no better folder was named.
- **Cards**: a read-only copy of the memory cards agents see first. Change a card by adding a note, not by
  editing the copy (it is overwritten).
- **Review.md**: tick a box to approve a new card, a merge or a deletion.
- **Librarian report.md**: what the helpers did on their last run.
- **Archive**: Log notes nobody has used for a long time.
"""


def enabled() -> bool:
    return vault.obs_root() is not None


def migrate() -> list:
    """Create the helpers' folder in Obsidian and move notes the old layout kept outside it. Safe to repeat."""
    done = []
    own = vault.own_root()
    if not own or not vault.obs_available():
        return done
    for sub in ("Inbox", "Log", "Notes", "Cards", "Archive"):
        (own / sub).mkdir(parents=True, exist_ok=True)
    about = own / "About this folder.md"
    if not about.exists():
        about.write_text(ABOUT, encoding="utf-8")
        done.append("created the %s folder in Obsidian" % vault.own_folder())
    root = vault.root()
    for old in ("20-Events", "30-Sources"):
        d = root / old
        if not d.exists():
            continue
        for p in sorted(d.rglob("*.md")):
            meta, _ = vault.parse(p.read_text(encoding="utf-8", errors="replace"))
            year = str(meta.get("date") or util.today())[:4]
            dest = own / "Log" / year / p.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name("%s-%s.md" % (p.stem, util.sha(str(p))[:4]))
            shutil.move(str(p), str(dest))
            done.append("moved %s to Obsidian" % p.name)
    for name, old in (("review", "review.md"), ("report", "librarian-report.md")):
        new, oldp = vault.core_file(name), root / "00-Core" / old
        if not new.exists() and oldp.exists():
            new.write_text(oldp.read_text(encoding="utf-8"), encoding="utf-8")
            oldp.write_text("Moved to the Obsidian vault: %s\n" % new, encoding="utf-8")
            done.append("moved %s to Obsidian" % old)
    if done:
        events.emit("migrated", "system", "Obsidian is now the source of truth: " + "; ".join(done[:6]))
    return done


# ---------- the local model reads what changed ----------

def _pending(db, dg):
    rows = []
    for r in db.execute("SELECT id, path, title, hash, tokens, body, scope FROM notes "
                        "WHERE type='doc' AND status != 'archived' ORDER BY mtime DESC"):
        if r["tokens"] < 25:
            continue
        if (dg.get(r["path"]) or {}).get("hash") != r["hash"]:
            rows.append(r)
    return rows


def pending_count() -> int:
    if not enabled():
        return 0
    db = index.connect()
    n = len(_pending(db, digests.load()))
    db.close()
    return n


def digest_pending(limit=None) -> dict:
    """Summarise, tag and pull card facts from Obsidian notes that are new or changed."""
    if not enabled() or not llm.available():
        return {"digested": 0, "pending": pending_count() if enabled() else 0}
    cfg = config.load()
    limit = int(limit or cfg["digest"]["per_tick"])
    index.reindex(quiet=True)
    db = index.connect()
    dg = digests.load()
    todo = _pending(db, dg)
    known = {r["id"]: dict(r) for r in db.execute("SELECT id, title, type FROM notes WHERE origin='brain'")
             if r["type"] in vault.ENTITY_TYPES and r["id"] != "preferences"}
    done = 0
    for r in todo[:limit]:
        body = r["body"]
        # Only cards this note is really about can receive facts from it.
        tsq = util.squash(r["title"])
        mentioned = []
        for e in db.execute("SELECT dst, w FROM edges WHERE src=? AND kind='mention' ORDER BY w DESC LIMIT 12",
                            (r["id"],)):
            if e["dst"] not in known:
                continue
            names = {x["squashed"] for x in db.execute("SELECT squashed FROM aliases WHERE note_id=?", (e["dst"],))}
            if e["w"] >= 3 or any(s and s in tsq for s in names):
                mentioned.append(e["dst"])
        mentioned = mentioned[:4]
        listing = "\n".join("- %s | %s | %s" % (k, known[k]["title"], known[k]["type"]) for k in mentioned)
        heads = re.findall(r"^#{1,3}\s+(.+)$", body, re.M)[:30]
        text = body[: int(cfg["digest"]["max_chars"])]
        user = "NOTE FILE: %s\n\nKNOWN ENTITIES (id | name | type):\n%s\n\n%sNOTE TEXT:\n%s" % (
            vault.show_path(r["path"]), listing or "(none)",
            ("SECTIONS: " + " · ".join(heads) + "\n\n") if len(body) > len(text) and heads else "", text)
        if llm.blocked(r["scope"] or ""):
            out = {}           # private note and a cloud model: keep it on the Mac, index it by rules only
        else:
            out = llm.chat(DIGEST, user, role="fast", max_tokens=600, scope=r["scope"] or "")
        if out is None or (out == {} and not llm.blocked(r["scope"] or "")):
            events.emit("error", "librarian", "The model gave no usable answer for “%s”. Will retry." % r["title"])
            break
        summary = re.sub(r"\s+", " ", str(out.get("summary") or "")).strip()[:240]
        if not util.grounded(summary, r["title"] + " " + vault.show_path(r["path"]) + " " + body, 0.6):
            summary = ""
        tags = [util.slugify(str(t), 24) for t in (out.get("tags") or []) if isinstance(t, str)][:6]
        facts = []
        for f in (out.get("facts") or [])[:5]:
            if not isinstance(f, dict) or not f.get("fact"):
                continue
            e = index.resolve_link(db, str(f.get("entity") or ""))
            fact = re.sub(r"\s+", " ", str(f["fact"])).strip()[:240]
            if e in mentioned and "[[" not in fact and len(fact) > 15 and util.grounded(fact, body):
                facts.append([e, fact])
        proposed = []
        nb = util.norm(body)
        for ne in (out.get("new_entities") or [])[:2]:
            if not isinstance(ne, dict) or not ne.get("name"):
                continue
            name = str(ne["name"]).strip()
            nn = util.norm(name)
            if len(nn) < 3 or len(nn.split()) > 5 or nb.count(nn) < 3 or index.resolve_link(db, name):
                continue
            typ = ne.get("type") if ne.get("type") in vault.ENTITY_TYPES else "topic"
            if filer.add_review("New cards proposed", "- [ ] Create card: %s (%s) · from [[%s]]" % (name, typ, r["id"])):
                proposed.append(name)
        prev = dg.get(r["path"]) or {}
        dg[r["path"]] = {"id": r["id"], "hash": r["hash"], "summary": summary, "tags": tags, "facts": facts,
                         "scope": r["scope"], "when": util.stamp(), "applied": prev.get("applied") or {}}
        digests.save(dg)
        if summary or tags:
            index.set_summary(r["id"], summary or "", tags)
        done += 1
        msg = "Read “%s”" % r["title"]
        if facts:
            msg += " → %d fact%s for %s" % (len(facts), "" if len(facts) == 1 else "s",
                                             ", ".join(sorted({known[e]["title"] for e, _ in facts})))
        if proposed:
            msg += " · proposed: " + ", ".join(proposed)
        events.emit("digested", "librarian", msg, notes=[r["id"]] + sorted({e for e, _ in facts}))
    left = len(todo) - done
    db.close()
    return {"digested": done, "pending": max(0, left)}


def mirror_cards() -> int:
    """Keep a read-only copy of every card in Obsidian, so cards can be read there (and on the phone)."""
    own = vault.own_root()
    if not own or not vault.obs_available():
        return 0
    d = own / "Cards"
    d.mkdir(parents=True, exist_ok=True)
    keep, wrote = set(), 0
    for n in vault.all_notes(include_archive=False, include_obs=False):
        if n.type not in vault.ENTITY_TYPES or n.id == "preferences" or n.meta.get("scope") == "private":
            continue
        name = n.id + ".md"
        keep.add(name)
        meta = {"title": n.title, "aliases": [n.title] + n.aliases, "tags": ["second-brain/card", n.type],
                "status": n.meta.get("status", "active"), "updated": n.meta.get("updated", "")}
        text = vault.dump(meta, "> Copy of a Second Brain card. Do not edit here: it is overwritten.\n\n"
                                "# %s\n%s\n\n%s" % (n.title, n.meta.get("summary") or "", n.body))
        p = d / name
        if not p.exists() or p.read_text(encoding="utf-8", errors="replace") != text:
            p.write_text(text, encoding="utf-8")
            wrote += 1
    for p in d.glob("*.md"):
        if p.name not in keep:
            try:
                p.unlink()
            except Exception:
                pass
    return wrote
