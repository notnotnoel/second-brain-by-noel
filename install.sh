#!/bin/bash
# Second Brain installer for macOS. Safe to run again: it never overwrites notes or config.
#   bash install.sh [--obsidian "/path/to/your/vault"] [--name "Your name"] [--model auto|local|claude-code|anthropic|none]
set -e
OBSIDIAN="${OBSIDIAN_VAULT:-}"
OWNER="${BRAIN_OWNER:-}"
MODEL="${BRAIN_MODEL:-auto}"
while [ $# -gt 0 ]; do
  case "$1" in
    --obsidian) OBSIDIAN="$2"; shift 2 ;;
    --name) OWNER="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_HOME="${BRAIN_HOME:-$HOME/SecondBrain}"
APP="$BRAIN_HOME/app"
LABEL="com.secondbrain.service"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

say "1/6  Finding Python"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  [ -n "$c" ] && [ -x "$c" ] || continue
  if [ "$c" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
  if "$c" -c 'import sys, sqlite3; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; fi
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer was not found."
  echo "Install it with:  xcode-select --install   (or from https://www.python.org/downloads/)"
  echo "Then run this installer again."
  exit 1
fi
echo "Using $PY ($("$PY" --version 2>&1))"

say "2/6  Copying the app to $APP"
mkdir -p "$APP" "$BRAIN_HOME/logs"
for d in brain bin skills tools; do
  mkdir -p "$APP/$d"
  cp -R "$SRC/$d/." "$APP/$d/"
done
cp "$SRC/seed.json" "$SRC/README.md" "$SRC/uninstall.sh" "$APP/" 2>/dev/null || true
chmod +x "$APP/bin/brain" "$APP/uninstall.sh" 2>/dev/null || true
echo "$PY" > "$APP/.python"
BRAIN="$APP/bin/brain"
export BRAIN_HOME

say "3/6  Config, Obsidian vault and local model"
if [ -z "$OBSIDIAN" ] && [ ! -f "$BRAIN_HOME/config.json" ]; then
  ICLOUD="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents"
  if [ -d "$ICLOUD" ] && [ "$(find "$ICLOUD" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "1" ]; then
    OBSIDIAN="$(find "$ICLOUD" -mindepth 1 -maxdepth 1 -type d)"
    echo "Found one Obsidian vault in iCloud: $OBSIDIAN"
  elif [ -t 0 ]; then
    printf "Path to your Obsidian vault (drag the folder here, or press Enter to skip): "
    read -r OBSIDIAN
    OBSIDIAN="${OBSIDIAN%/}"; OBSIDIAN="${OBSIDIAN//\\ / }"
  fi
fi
if [ -n "$OBSIDIAN" ] && [ ! -d "$OBSIDIAN" ]; then
  echo "No folder at: $OBSIDIAN  (continuing without Obsidian; add it to config.json later)"; OBSIDIAN=""
fi
if [ -z "$OWNER" ]; then OWNER="$(id -F 2>/dev/null || whoami)"; fi
if [ ! -f "$BRAIN_HOME/config.json" ]; then
  "$PY" - "$BRAIN_HOME" "$OBSIDIAN" "$MODEL" <<'PYEOF'
import json, os, sys
home = sys.argv[1]
obsidian = sys.argv[2]
model = sys.argv[3] if len(sys.argv) > 3 else "auto"
key = ""
def find(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k.lower() in ("api_key", "apikey") and isinstance(v, str) and v:
                return v
            r = find(v)
            if r:
                return r
    return ""
try:
    key = find(json.load(open(os.path.expanduser("~/.omlx/settings.json"))))
except Exception:
    pass
port = 8000
try:
    s = json.load(open(os.path.expanduser("~/.omlx/settings.json")))
    port = int((s.get("server") or {}).get("port") or s.get("port") or 8000)
except Exception:
    pass
cfg = {"vault": os.path.join(home, "vault"),
       "llm": {"provider": model, "base_url": "http://localhost:%d/v1" % port, "api_key": key,
               "model_fast": "auto", "model_deep": "auto", "embed_model": "auto"},
       "dashboard_port": 8765}
if obsidian:
    cfg["obsidian"] = {"path": obsidian, "folder": "Second Brain", "exclude": [],
                       "scope_by_folder": {}, "no_auto_link": []}
json.dump(cfg, open(os.path.join(home, "config.json"), "w"), indent=2)
os.chmod(os.path.join(home, "config.json"), 0o600)
print("Created config.json (oMLX key %s, port %d)" % ("found in ~/.omlx" if key else "not found: add it if oMLX asks for one", port))
PYEOF
else
  echo "Keeping your existing config.json"
fi

say "4/6  Creating the starter cards"
sed "s/__OWNER__/$(printf '%s' "$OWNER" | sed 's/[&/\\]/\\&/g')/g" "$APP/seed.json" > "$BRAIN_HOME/seed.local.json"
"$BRAIN" init --seed "$BRAIN_HOME/seed.local.json"

say "5/6  Starting the background service"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/bin"
ln -sf "$BRAIN" "$HOME/.local/bin/brain"
"$BRAIN" service
if ! grep -q 'second-brain-path' "$HOME/.zshrc" 2>/dev/null; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"  # second-brain-path\n' >> "$HOME/.zshrc"
fi
if [ -d "$HOME/.claude" ]; then
  mkdir -p "$HOME/.claude/skills/remember"
  cp "$APP/skills/remember/SKILL.md" "$HOME/.claude/skills/remember/SKILL.md"
  echo "Installed the remember skill for Claude Code"
fi

say "6/6  Checking and connecting"
sleep 2
"$BRAIN" doctor || true
echo
"$BRAIN" connect || true

say "Done"
echo "Dashboard:  http://localhost:8765   (opening now)"
if [ -n "$OBSIDIAN" ]; then
  echo "Details:    $OBSIDIAN   (your Obsidian vault; the helpers write only in its 'Second Brain' folder)"
else
  echo "Details:    no Obsidian vault set. Add \"obsidian\": {\"path\": \"...\"} to $BRAIN_HOME/config.json, then: brain restart"
fi
echo "Cards:      $BRAIN_HOME/vault"
echo "Command:    brain help   (open a new Terminal window first)"
open "http://localhost:8765" >/dev/null 2>&1 || true
