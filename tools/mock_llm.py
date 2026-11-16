"""Tiny stand-in for oMLX, for testing without a model. Usage: python3 tools/mock_llm.py [port]"""
import hashlib, json, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def answer(system, user):
    if "NOTE TEXT:\n" in user:
        note = user.split("NOTE TEXT:\n", 1)[1]
        known = re.findall(r"^- (\S+) \| (.+?) \| ", user, re.M)
        line = next((l.strip() for l in note.splitlines() if l.strip() and l[0].isalpha()), "A note")
        facts = [{"entity": known[0][0], "fact": line[:150]},
                 {"entity": known[0][0], "fact": "Alex is a Product Manager at Zebra Corp since 1999"}] if known else []
        return {"summary": line[:120], "tags": ["Test Tag", "notes"], "facts": facts,
                "new_entities": [{"name": "Ghost Corp", "type": "org"}]}
    if "KNOWN ENTITIES" in user:
        note = user.split("NOTE:\n", 1)[1]
        known = re.findall(r"^- (\S+) \| (.+?) \| ", user, re.M)
        ents = [i for i, t in known if t.lower() in note.lower()]
        new = [{"name": n, "type": "person"} for n in re.findall(r"\bwith ([A-Z][a-z]+)\b", note)]
        first = re.sub(r"^\d{4}-\d{2}-\d{2}[: ]*", "", note.strip().split("\n")[0])
        facts = [{"entity": ents[0], "fact": m.strip()} for m in re.findall(r"Fact: ([^.\n]+)", note)] if ents else []
        return {"title": " ".join(first.split()[:7]), "date": "", "summary": first[:140],
                "kind": "decision" if "decided" in note.lower() else "event",
                "entities": ents + ["made-up-id"], "new_entities": new + [{"name": "Ghost Corp", "type": "org"}],
                "facts": facts}
    if "NEW ENTRIES" in user:
        entries = user.split("NEW ENTRIES:\n", 1)[1].strip().splitlines()
        return {"now": "Latest: " + entries[0].split("|", 1)[-1].strip() + ".", "summary": "",
                "add_facts": [], "superseded": []}
    if "older timeline" in system:
        return {"summary": "Earlier work covered %d entries." % len(user.splitlines())}
    return {"ok": True, "word": "memory"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send({"data": [{"id": "Qwen3.5-9B-MLX-4bit"}, {"id": "Qwen3.6-27B-MLX-4bit"}, {"id": "bge-m3"}]})

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/embeddings"):
            out = []
            for i, t in enumerate(d["input"]):
                v = [0.0] * 64
                for w in re.findall(r"[a-z0-9]+", t.lower()):
                    v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
                out.append({"index": i, "embedding": v})
            return self._send({"data": out})
        m = d["messages"]
        self._send({"choices": [{"message": {"content": "<think>hm</think>```json\n%s\n```" %
                                             json.dumps(answer(m[0]["content"], m[1]["content"]))}}]})


HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8999), H).serve_forever()
