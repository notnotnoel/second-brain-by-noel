"""Librarian: the only helper that edits cards. Rules decide what happens; the local model
only writes summaries. Nothing is deleted without a ticked box in 00-Core/review.md."""
from __future__ import annotations
import datetime as dt, difflib, json, re, shutil, subprocess, sys
from . import config, digests, events, filer, index, llm, util, vault

EVENT_KINDS = ("event", "decision", "source")

CONSOLIDATE = """You maintain one card in a personal knowledge base. Given the card and NEW ENTRIES,
reply with ONE JSON object and nothing else:
{"now": "2-3 short sentences on the current state, written from the entries and facts",
 "summary": "one line, max 15 words, saying what this card is",
 "add_facts": ["durable facts from the new entries that are not already in KEY FACTS"],
 "superseded": ["exact text of any KEY FACTS line that a new entry contradicts or replaces"]}
Use only what is written below. Never invent. Use [] when there is nothing to add."""

COMPRESS = """Summarise these older timeline entries of a personal knowledge card into ONE sentence of at
most 40 words. Keep names, decisions and outcomes. Reply with ONE JSON object: {"summary": "..."}"""


# ---------- state, git ----------

def _state() -> dict:
    p = config.home() / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    (config.home() / "state.json").write_text(json.dumps(s, indent=2), encoding="utf-8")


def git_ok() -> bool:
    if not shutil.which("git"):
        return False
    if sys.platform == "darwin":  # avoid the "install developer tools" popup
        try:
            if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
                return False
        except Exception:
            return False
    return True


def snapshot(msg: str) -> bool:
    """Commit the vault so every librarian change can be undone."""
    root = vault.root()
    if not git_ok():
        return False
    try:
        if not (root / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=str(root), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True)
        subprocess.run(["git", "-c", "user.name=Second Brain", "-c", "user.email=brain@localhost",
                        "commit", "-q", "-m", msg], cwd=str(root), capture_output=True)
        return True
    except Exception:
        return False


def _trash(note: vault.Note) -> None:
    if not note.writable:
        raise PermissionError("Not deleting the owner's Obsidian note: %s" % note.path)
    t = (vault.obs_root() if note.origin == "obs" else vault.root()) / ".trash"
    t.mkdir(exist_ok=True)
    dest = t / note.path.name
    if dest.exists():
        dest = t / ("%s-%s.md" % (note.path.stem, util.sha(util.stamp())[:5]))
    shutil.move(str(note.path), str(dest))


# ---------- approvals (ticked boxes in review.md) ----------

def merge(src: vault.Note, dst: vault.Note) -> None:
    names = [src.title] + src.aliases
    dst.meta["aliases"] = list(dict.fromkeys(dst.aliases + [n for n in names if util.norm(n) != util.norm(dst.title)]))
    for sec in ("Key facts", "Timeline"):
        have = [l for l in dst.section(sec).splitlines() if l.strip()]
        seen = {util.norm(l) for l in have}
        extra = [l for l in src.section(sec).splitlines() if l.strip() and util.norm(l) not in seen]
        if extra:
            merged = have + extra
            if sec == "Timeline":
                merged.sort(reverse=True)
            dst.set_section(sec, "\n".join(merged))
    dst.meta["updated"] = util.today()
    dst.save()
    pat = re.compile(r"\[\[%s(?=[\]|#])" % re.escape(src.id))
    for n in vault.all_notes():
        if n.writable and n.path != src.path and pat.search(n.body):
            n.body = pat.sub("[[" + dst.id, n.body)
            n.save()
    _trash(src)


