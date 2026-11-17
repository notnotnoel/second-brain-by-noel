#!/usr/bin/env python3
"""Retrieval benchmark: tokens to a correct answer, Second Brain vs. a grep-and-read agent.

    python3 tools/bench.py --demo                      # synthetic vault, fully reproducible
    python3 tools/bench.py --vault ~/Obsidian/MyVault --questions my_questions.json

questions.json: [{"id": "q1", "q": "question text", "gold": ["regex", ...]}]
  gold  every regex must appear in what the agent has read (case-insensitive)
The grep agents get no help from the answer key: they pick search words from the question text only.

What is measured (all deterministic, no model calls). "Tokens" = characters / 4, the project's own estimate.
  second-brain   recall(question), then follow pointers the way an agent would: open a note (800 tokens),
                 and if it was trimmed drill into the best-matching section. At most 4 opens.
  grep (smart)   searches the 2 rarest words of the question that exist in the vault; if that misses, the next 2.
                 Each search: per-file counts, matching lines of the top 3 files, then ~60-line windows and
                 finally the whole top file (capped). At most 5 calls.
  grep (naive)   the same flow, but searches every content word of the question at once.
  read-all       read every note containing the 2 rarest words. The baseline the old README used.
A run counts as correct only if every gold regex is present in the text the agent has read.
It does not measure how well a model reasons over that text.
"""
from __future__ import annotations
import argparse, json, os, random, re, shutil, statistics, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STOP = set("the a an of to in on for and or is are was were be been by with at from as it its this that his her "
           "their he she they has have had not no will would can could than then also into about after before "
           "which who what when where why how do does did my me i we you your our mine many much big long any "
           "all use used uses demo agent agents note notes first".split())


def content_words(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+.#-]{2,}", text.lower()) if w not in STOP]


def tok(s):
    return (len(s) + 3) // 4


def has_all(text, gold):
    return all(re.search(g, text, re.I) for g in gold)


# ---------------------------------------------------------------- home + index

def prepare_home(home: Path, vault: Path, folder="Second Brain"):
    """A scratch BRAIN_HOME that reads `vault` the way the real service does. Cards come from the
    read-only mirror in <vault>/<folder>/Cards when it exists."""
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps(
        {"vault": str(home / "vault"), "obsidian": {"path": str(vault), "folder": folder},
         "llm": {"provider": "none"}}))
    os.environ["BRAIN_HOME"] = str(home)
    from brain import index, util, vault as V
    V.init_vault()
    cards = vault / folder / "Cards"
    for p in sorted(cards.glob("*.md")) if cards.exists() else []:
        meta, body = V.parse(p.read_text(encoding="utf-8", errors="replace"))
        typ = next((t for t in V.ENTITY_TYPES if t in [str(x) for x in (meta.get("tags") or [])]), "topic")
        body = "\n".join(l for l in body.splitlines() if not l.startswith("> Copy of"))
        lines = [l.strip() for l in body.splitlines()]
        summary = ""
        if lines and lines[0].startswith("# "):
            summary = next((l for l in lines[1:] if l and not l.startswith("#")), "")
        m = {"id": p.stem, "type": typ, "title": meta.get("title") or p.stem, "aliases": meta.get("aliases") or [],
             "status": meta.get("status") or "active", "scope": "personal", "summary": summary,
             "updated": str(meta.get("updated") or util.today()), "pin": False}
        n = V.Note(V.entity_path(p.stem, typ), m, body.lstrip("\n"))
        n.save()
    index.reindex(full=True, quiet=True)


# ---------------------------------------------------------------- the three agents

OPEN_TOKENS = 800


def pointers(text):
    """Ids an agent would follow, in the order the reply lists them (Linked cards are skipped)."""
    out, block = [], ""
    for line in text.splitlines():
        if line and not line.startswith(("-", " ")):
            block = line
        m = re.match(r"- .*\(id: ([^)]+)\)", line)
        if m and not block.startswith("Linked"):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def best_heading(sections_line, question):
    qw = set(content_words(question))
    best, score = None, 0
    for h in re.split(r"\s·\s", sections_line):
        s = len(qw & set(content_words(h)))
        if s > score:
            best, score = h.strip(), s
    return best


