#!/bin/bash
# Stops the Second Brain service. Your notes in ~/SecondBrain/vault are left untouched.
LABEL="com.secondbrain.service"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
[ -f "$HOME/Library/LaunchAgents/$LABEL.plist" ] && mv "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/.Trash/" 2>/dev/null
[ -L "$HOME/.local/bin/brain" ] && unlink "$HOME/.local/bin/brain"
command -v claude >/dev/null 2>&1 && claude mcp remove --scope user second-brain >/dev/null 2>&1
echo "Service stopped and disconnected from Claude Code."
echo "Still in place: ~/SecondBrain (notes, config), the second-brain entry in Claude desktop's config,"
echo "the memory rule in ~/.claude/CLAUDE.md and the PATH line in ~/.zshrc. Remove those by hand if you want."