def apply_approvals() -> list:
    p = vault.core_file("review")
    if not p.exists():
        return []
    notes = {n.id: n for n in vault.all_notes()}
    applied, keep = [], []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*-\s*\[[xX]\]\s*(.+)$", line)
        if not m:
            keep.append(line)
            continue
        act = m.group(1)
        try:
            c = re.match(r"Create card:\s*(.+?)\s*\((\w+)\)(?:.*?\[\[([^\]]+)\]\])?", act)
            g = re.match(r"Merge \[\[([^\]]+)\]\] into \[\[([^\]]+)\]\]", act)
            d = re.match(r"Delete \[\[([^\]]+)\]\]", act)
            if c:
                card = vault.create_card(c.group(1), c.group(2), taken=set(notes))
                notes[card.id] = card
                ev = notes.get(c.group(3) or "")
                if ev and ev.writable and not ev.is_doc:
                    if "Mentions:" in ev.body:
                        ev.body = re.sub(r"(Mentions:[^\n]*)", r"\1 [[%s]]" % card.id, ev.body, count=1)
                    else:
                        ev.body = ev.body.rstrip() + "\n\nMentions: [[%s]]\n" % card.id
                    ev.save()
                applied.append("Created card %s" % card.title)
                events.emit("card_created", "librarian", "Created card “%s”" % card.title, notes=[card.id])
            elif g and g.group(1) in notes and g.group(2) in notes and notes[g.group(1)].origin == "brain":
                src, dst = notes[g.group(1)], notes[g.group(2)]
                merge(src, dst)
                notes.pop(src.id, None)
                applied.append("Merged %s into %s" % (src.title, dst.title))
                events.emit("merged", "librarian", "Merged “%s” into “%s”" % (src.title, dst.title),
                            notes=[dst.id], removed=[src.id])
            elif d and d.group(1) in notes and notes[d.group(1)].writable:
                n = notes.pop(d.group(1))
                _trash(n)
                applied.append("Deleted %s" % n.title)
                events.emit("deleted", "librarian", "Deleted “%s” (approved)" % n.title, removed=[n.id])
            else:
                keep.append(line.replace("[x]", "[ ]").replace("[X]", "[ ]") + "  (could not apply)")
        except Exception as e:
            keep.append(line + "  (error: %s)" % e)
    if applied:
        p.write_text("\n".join(keep) + "\n", encoding="utf-8")
        index.reindex(quiet=True)
    return applied


# ---------- nightly: consolidate, cap, brief ----------

def _event_facts(ev: vault.Note, card_id: str):
    out = []
    for l in ev.section("Facts").splitlines():
        m = re.match(r"-\s*\[\[([^\]]+)\]\]:\s*(.+)$", l.strip())
        if m and m.group(1) == card_id:
            out.append(m.group(2).strip())
    return out


def _ok(text, source, ratio=0.7):
    return bool(str(text or "").strip()) and util.grounded(str(text), source, ratio)


