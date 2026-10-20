# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local tool that turns Claude Code session transcripts (`~/.claude/projects/*/*.jsonl`) into a ledger of
items (`decision`, `done`, `pending`, `abandoned`), finds duplicates across sessions, and serves a review
dashboard for merging them. Single user, single machine, SQLite.

## Commands

Tools (Python 3.13, uv) and dependencies are managed by mise; tasks run from `ledger/`.

```bash
mise trust && mise install && mise run install   # setup (.venv via uv sync)
mise run refresh    # full incremental pipeline: ingest -> extract -> embed -> judge (locked)
mise run serve      # dashboard on http://127.0.0.1:8765
mise run ingest | extract | embed | judge        # single steps
```

Useful flags (run inside `ledger/` with `mise exec -- python ...`):

- `extract.py --session <id-prefix>` re-extracts one session even if unchanged; `--limit`, `--model`, `--workers`.
- `judge.py --limit N --model sonnet --batch 25`.

There are no tests or linters yet. Verify changes by running the step against the real database and
inspecting it with `sqlite3 ledger/data/ledger.db`.

## Architecture

Pipeline scripts in `ledger/` share one SQLite database (`db.py`, schema plus additive `ALTER TABLE`
migrations in `connect()`, WAL mode, 60 s busy timeout).

1. **ingest.py**: condenses each transcript to user prompts, assistant prose and one line per tool call
   (tool output dropped), tagged `[mN timestamp]`. The `refs` JSON maps `mN` to `(uuid, timestamp)`.
   Stores a `content_hash` of the condensed text. Excludes automated-run dirs (`archive|nightly|snaps|scratchpad`)
   and sessions with fewer than 2 prompts.
2. **extract.py**: runs headless `claude -p` with `--json-schema` per session where
   `extracted_hash != content_hash` (so renames, which only change metadata, do not re-extract).
   The model cites message refs; **all item dates are looked up from `refs`, never written by the model**.
   Re-extraction deletes only the session's machine items that are unedited and unmerged.
3. **embed.py**: local fastembed (`BAAI/bge-small-en-v1.5`), normalised float32 BLOBs in `item_embedding`,
   brute-force dot product. `candidates()` returns cross-session pairs above `THRESHOLD` (0.85, calibrated
   on real data; see the comment), skipping `not_duplicate` pairs.
4. **judge.py**: `claude -p` labels candidate pairs `duplicate | resolves | related | unrelated`, cached in
   `pair_judgment` keyed by a hash of both texts. `resolves` is only valid when the closed item is `pending`.
5. **server.py** + **index.html**: stdlib HTTP server, one `/api/state` read plus POST actions
   (`merge`, `undo`, `not-duplicate`, `resolve`, `item`). Single static page, no build step.
6. **refresh.py**: runs steps 1-4 under an `fcntl` lock; concurrent callers drop a `refresh.pending`
   marker and the lock holder loops until it is gone.

### Invariants to preserve

- **Merges are non-destructive**: absorbed items get `merged_into = canonical`; `merge_event` stores a
  snapshot for undo. Readers treat `merged_into IS NULL` as active and gather sessions transitively.
- **Human edits win**: anything edited, merged or resolved has `edited = 1` and must survive re-extraction.
- **Headless `claude -p` calls must keep** `--no-session-persistence` (or they pollute the corpus) and
  `--setting-sources ""` (or they load user hooks and re-trigger the SessionEnd hook recursively). They run in
  a temp cwd with `--tools ""`; transcript text is untrusted data.
- `ledger/data/` holds condensed transcripts and may contain secrets: it is gitignored and must never be committed.

## Automatic refresh

The global `SessionEnd` hook in `~/.claude/settings.json` (not in this repo) calls `bin/on-session-end.sh`,
which detaches `mise -C <repo> run refresh` and returns immediately. Logs: `ledger/data/refresh.log`.
Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30): the ledger keeps their items,
but `claude --resume` no longer works for them.
