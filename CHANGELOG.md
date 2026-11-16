# Changelog

## 1.2.0 (2026-11-17)
- Runs without a local model. The helpers can use Claude Code, an Anthropic API key, a local model, or rules only (`brain model`).
- Private notes never go to a cloud model. A daily cap limits cloud calls.
- The background service now starts Python directly, so macOS asks about one clear program. `brain doctor` and the dashboard name the exact file to allow in Full Disk Access.
- New look: the noel. design system (dark Liquid Glass, system SF type, one blue accent). Clearer graph labels.

## 1.1.0 (2026-11-16)
- The Obsidian vault is the source of truth. Cards and the index point into it.
- New tools: `write` (save details into the vault) and `sync` (catch up now).
- The local model reads changed notes and patches the cards.
- Every model-written line is checked against the source text. Unsupported lines are dropped.
- Links are derived: notes to the cards they name, and cards that keep appearing together.
- The index survives a vault that is briefly unreachable. The service restarts itself after an update.

## 1.0.0 (2026-11-16)
- First version: cards, inbox, filer, librarian, MCP gateway, live dashboard.