def consolidate() -> dict:
    """Fold new Log notes and new facts read from Obsidian notes into the cards they name."""
    cfg = config.load()
    notes = vault.all_notes(include_archive=False)
    db = index.connect()
    by_card = {}
    for ev in notes:
        if ev.type not in EVENT_KINDS:
            continue
        for target in ev.links():
            cid = index.resolve_link(db, target)
            if cid and cid != ev.id:
                by_card.setdefault(cid, []).append(ev)
    db.close()
    dg = digests.load()
    doc_facts = {}
    for rel, d in dg.items():
        for cid, fact in d.get("facts") or []:
            if (d.get("applied") or {}).get(cid) != d.get("hash"):
                doc_facts.setdefault(cid, []).append((rel, fact, d.get("scope") or "personal"))
    updated, dg_dirty = [], False
    for card in notes:
        if card.origin != "brain" or card.type not in vault.ENTITY_TYPES or card.id == "preferences":
            continue
        private_ok = card.meta.get("scope") == "private"
        new = [e for e in by_card.get(card.id, []) if "[[%s]]" % e.id not in card.body
               and (private_ok or e.meta.get("scope") != "private")]
        dfacts = [(rel, f) for rel, f, sc in doc_facts.get(card.id, []) if private_ok or sc != "private"]
        for rel, f, sc in doc_facts.get(card.id, []):
            dg[rel].setdefault("applied", {})[card.id] = dg[rel].get("hash")
            dg_dirty = True
        if not new and not dfacts:
            continue
        new.sort(key=lambda e: str(e.meta.get("date") or ""), reverse=True)
        tl_new = ["- %s: %s [[%s]]" % (e.meta.get("date") or util.today(),
                                        (e.meta.get("summary") or e.title).rstrip("."), e.id) for e in new]
        timeline = [l for l in card.section("Timeline").splitlines() if l.strip()]
        earlier = [l for l in timeline if l.startswith("- Earlier")]
        dated = sorted([l for l in timeline if not l.startswith("- Earlier")] + tl_new, reverse=True)
        if tl_new:
            card.set_section("Timeline", "\n".join(dated + earlier))

        facts = [l[2:].strip() for l in card.section("Key facts").splitlines() if l.strip().startswith("- ")]
        cand = [f for e in new for f in _event_facts(e, card.id)] + [f for _, f in dfacts]
        entries = "\n".join("%s | %s%s" % (e.meta.get("date"), e.meta.get("summary") or e.title,
                                           "".join("\n   fact: " + f for f in _event_facts(e, card.id)))
                            for e in new[:12])
        entries += "".join("\nfrom note %s | fact: %s" % (vault.show_path(rel), f) for rel, f in dfacts[:12])
        source = "%s\n%s\n%s\n%s" % (card.title, card.body, entries, "\n".join(e.body for e in new[:12]))
        out = None
        if llm.available():
            out = llm.chat(CONSOLIDATE, "CARD: %s (%s)\nNOW:\n%s\n\nKEY FACTS:\n%s\n\nNEW ENTRIES:\n%s" % (
                card.title, card.type, card.section("Now") or "(empty)",
                "\n".join("- " + f for f in facts) or "(none)", entries), role="deep", max_tokens=700,
                scope=card.scope)
        add, superseded = list(cand), []
        if out:
            # Every model-written line must be supported by the card and its entries, or it is dropped.
            if _ok(out.get("now"), source):
                card.set_section("Now", str(out["now"]).strip()[:600])
            cur = str(card.meta.get("summary") or "")
            if (not cur or util.norm(cur) == util.norm(card.title)) and _ok(out.get("summary"), source):
                card.meta["summary"] = re.sub(r"\s+", " ", str(out["summary"])).strip()[:140]
            add += [str(f).strip() for f in (out.get("add_facts") or [])
                    if isinstance(f, str) and _ok(f, source, 0.8)]
            superseded = [str(s).strip().lstrip("- ").strip() for s in (out.get("superseded") or [])]
        if not card.section("Now") and new:
            card.set_section("Now", (new[0].meta.get("summary") or new[0].title))
        seen = {util.norm(re.sub(r"~~", "", f)) for f in facts}
        lines = []
        for f in facts:
            if f in superseded and not f.startswith("~~"):
                lines.append("- ~~%s~~ (superseded %s)" % (f, util.today()))
            else:
                lines.append("- " + f)
        for f in add:
            if f and util.norm(f) not in seen and len(f) < 260:
                seen.add(util.norm(f))
                lines.append("- " + f)
        card.set_section("Key facts", "\n".join(lines))
        card.meta["updated"] = util.today()
        if card.meta.get("status") == "dormant":
            card.meta["status"] = "active"
        _enforce_cap(card, cfg["caps"]["card_tokens"])
        card.save()
        updated.append(card.id)
        n_in = len(new) + len(dfacts)
        events.emit("consolidated", "librarian", "Updated card “%s” with %d new entr%s" %
                    (card.title, n_in, "y" if n_in == 1 else "ies"),
                    notes=[card.id] + [e.id for e in new[:8]])
    if dg_dirty:
        digests.save(dg)
    if updated:
        from . import obsidian
        obsidian.mirror_cards()
    return {"cards_updated": updated}