def run_sb(q, gold):
    from brain import mcp_server
    t0, text, calls, ms = time.time(), "", 0, []
    reply = mcp_server.call("recall", {"query": q})
    ms.append((time.time() - t0) * 1000)
    ctx, calls = reply, 1
    if not has_all(ctx, gold):
        opens = 0
        for pid in pointers(reply):
            if opens >= 4 or has_all(ctx, gold):
                break
            t0 = time.time()
            r = mcp_server.call("expand", {"id": pid, "max_tokens": OPEN_TOKENS})
            ms.append((time.time() - t0) * 1000)
            ctx += "\n" + r
            calls += 1
            opens += 1
            if not has_all(ctx, gold) and "#" not in pid and "Sections:" in r and opens < 4:
                h = best_heading(r.split("Sections:", 1)[1].split("\n")[0], q)
                if h:
                    t0 = time.time()
                    r2 = mcp_server.call("expand", {"id": pid + "#" + h, "max_tokens": OPEN_TOKENS})
                    ms.append((time.time() - t0) * 1000)
                    ctx += "\n" + r2
                    calls += 1
                    opens += 1
    return ctx, calls, ms


def md_files(vault):
    for p in sorted(vault.rglob("*.md")):
        rel = p.relative_to(vault).parts
        if any(x.startswith(".") for x in rel) or rel[0] == "Second Brain":
            continue    # the helpers' own folder (card mirror) is not part of a no-memory baseline
        yield p, "/".join(rel)


def df_map(vault):
    df = {}
    for p, rel in md_files(vault):
        for w in set(content_words(p.read_text(encoding="utf-8", errors="replace"))):
            df[w] = df.get(w, 0) + 1
    return df


def pick_terms(question, df, k=2, skip=0):
    ws = [w for w in dict.fromkeys(content_words(question)) if df.get(w)]
    ws.sort(key=lambda w: (df[w], -len(w)))
    return [re.escape(w) for w in ws[skip:skip + k]]


def run_grep_once(vault, kw_regex, gold, ctx="", calls=0, limit=5):
    rx = re.compile(kw_regex, re.I)
    files = []
    for p, rel in md_files(vault):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [i for i, l in enumerate(lines) if rx.search(l)]
        if hits:
            files.append((len(hits), rel, lines, hits))
    files.sort(key=lambda f: -f[0])
    listing = "\n".join("%s:%d" % (rel, n) for n, rel, _, _ in files[:12])
    ctx += listing
    calls += 1
    if has_all(ctx, gold) or not files:
        return ctx, calls
    chunk = []
    for n, rel, lines, hits in files[:3]:
        chunk += ["%s:%d:%s" % (rel, i + 1, lines[i][:220]) for i in hits[:15]]
    ctx += "\n" + "\n".join(chunk)
    calls += 1
    for n, rel, lines, hits in files[:3]:
        if has_all(ctx, gold) or calls >= limit:
            break
        i = hits[0]
        ctx += "\n" + "\n".join(lines[max(0, i - 30): i + 30])[:12000]
        calls += 1
    for n, rel, lines, hits in files[:2]:
        if has_all(ctx, gold) or calls >= limit:
            break
        ctx += "\n" + "\n".join(lines)[:12000]
        calls += 1
    return ctx, calls


def run_grep(vault, question, df, gold):
    ctx, calls = "", 0
    for skip in (0, 2):
        terms = pick_terms(question, df, 2, skip)
        if not terms:
            break
        ctx, calls = run_grep_once(vault, "|".join(terms), gold, ctx, calls, limit=calls + 4)
        if has_all(ctx, gold) or calls >= 7:
            break
    return ctx, calls


def run_naive(vault, question, gold):
    words = "|".join(re.escape(w) for w in dict.fromkeys(content_words(question)))
    return run_grep_once(vault, words, gold) if words else ("", 0)


def read_all(vault, question, df):
    terms = pick_terms(question, df, 2)
    if not terms:
        return 0
    rx = re.compile("|".join(terms), re.I)
    total = 0
    for p, rel in md_files(vault):
        t = p.read_text(encoding="utf-8", errors="replace")
        if rx.search(t):
            total += tok(t)
    return total


# ---------------------------------------------------------------- run + report

