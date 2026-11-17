# Changelog

## 1.3.0 (2026-11-17)
- Recall now points at the answer. Every heading section of your notes is indexed, and `recall` returns the best one to three sections as `id#Heading` with a one-line snippet. Agents open a section instead of a whole note. On my vault: 13 of 18 benchmark questions answered (was 9), 19.0k tokens in total (was 44.6k), 2.6 calls per question (was 3.7).
- Related notes keep only close matches. Summaries and snippets are cut at word boundaries, never mid-word.
- `expand` with an unknown section returns the note's outline instead of the whole note. `id#(intro)` opens the text before the first heading.
- Faster: recall median 35.7 ms to 12.1 ms on my vault. Reads scan the vault at most once every 2 seconds; `sync` and `write` still scan at once.
- New `tools/bench.py`: a reproducible token and accuracy benchmark with a synthetic demo vault.
- The index rebuilds itself once on first use after the update (one new table, about 0.3 s for 100 notes).

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