def _enforce_cap(card: vault.Note, cap: int) -> bool:
    if util.tokens(card.body) <= cap:
        return False
    timeline = [l for l in card.section("Timeline").splitlines() if l.strip()]
    earlier = [l for l in timeline if l.startswith("- Earlier")]
    dated = [l for l in timeline if not l.startswith("- Earlier")]
    keep = 8
    while util.tokens(card.body) > cap and keep >= 3 and len(dated) > keep:
        old = dated[keep:]
        dated = dated[:keep]
        text = None
        if llm.available():
            out = llm.chat(COMPRESS, "\n".join(earlier + old)[:5000], role="deep", max_tokens=200, scope=card.scope)
            text = out and str(out.get("summary") or "").strip()
        first = re.match(r"- (\d{4}-\d{2}-\d{2})", old[0])
        label = "- Earlier (to %s): " % first.group(1) if first else "- Earlier: "
        if text:
            earlier = [label + text]
        else:
            prev = int(re.search(r"(\d+) earlier entr", " ".join(earlier)).group(1)) if re.search(
                r"(\d+) earlier entr", " ".join(earlier)) else 0
            earlier = [label + "%d earlier entries, see the Log" % (prev + len(old))]
        card.set_section("Timeline", "\n".join(dated + earlier))
        keep -= 2
    if util.tokens(card.body) > cap:  # drop struck-through facts next
        facts = [l for l in card.section("Key facts").splitlines() if l.strip() and "~~" not in l]
        card.set_section("Key facts", "\n".join(facts))
    events.emit("compressed", "librarian", "Compressed card “%s” to fit its cap" % card.title, notes=[card.id])
    return True


def build_brief() -> bool:
    cfg = config.load()
    notes = [n for n in vault.all_notes(include_archive=False, include_obs=False)
             if n.meta.get("scope", "personal") != "private"]
    lines = ["# Brief", ""]
    owner = [n for n in notes if n.meta.get("owner")]
    if owner:
        o = owner[0]
        lines += ["## Owner", "%s. %s" % (o.title, o.meta.get("summary") or "")]
        lines += [l for l in o.section("Key facts").splitlines() if l.strip() and "~~" not in l][:8]
        lines.append("")
    prefs = next((n for n in notes if n.id == "preferences"), None)
    if prefs:
        pl = [l for l in prefs.body.splitlines() if re.match(r"-\s+\S", l)]
        if pl:
            lines += ["## Preferences"] + pl[:12] + [""]

    def rank(n):
        return (-int(n.meta.get("use_count_90d") or 0), n.title.lower())

    def row(n):
        return "- %s: %s (id: %s)" % (n.title, n.meta.get("summary") or n.type, n.id)
    active = [n for n in notes if n.type in ("project", "goal") and n.meta.get("status", "active") == "active"]
    if active:
        lines += ["## Active projects and goals"] + [row(n) for n in sorted(active, key=rank)[:12]] + [""]
    others = [n for n in notes if n.type in vault.ENTITY_TYPES and n.type not in ("project", "goal", "preference")
              and not n.meta.get("owner") and n.meta.get("status", "active") == "active"]
    if others:
        lines += ["## Often referenced"] + [row(n) for n in sorted(others, key=rank)[:8]] + [""]
    lines += ["Use recall(\"<name>\") for the full card on anything above."]
    if vault.obs_root():
        lines += ["Details live in the Obsidian vault %s (%s). recall() returns the note paths; write new "
                  "details there with write() or directly, then call sync()." % (vault.obs_root().name, vault.obs_root())]
    text = util.trim_tokens("\n".join(lines), cfg["caps"]["brief_tokens"]) + "\n"
    p = vault.root() / "00-Core" / "brief.md"
    if p.exists() and p.read_text(encoding="utf-8") == text:
        return False  # byte-stable: prompt caches keep hitting
    p.write_text(text, encoding="utf-8")
    events.emit("brief", "librarian", "Rebuilt the brief (~%d tokens)" % util.tokens(text))
    return True