def run(vault, questions, home):
    prepare_home(home, vault)
    df = df_map(vault)
    rows = []
    for q in questions:
        gold = q["gold"]
        ctx, calls, ms = run_sb(q["q"], gold)
        sb = {"tokens": tok(ctx), "ok": has_all(ctx, gold), "calls": calls, "ms": ms}
        c1, n1 = run_grep(vault, q["q"], df, gold)
        c2, n2 = run_naive(vault, q["q"], gold)
        rows.append({"id": q["id"], "q": q["q"], "sb": sb,
                     "grep": {"tokens": tok(c1), "ok": has_all(c1, gold), "calls": n1},
                     "naive": {"tokens": tok(c2), "ok": has_all(c2, gold), "calls": n2},
                     "readall": read_all(vault, q["q"], df)})
    return rows


def overhead():
    from brain import mcp_server, recall
    schema = tok(json.dumps(mcp_server.TOOLS))
    return {"tool_schemas": schema, "brief": tok(recall.brief())}


def report(rows, over, label, vault_note):
    def agg(key):
        ok = [r for r in rows if r[key]["ok"]]
        t = [r[key]["tokens"] for r in rows]
        return {"correct": len(ok), "of": len(rows), "median_tokens": int(statistics.median(t)),
                "total_tokens": sum(t), "mean_calls": round(statistics.mean(r[key]["calls"] for r in rows), 1)}
    A = {k: agg(k) for k in ("sb", "grep", "naive")}
    ra = sum(r["readall"] for r in rows)
    both = [r for r in rows if r["sb"]["ok"] and r["grep"]["ok"]]
    ratios = [1 - r["sb"]["tokens"] / r["grep"]["tokens"] for r in both if r["grep"]["tokens"]]
    ra_ratios = [1 - r["sb"]["tokens"] / r["readall"] for r in rows if r["sb"]["ok"] and r["readall"]]
    ms = [m for r in rows for m in r["sb"]["ms"]]
    fixed = over["tool_schemas"] + over["brief"]
    per_q = (sum(r["grep"]["tokens"] for r in both) - sum(r["sb"]["tokens"] for r in both)) / max(1, len(both))
    out = {"label": label, "vault": vault_note, "agents": A, "readall_total": ra,
           "median_saving_vs_grep": round(100 * statistics.median(ratios), 1) if ratios else None,
           "median_saving_vs_readall": round(100 * statistics.median(ra_ratios), 1) if ra_ratios else None,
           "server_ms_median": round(statistics.median(ms), 1) if ms else None,
           "overhead_tokens": over, "break_even_questions": round(fixed / per_q, 1) if per_q > 0 else None,
           "rows": rows}
    return out


def print_report(o):
    A = o["agents"]
    print("\n== %s ==  (%s)" % (o["label"], o["vault"]))
    print("%-22s %9s %14s %12s %10s" % ("method", "correct", "median tokens", "total tokens", "calls/q"))
    for k, name in (("sb", "second-brain"), ("grep", "grep (smart kw)"), ("naive", "grep (naive kw)")):
        a = A[k]
        print("%-22s %6d/%-2d %14d %12d %10s" % (name, a["correct"], a["of"], a["median_tokens"],
                                                  a["total_tokens"], a["mean_calls"]))
    print("%-22s %9s %14s %12d" % ("read-all (old README)", "n/a", "", o["readall_total"]))
    print("median saving vs smart grep : %s%%  (questions both answered)" % o["median_saving_vs_grep"])
    print("median saving vs read-all   : %s%%" % o["median_saving_vs_readall"])
    print("server time per call (median): %s ms" % o["server_ms_median"])
    ov = o["overhead_tokens"]
    print("fixed cost per session: tool schemas %d + brief %d tokens; break-even after %s questions" %
          (ov["tool_schemas"], ov["brief"], o["break_even_questions"]))
    print("\nper question (tokens; * = wrong/incomplete):")
    for r in o["rows"]:
        f = lambda m: "%6d%s" % (r[m]["tokens"], "" if r[m]["ok"] else "*")
        print("  %-5s sb %s  grep %s  naive %s  read-all %6d  | %s" %
              (r["id"], f("sb"), f("grep"), f("naive"), r["readall"], r["q"][:58]))


# ---------------------------------------------------------------- synthetic demo vault

