# Session Ledger

Tracks Claude Code sessions as a ledger of what was decided, done, pending and abandoned,
so open threads spread across many conversations are not lost, and duplicates can be merged.

## How it works

1. `ingest.py` scans `~/.claude/projects/*/*.jsonl`, skips automated runs (archive, nightly, snaps,
   scratchpad dirs) and sessions with fewer than 2 prompts, and stores a condensed transcript:
   user prompts, assistant prose and one line per tool call. Each message gets a ref (`m12`)
   mapped to its uuid and timestamp.
2. `extract.py` runs headless `claude -p` with structured output on each session whose condensed
   content changed (renames do not trigger it), and stores items in English. Item dates come from
   the cited message refs, never from the model. Extraction runs are not persisted as sessions and
   load no user settings, so they cannot trigger the SessionEnd hook.
3. `embed.py` embeds active items locally (fastembed, `BAAI/bge-small-en-v1.5`) and proposes
   cross-session pairs above cosine 0.85.
4. `judge.py` asks Claude to label each pair: `duplicate`, `resolves` (closes a pending item),
   `related` or `unrelated`. Verdicts are cached and invalidated when a text changes.
5. `cluster.py` files items under subjects (named workstreams spanning sessions). The first run drafts
   a catalog from session summaries; later runs assign only new items and create subjects for items
   no existing subject fits.
6. `server.py` serves the review dashboard and a JSON API over SQLite (`ledger/data/ledger.db`).
   The Subjects tab shows pending/decided/done per subject and copies a markdown brief to continue a
   subject in a new session.

Merges are non-destructive: absorbed items point at the canonical item, every merge is logged
with a snapshot, and can be undone. Pairs marked "not a duplicate" are never suggested again.
Items you edit or merge survive re-extraction.

## Setup

Tools (Python 3.13, uv) and libraries are managed by mise:

```bash
mise trust && mise install && mise run install
```

Requires a logged-in `claude` CLI. The embedding model (~70 MB) downloads on first `mise run embed`.

## Usage

```bash
mise run refresh   # ingest -> extract -> embed -> judge, locked, incremental
mise run serve     # http://127.0.0.1:8765
```

Individual steps: `mise run ingest|extract|embed|judge|cluster`.

## Automatic refresh

A global Claude Code `SessionEnd` hook in `~/.claude/settings.json` runs `bin/on-session-end.sh`,
which starts `mise run refresh` detached and returns immediately. Concurrent session ends queue
behind a lock instead of running twice. Output goes to `ledger/data/refresh.log`.

## Not built yet

- Scheduled refresh as a fallback when sessions are killed without a SessionEnd event.