def nightly() -> dict:
    events.emit("working", "librarian", "Nightly tidy-up started")
    snapshot("Before nightly librarian run")
    from . import obsidian
    obsidian.migrate()
    filer.run()
    applied = apply_approvals()
    index.reindex(quiet=True)
    digested = obsidian.digest_pending(limit=40)
    res = consolidate()
    capped = []
    for n in vault.all_notes(include_archive=False, include_obs=False):
        if n.type in vault.ENTITY_TYPES and n.id not in res["cards_updated"]:
            if _enforce_cap(n, config.load()["caps"]["card_tokens"]):
                n.save()
                capped.append(n.id)
    obsidian.mirror_cards()
    index.reindex(quiet=True)
    brief_changed = build_brief()
    index.reindex(quiet=True)
    try:
        embedded = index.embed_pending()
    except Exception:
        embedded = 0
    snapshot("Nightly librarian run")
    out = {"when": util.stamp(), "applied": applied, "cards_updated": len(res["cards_updated"]),
           "compressed": len(capped), "brief_changed": brief_changed, "embedded": embedded, "notes_read": digested.get("digested", 0),
           "notes_waiting": digested.get("pending", 0),
           "model": ("%s · %s" % (llm.pick("deep"), llm.where())) if llm.available() else "rules only (no model reachable)"}
    s = _state()
    s["nightly"] = out
    s["last_nightly"] = util.today()
    _save_state(s)
    write_report()
    events.emit("done", "librarian", "Nightly tidy-up done: %d cards updated, %d compressed" %
                (out["cards_updated"], out["compressed"]))
    return out


# ---------- weekly: usage, lifecycle, duplicates, structure ----------

def _ref_days(n: vault.Note):
    ds = [util.days_since(n.meta.get(k)) for k in ("last_used", "updated", "date")]
    ds = [d for d in ds if d is not None]
    if not ds:
        try:
            return (dt.datetime.now() - dt.datetime.fromtimestamp(n.path.stat().st_mtime)).days
        except Exception:
            return 0
    return min(ds)


