"""Background service: watches the inbox, runs the helpers on schedule, serves the dashboard.
Listens on 127.0.0.1 only."""
from __future__ import annotations
import datetime as dt, json, os, sys, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from . import config, events, filer, index, librarian, llm, obsidian, recall, util, vault


def _process_identity() -> dict:
    """What macOS sees as this process. Full Disk Access must be given to 'grant'."""
    exe = os.path.realpath(sys.executable)
    try:
        import subprocess
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True,
                              text=True, timeout=3).stdout.strip()
        if comm.startswith("/"):
            exe = os.path.realpath(comm)
    except Exception:
        pass
    grant = exe
    if ".app/Contents/MacOS/" in exe:
        grant = exe.split(".app/Contents/MacOS/")[0] + ".app"
    return {"exe": exe, "grant": grant, "pid": os.getpid()}


PROCESS = _process_identity()

JOBS = {"filer": {"state": "idle", "last": ""}, "librarian": {"state": "idle", "last": ""}}
_job_lock = threading.Lock()


def run_job(name: str) -> None:
    helper = "filer" if name == "filer" else "librarian"
    if not _job_lock.acquire(blocking=False):
        return
    JOBS[helper]["state"] = "working"
    try:
        if name == "filer":
            filer.run()
        elif name == "nightly":
            librarian.nightly()
        elif name == "weekly":
            librarian.weekly()
        elif name == "sync":
            patch_gaps(limit=12)
    except Exception as e:
        events.emit("error", helper, "%s failed: %s" % (name, e))
        traceback.print_exc()
    finally:
        JOBS[helper]["state"] = "idle"
        JOBS[helper]["last"] = util.stamp()
        _job_lock.release()


def patch_gaps(limit=None) -> dict:
    """Read Obsidian notes that changed, then update the cards they talk about."""
    r = obsidian.digest_pending(limit)
    if r.get("digested"):
        res = librarian.consolidate()
        if res["cards_updated"]:
            index.reindex(quiet=True)
            librarian.build_brief()
    return r


def _code_stamp() -> float:
    try:
        return max(f.stat().st_mtime for f in Path(__file__).parent.glob("*.py"))
    except Exception:
        return 0.0


def loop() -> None:
    started = _code_stamp()
    flag = config.home() / "sync.request"
    while True:
        try:
            cfg = config.load()
            if _code_stamp() != started and JOBS["filer"]["state"] == JOBS["librarian"]["state"] == "idle":
                events.emit("started", "system", "The app was updated: restarting the service")
                os.execv(sys.executable, [sys.executable, "-m", "brain.cli"] + (sys.argv[1:] or ["serve"]))
            if filer.inbox_items():
                run_job("filer")
                if True:                       # new Log notes go straight onto their cards
                    with _job_lock:
                        JOBS["librarian"]["state"] = "working"
                        try:
                            if librarian.consolidate()["cards_updated"]:
                                index.reindex(quiet=True)
                                librarian.build_brief()
                        finally:
                            JOBS["librarian"]["state"] = "idle"
            else:
                index.reindex()
            asked = flag.exists()
            if asked:
                try:
                    flag.unlink()
                except Exception:
                    pass
            if obsidian.enabled() and (asked or obsidian.pending_count()):
                if _job_lock.acquire(blocking=False):
                    JOBS["librarian"]["state"] = "working"
                    try:
                        patch_gaps(limit=12 if asked else None)
                    except Exception as e:
                        events.emit("error", "librarian", "Reading Obsidian notes failed: %s" % e)
                    finally:
                        JOBS["librarian"]["state"] = "idle"
                        JOBS["librarian"]["last"] = util.stamp()
                        _job_lock.release()
            s, now = librarian._state(), dt.datetime.now()
            if now.hour >= cfg["schedule"]["nightly_hour"] and s.get("last_nightly") != util.today():
                run_job("nightly")
            last_w = util.days_since(s.get("last_weekly"))
            if s.get("last_nightly") == util.today() and (
                    last_w is None or last_w >= 7 or
                    (now.weekday() == cfg["schedule"]["weekly_day"] and last_w > 0)):
                run_job("weekly")
        except Exception:
            traceback.print_exc()
        time.sleep(max(5, int(config.load()["schedule"]["poll_sec"])))