def make_demo(root: Path):
    rnd = random.Random(7)
    subj = ["the team", "the stack", "the region", "the licence", "the budget", "the rollout", "the pipeline",
            "the cluster", "the roadmap", "a vendor", "the review", "the migration", "the dashboard", "the audit"]
    verb = ["was discussed", "needs another look", "stayed unchanged", "was postponed", "was measured twice",
            "is tracked weekly", "depends on staffing", "was compared with last year", "was documented",
            "slipped a little", "was approved in principle", "remains an open item"]
    tail = ["during planning", "after the offsite", "in the second quarter", "for the pilot", "across both sites",
            "with the platform group", "before the release", "in the weekly sync", "for the board pack"]

    def filler(n_chars, seed_words=()):
        out, size = [], 0
        while size < n_chars:
            s = "%s %s %s%s." % (rnd.choice(subj).capitalize(), rnd.choice(verb), rnd.choice(tail),
                                 (" (%s)" % rnd.choice(seed_words)) if seed_words and rnd.random() < .2 else "")
            out.append(s)
            size += len(s) + 1
        return " ".join(out)

    def w(rel, text):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    w("Work/Northwind Logistics.md", "# Northwind Logistics\n\nSam's employer since 2024. Strategy and operations.\n\n"
      "## Role\n\nProgram lead, fleet digitisation.\n\n## Scope\n\n" + filler(1500, ["team", "stack"]) +
      "\n\nSam co-leads a team of 34 people across Rotterdam, Singapore and Chicago.\n\n## Results\n\n" + filler(2500) +
      "\n\n## Timeline\n\n" + filler(2000))
    w("Work/Harbor Project.md", "# Harbor Project\n\nCustomer portal for container tracking.\n\n## Why\n\n" + filler(1500) +
      "\n\n## Stack\n\nFront end is Svelte with Vite. Back end is Go with gRPC. Data lives in one Parquet file "
      "of 2.4 GB opened read-only. Hosted on eu-west-2 behind a CDN.\n\n## Guards\n\nEach visitor gets 20 requests "
      "per 5 minutes, and a nightly spend cap applies.\n\n## Log\n\n" + filler(3000, ["stack", "region", "team"]))
    w("Work/Atlas Project.md", "# Atlas Project\n\nInternal planning tool.\n\n## Stack\n\nPython with Django and Postgres. "
      "Hosted on us-east-1.\n\n## Notes\n\n" + filler(4000, ["stack", "team", "region"]))
    for name, pat, extra in (("Alpha", "retry loop with a rules verdict", "23 checks"),
                             ("Bravo", "parallel map-reduce over batches", "9 signals"),
                             ("Charlie", "router with human approval", "4 queues"),
                             ("Delta", "evaluator and optimizer pair", "17 clause types")):
        w("Agents/Agent %s.md" % name, "# Agent %s\n\n## 1. Overview\n\n%s\n\n## 2. Design pattern: %s\n\n"
          "Runs as a %s. Uses %s.\n\n## 3. Data\n\n%s\n\n## 4. Risks\n\n%s\n" %
          (name, filler(900), pat, pat, extra, filler(1800, ["team"]), filler(1500)))
    for org, role, dates in (("Contoso", "Analyst", "Mar 2019 to Feb 2021"), ("Fabrikam", "Consultant", "Mar 2021 to Dec 2023"),
                             ("Tailspin", "Research intern", "Jun to Aug 2018")):
        w("Career/%s.md" % org, "# %s\n\nRole: %s. Dates: %s.\n\n%s\n" % (org, role, dates, filler(900)))
    w("Tech/Kubernetes Notes.md", "# Kubernetes Notes\n\n" + "\n\n".join(
        "## Topic %d\n\n%s" % (i, filler(3000, ["team", "stack", "region", "licence"])) for i in range(40)))
    w("Tech/Postgres Notes.md", "# Postgres Notes\n\n" + "\n\n".join(
        "## Topic %d\n\n%s" % (i, filler(3000, ["team", "stack", "budget"])) for i in range(30)) +
      "\n\n## Licensing\n\nPostgres uses the PostgreSQL licence, a permissive BSD-style licence.\n")
    w("Tech/Tool shortlist.md", "# Tool shortlist\n\n## Kestrel (small router model)\n\nLicence: Apache-2.0. Built on a Qwen base. "
      "Not an agent framework.\n\n## Other tools\n\n" + filler(2500))
    w("Tech/Mac mini setup.md", "# Mac mini setup\n\n## Memory\n\nThe GPU can use about 16 GB of the 24 GB by default.\n\n## Disk\n\n" + filler(1200))
    cards = "Second Brain/Cards/"
    w(cards + "sam-rivera.md", "---\ntitle: Sam Rivera\naliases: [Sam, Sam Rivera]\nstatus: active\nupdated: 2026-11-01\n"
      "tags: [second-brain/card, person]\n---\n> Copy of a Second Brain card. Do not edit here: it is overwritten.\n\n"
      "# Sam Rivera\nOwner of this memory. Program lead at Northwind Logistics.\n\n## Key facts\n"
      "- Program lead at Northwind Logistics since 2024\n- Previously a consultant at Fabrikam\n")
    w(cards + "northwind.md", "---\ntitle: Northwind Logistics\naliases: [Northwind]\nstatus: active\nupdated: 2026-11-01\n"
      "tags: [second-brain/card, org]\n---\n> Copy of a Second Brain card. Do not edit here: it is overwritten.\n\n"
      "# Northwind Logistics\nSam's employer.\n\n## Key facts\n- Fleet digitisation programme\n")
    w(cards + "harbor.md", "---\ntitle: Harbor Project\naliases: [Harbor]\nstatus: active\nupdated: 2026-11-01\n"
      "tags: [second-brain/card, project]\n---\n> Copy of a Second Brain card. Do not edit here: it is overwritten.\n\n"
      "# Harbor Project\nCustomer portal for container tracking.\n\n## Key facts\n- Live demo for shippers\n")
    qs = [
        {"id": "E1", "q": "What stack does Harbor use?", "gold": ["Svelte", "Go with gRPC"]},
        {"id": "E2", "q": "How big is the data file behind Harbor and where is it hosted?", "gold": ["2\\.4 GB", "eu-west-2"]},
        {"id": "E3", "q": "What request limit does Harbor apply per visitor?", "gold": ["20 requests per 5 minutes"]},
        {"id": "K1", "q": "How large is the team Sam co-leads?", "gold": ["team of 34"]},
        {"id": "K2", "q": "Which agent ends in a rules verdict after retrying?", "gold": ["retry loop with a rules verdict"]},
        {"id": "K3", "q": "Which agent has a human approval step?", "gold": ["router with human approval"]},
        {"id": "K4", "q": "How many clause types does the evaluator agent check?", "gold": ["17 clause types"]},
        {"id": "K5", "q": "What licence does the small router model use?", "gold": ["Apache-2\\.0"]},
        {"id": "K6", "q": "How much GPU memory can the Mac mini use by default?", "gold": ["16 GB of the 24 GB"]},
        {"id": "C1", "q": "When was the research internship?", "gold": ["Jun to Aug 2018"]},
        {"id": "C2", "q": "Which companies has Sam worked at before Northwind?",
         "gold": ["Contoso", "Fabrikam", "Tailspin"]},
        {"id": "C3", "q": "Which region hosts the Atlas tool?", "gold": ["us-east-1"]},
        {"id": "P1", "q": "What is Sam's headcount?", "gold": ["team of 34"]},
        {"id": "P2", "q": "Which cloud datacentre serves Harbor?", "gold": ["eu-west-2"]},
        {"id": "P3", "q": "What did Sam do before joining Northwind?", "gold": ["Contoso", "Fabrikam"]},
    ]
    return qs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use the built-in synthetic vault")
    ap.add_argument("--vault", help="path to an Obsidian vault")
    ap.add_argument("--questions", help="questions.json")
    ap.add_argument("--home", help="scratch BRAIN_HOME (default: a temp folder)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--json", help="write the full result here")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="brain-bench-"))
    if a.demo:
        vault = tmp / "vault"
        qs = make_demo(vault)
        note = "synthetic demo vault"
    elif a.vault and a.questions:
        vault, qs = Path(a.vault).expanduser(), json.loads(Path(a.questions).read_text())
        note = "%d questions" % len(qs)
    else:
        ap.error("give --demo, or --vault and --questions")
    home = Path(a.home) if a.home else tmp / "home"
    rows = run(vault, qs, home)
    o = report(rows, overhead(), a.label, note)
    print_report(o)
    if a.json:
        Path(a.json).write_text(json.dumps(o, indent=1))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