def weekly() -> dict:
    cfg = config.load()
    life = cfg["lifecycle"]
    events.emit("working", "librarian", "Weekly review started")
    snapshot("Before weekly librarian run")
    applied = apply_approvals()
    index.reindex(quiet=True)
    db = index.connect()

    # usage -> frontmatter
    cutoff = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    last, count = {}, {}
    for u in events.usage():
        nid, ts = u.get("id"), str(u.get("ts"))
        last[nid] = max(last.get(nid, ""), ts)
        if ts >= cutoff:
            count[nid] = count.get(nid, 0) + 1
    inbound = {}
    for r in db.execute("SELECT src, dst FROM edges"):
        t = index.resolve_link(db, r["dst"])
        if t and t != r["src"]:
            inbound[t] = inbound.get(t, 0) + 1
    broken = []
    for r in db.execute("SELECT e.src, e.dst FROM edges e JOIN notes n ON n.id=e.src "
                        "WHERE e.kind='link' AND n.type != 'doc'"):
        if not index.resolve_link(db, r["dst"]):
            broken.append("[[%s]] in %s" % (r["dst"], r["src"]))
    db.close()

    dormant, archived, woke, orphans = [], [], [], []
    # Only the helpers' own notes have a lifecycle. The owner's Obsidian notes are never moved or changed.
    notes = [n for n in vault.all_notes() if n.writable and not n.is_doc and vault.top(n) != "00-Core"]
    for n in notes:
        in_archive = vault.is_archive(n.rel)
        changed = False
        for k, v in (("last_used", last.get(n.id)), ("use_count_90d", count.get(n.id, 0)),
                     ("inbound_links", inbound.get(n.id, 0))):
            if v is not None and n.meta.get(k) != v and not (k != "last_used" and v == 0 and k not in n.meta):
                n.meta[k] = v
                changed = True
        if in_archive:
            if changed:
                n.save()
            continue
        days, status = _ref_days(n), n.meta.get("status", "active")
        protected = bool(n.meta.get("pin")) or n.type in ("decision", "person") or n.meta.get("scope") == "private"
        if status == "dormant" and days <= life["dormant_days"]:
            n.meta["status"], changed = "active", True
            woke.append(n.id)
        elif status == "active" and days > life["dormant_days"] and not n.meta.get("pin"):
            n.meta["status"], changed = "dormant", True
            dormant.append(n.id)
            status = "dormant"
        if (status == "dormant" and days > life["archive_days"] and not protected
                and inbound.get(n.id, 0) < 3):
            n.meta["status"] = "archived"
            n.meta["archived_on"] = util.today()
            n.save()
            adir = vault.archive_dir(n)
            adir.mkdir(parents=True, exist_ok=True)
            dest = adir / n.path.name
            if dest.exists():
                dest = adir / ("%s-%s.md" % (n.path.stem, util.sha(n.id)[:5]))
            shutil.move(str(n.path), str(dest))
            archived.append(n.id)
            continue
        if n.type in vault.ENTITY_TYPES and not inbound.get(n.id) and not n.links() and n.id != "preferences":
            orphans.append(n.id)
        if changed:
            n.save()
    if dormant:
        events.emit("dormant", "librarian", "%d notes went dormant (unused %d+ days)" %
                    (len(dormant), life["dormant_days"]), notes=dormant[:60])
    if archived:
        events.emit("archived", "librarian", "Archived %d notes (unused %d+ days)" %
                    (len(archived), life["archive_days"]), removed=archived[:60])
    if woke:
        events.emit("woke", "librarian", "%d notes back to active" % len(woke), notes=woke[:60])

    # duplicate cards -> merge proposals
    cards = [n for n in notes if n.type in vault.ENTITY_TYPES and n.id != "preferences"
             and not vault.is_archive(n.rel) and n.id not in archived]
    merges = 0
    for i, a in enumerate(cards):
        for b in cards[i + 1:]:
            an, bn = util.norm(a.title), util.norm(b.title)
            same = (util.squash(a.title) == util.squash(b.title)
                    or any(util.squash(x) == util.squash(b.title) for x in a.aliases)
                    or any(util.squash(x) == util.squash(a.title) for x in b.aliases)
                    or (min(len(an), len(bn)) >= 5 and difflib.SequenceMatcher(None, an, bn).ratio() >= 0.88))
            if same:
                keep, drop = (a, b) if inbound.get(a.id, 0) >= inbound.get(b.id, 0) else (b, a)
                if filer.add_review("Merge proposals", "- [ ] Merge [[%s]] into [[%s]] · “%s” looks like “%s”"
                                    % (drop.id, keep.id, drop.title, keep.title)):
                    merges += 1

    # deletion candidates: long-archived notes and stale agent scratch files
    deletes = 0
    for n in vault.all_notes():
        if not n.writable or n.is_doc:
            continue
        top = vault.top(n)
        old_archive = (top == "99-Archive" and not n.meta.get("pin")
                       and (util.days_since(n.meta.get("archived_on")) or 0) >= life["delete_after_archived_days"])
        stale_agent = top == "90-Agent" and _ref_days(n) > life["agent_scratch_days"] and not n.meta.get("pin")
        if old_archive or stale_agent:
            why = "archived since %s" % n.meta.get("archived_on") if old_archive else "agent scratch, unused"
            if filer.add_review("Ready to delete", "- [ ] Delete [[%s]] · %s (%s)" % (n.id, n.title, why)):
                deletes += 1

    index.reindex(quiet=True)
    try:
        index.embed_pending()
    except Exception:
        pass
    unfiled = len(filer.inbox_items())
    out = {"when": util.stamp(), "applied": applied, "dormant": len(dormant), "archived": len(archived),
           "reactivated": len(woke), "merge_proposals": merges, "delete_proposals": deletes,
           "broken_links": broken[:15], "broken_count": len(broken), "orphans": orphans[:20],
           "unfiled": unfiled}
    s = _state()
    s["weekly"] = out
    s["last_weekly"] = util.today()
    _save_state(s)
    build_brief()
    write_report()
    from . import obsidian
    obsidian.mirror_cards()
    snapshot("Weekly librarian run")
    events.emit("done", "librarian", "Weekly review done: %d dormant, %d archived, %d merge proposals" %
                (len(dormant), len(archived), merges))
    return out