def status() -> dict:
    db = index.connect()
    by_type, by_status = {}, {}
    for r in db.execute("SELECT type, status, COUNT(*) c FROM notes WHERE path NOT LIKE '00-Core%' GROUP BY 1,2"):
        by_type[r["type"]] = by_type.get(r["type"], 0) + r["c"]
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["c"]
    edges = db.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    vectors = db.execute("SELECT COUNT(*) c FROM vectors").fetchone()["c"]
    db.close()
    s = librarian._state()
    return {"notes": sum(by_type.values()), "by_type": by_type, "by_status": by_status, "edges": edges,
            "vectors": vectors, "inbox": len(filer.inbox_items()), "review": librarian.pending_review(),
            "jobs": JOBS, "last_nightly": (s.get("nightly") or {}).get("when", ""),
            "last_weekly": (s.get("weekly") or {}).get("when", ""),
            "llm": llm.status(), "process": PROCESS,
            "vault": str(vault.root()),
            "obsidian": {"path": str(vault.obs_root() or ""), "reachable": vault.obs_available(),
                         "notes": by_type.get("doc", 0), "waiting": obsidian.pending_count()}}


class Handler(BaseHTTPRequestHandler):
    server_version = "SecondBrain"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "local only"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                html = (Path(__file__).parent / "dashboard.html").read_bytes()
                return self._send(200, html, "text/html")
            if u.path == "/api/graph":
                return self._send(200, index.graph())
            if u.path == "/api/status":
                return self._send(200, status())
            if u.path == "/api/events":
                evs, cursor = events.read(int(q.get("after", ["-1"])[0]))
                return self._send(200, {"events": evs, "cursor": cursor})
            if u.path == "/api/review":
                return self._send(200, {"items": librarian.review_items(), "file": str(vault.core_file("review"))})
            if u.path == "/api/note":
                db = index.connect()
                r = db.execute("SELECT * FROM notes WHERE id=?", (q.get("id", [""])[0],)).fetchone()
                db.close()
                if not r:
                    return self._send(404, {"error": "not found"})
                d = dict(r)
                d["body"] = d["body"][:6000]
                d["file"] = str(vault.abs_path(d["path"])) if d.get("origin") == "obs" else ""
                return self._send(200, d)
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        # The custom header blocks other websites from posting to this local server.
        if not self._local() or self.headers.get("X-Brain") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            if self.path == "/api/capture":
                msg = recall.capture(str(data.get("text", "")), source="dashboard",
                                     scope=str(data.get("scope") or "personal"))
                return self._send(200, {"ok": True, "msg": msg})
            if self.path == "/api/review":
                act = data.get("action")
                if act not in ("approve", "dismiss"):
                    return self._send(400, {"error": "unknown action"})
                with _job_lock:
                    done = librarian.review_act(str(data.get("text", "")), act)
                return self._send(200, {"ok": bool(done), "applied": done})
            if self.path == "/api/run":
                job = data.get("job")
                if job not in ("filer", "nightly", "weekly", "sync"):
                    return self._send(400, {"error": "unknown job"})
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def serve(port=None, schedule: bool = True) -> None:
    vault.init_vault()
    try:
        obsidian.migrate()
    except Exception as e:
        events.emit("error", "system", "Could not set up the Obsidian folder: %s" % e)
    index.reindex(quiet=True)
    port = int(port or config.load()["dashboard_port"])
    if schedule:
        threading.Thread(target=loop, daemon=True).start()
    events.emit("started", "system", "Second Brain service started")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Second Brain is running. Dashboard: http://localhost:%d" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
