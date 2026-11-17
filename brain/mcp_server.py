"""Memory gateway: a minimal MCP server over stdio (JSON-RPC, one message per line).
Any MCP client (Claude Code, Claude desktop, Cursor, ...) can launch it.

  BRAIN_SCOPES=personal,work   scopes this agent may read (private is never included by default)
  BRAIN_AGENT=claude-code      name shown in the activity log
"""
from __future__ import annotations
import json, os, sys
from . import __version__, index, recall

AGENT = os.environ.get("BRAIN_AGENT", "agent")
SCOPES = [s.strip() for s in os.environ.get("BRAIN_SCOPES", "").split(",") if s.strip()] or None

TOOLS = [
    {"name": "brief",
     "description": "Load the owner's core context (who they are, preferences, active projects). "
                    "Call once at the start of a task. About 400 tokens.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "recall",
     "description": "Look up stored memory before making a decision. Name the people, projects or topics "
                    "involved; returns their cards, the best-matching note sections (open one with expand) and "
                    "related notes. Capped and cheap.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "What you need to know, including entity names"},
         "max_tokens": {"type": "integer", "description": "Budget, default 500"}},
         "required": ["query"]}},
    {"name": "expand",
     "description": "Read one note, or just one section as 'id#Heading', by an id from recall. Prefer the "
                    "section: it is far cheaper than the whole note. Notes come from the owner's Obsidian vault.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "max_tokens": {"type": "integer", "description": "Budget, default 500, up to 6000"}},
         "required": ["id"]}},
    {"name": "write",
     "description": "Write details into the owner's Obsidian vault (the source of truth): a plan, findings, a "
                    "write-up. Creates a new note, or with mode='append' adds a dated section to an existing "
                    "one. Never overwrites. Use log for a one-line record; use write for anything longer.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Note name, e.g. 'Acme launch pricing test'"},
         "content": {"type": "string", "description": "Markdown"},
         "folder": {"type": "string", "description": "Folder inside the vault, e.g. 'Projects/Acme launch'. "
                                                      "Optional; defaults to 'Second Brain/Notes'"},
         "mode": {"type": "string", "enum": ["create", "append"]}},
         "required": ["title", "content"]}},
    {"name": "sync",
     "description": "Call after you (or the owner) changed notes in the Obsidian vault directly. Brings the "
                    "index level with the notes at once and queues the helpers' model to patch the cards.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "log",
     "description": "Save something to memory: a finished task, a decision and its reason, or a fact the owner "
                    "asked to remember. One or two dated sentences; name the entities involved.",
     "inputSchema": {"type": "object", "properties": {
         "entry": {"type": "string"},
         "entities": {"type": "array", "items": {"type": "string"},
                      "description": "Names or ids of the cards this is about"},
         "scope": {"type": "string", "enum": ["personal", "work", "private"]}},
         "required": ["entry"]}},
]


def call(name: str, args: dict) -> str:
    if name == "brief":
        return recall.brief()
    if name == "recall":
        index.reindex(quiet=True, max_age=2.0)     # a scan per call was over half the server time
        return recall.recall(args.get("query", ""), SCOPES, args.get("max_tokens"), AGENT)
    if name == "expand":
        return recall.expand(args.get("id", ""), SCOPES, AGENT, args.get("max_tokens"))
    if name == "write":
        return recall.write_note(args.get("title", ""), args.get("content", ""), args.get("folder") or "",
                                 args.get("mode") or "create", AGENT)
    if name == "sync":
        return recall.sync(AGENT)
    if name == "log":
        return recall.log(args.get("entry", ""), args.get("entities"), AGENT, args.get("scope") or "personal")
    raise ValueError("Unknown tool: %s" % name)


def handle(msg: dict):
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "second-brain", "version": __version__},
                  "instructions": "Shared memory. Cards here point to the details in the owner's Obsidian vault. "
                                  "Call brief() at the start and recall() before decisions; open the note paths "
                                  "it returns (or expand the section ids it lists). Save one-liners with log(), longer details with "
                                  "write(); after editing vault files directly, call sync()."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text = call(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:
            result = {"content": [{"type": "text", "text": "Memory error: %s" % e}], "isError": True}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    elif mid is None:
        return None  # notification
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: " + method}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