def pending_review() -> int:
    p = vault.core_file("review")
    if not p.exists():
        return 0
    return len(re.findall(r"^\s*-\s*\[ \]", p.read_text(encoding="utf-8"), flags=re.M))


def write_report() -> None:
    s = _state()
    n, w = s.get("nightly") or {}, s.get("weekly") or {}
    L = ["# Librarian report", "", "Approve items in [[Review]] by ticking their boxes. Open items: %d." %
         pending_review(), ""]
    if n:
        L += ["## Last nightly run (%s)" % n.get("when", ""), "- Cards updated: %s" % n.get("cards_updated", 0),
              "- Cards compressed to cap: %s" % n.get("compressed", 0),
              "- Brief rebuilt: %s" % ("yes" if n.get("brief_changed") else "no change"),
              "- Obsidian notes read by the helpers' model: %s (still waiting: %s)" % (
                  n.get("notes_read", 0), n.get("notes_waiting", 0)),
              "- Model: %s" % n.get("model", "")]
        L += ["- Applied: %s" % a for a in n.get("applied", [])] + [""]
    if w:
        L += ["## Last weekly review (%s)" % w.get("when", ""), "- Went dormant: %s" % w.get("dormant", 0),
              "- Archived: %s" % w.get("archived", 0), "- Back to active: %s" % w.get("reactivated", 0),
              "- New merge proposals: %s" % w.get("merge_proposals", 0),
              "- New delete proposals: %s" % w.get("delete_proposals", 0),
              "- Inbox items still unfiled: %s" % w.get("unfiled", 0),
              "- Broken links: %s" % w.get("broken_count", 0)]
        L += ["    - %s" % b for b in w.get("broken_links", [])]
        if w.get("orphans"):
            L += ["- Cards with no links: " + ", ".join("[[%s]]" % o for o in w["orphans"])]
        L += ["- Applied: %s" % a for a in w.get("applied", [])] + [""]
    vault.core_file("report").write_text("\n".join(L) + "\n", encoding="utf-8")


def review_items() -> list:
    p = vault.core_file("review")
    if not p.exists():
        return []
    out, section = [], ""
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        m = re.match(r"\s*-\s*\[ \]\s*(.+)$", line)
        if m:
            out.append({"section": section, "text": m.group(1).strip()})
    return out


def review_act(text: str, action: str) -> list:
    """Approve (tick and apply now) or dismiss (remove) one open review item."""
    p = vault.core_file("review")
    if not p.exists():
        return []
    lines, hit = [], False
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\s*-\s*)\[ \](\s*)(.+)$", line)
        if m and not hit and m.group(3).strip() == text.strip():
            hit = True
            if action == "dismiss":
                continue
            line = "%s[x]%s%s" % (m.group(1), m.group(2), m.group(3))
        lines.append(line)
    if not hit:
        return []
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if action == "dismiss":
        events.emit("dismissed", "you", "Dismissed: " + text[:80])
        return ["Dismissed"]
    snapshot("Before applying an approval")
    applied = apply_approvals()
    index.reindex(quiet=True)
    build_brief()
    from . import obsidian
    obsidian.mirror_cards()
    return applied
