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
   the cited message refs, never from the model. The first extraction reads the whole session;
   later ones read only the messages after the session's checkpoint, with its existing items as
   context, so items keep their ids and a thread closed in a resumed session becomes a status
   change on the same item (shown as ↻ in the dashboard). Items you edited are never re-created.
   Extraction runs are not persisted as sessions and load no user settings, so they cannot trigger
   the SessionEnd hook.
3. `embed.py` embeds active items locally (fastembed, `BAAI/bge-small-en-v1.5`) and proposes
   cross-session pairs above cosine 0.85.
4. `judge.py` asks Claude to label each pair: `duplicate`, `resolves` (closes a pending item),
   `related` or `unrelated`. Verdicts are cached and invalidated when a text changes.
5. `cluster.py` files items under subjects (named workstreams spanning sessions). The first run drafts
   a catalog from session summaries; later runs assign only new items and create subjects for items
   no existing subject fits. Subjects are grouped into sections (product areas), drafted once and
   then extended as new subjects appear.
6. `server.py` serves the review dashboard and a JSON API over SQLite (`ledger/data/ledger.db`).
   The Subjects tab groups subjects by section, shows pending/decided/done per subject, and copies a
   markdown brief to continue a subject in a new session. Sections can be created, renamed and deleted,
   and subjects moved between them.

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
mise run refresh   # ingest -> extract -> embed -> judge -> cluster, locked, incremental
mise run serve     # http://127.0.0.1:8765
```

Individual steps: `mise run ingest|extract|embed|judge|cluster`.

## Automatic refresh

- **On session exit:** a global Claude Code `SessionEnd` hook in `~/.claude/settings.json` runs
  `bin/on-session-end.sh`, which starts the refresh detached and returns immediately.
- **Hourly fallback:** a launchd agent catches sessions that never fire `SessionEnd` (killed,
  crashed). It skips sessions changed in the last 30 minutes, so live sessions are not re-extracted
  every hour.

```bash
mise run schedule:install     # or schedule:uninstall, schedule:status
```

Concurrent triggers queue behind a lock instead of running twice. Output goes to
`ledger/data/refresh.log`.

## Monitoring

The dashboard's Monitor tab shows hook and schedule health, the last refresh, corpus backlog,
Claude spend (24 h / 7 d / 30 d, daily chart, by step and model), the history of refresh runs with
per-step timings, and the tail of the refresh log. Spend is recorded per Claude call from the
moment this was added; earlier calls only appear in the logs.

## Follow-ups

Known gaps, roughly by priority.

### Correctness

- **Delta extraction is unproven on a resumed session.** It was validated on a growing session
  (stable ids, one pending -> done transition), not yet on the case that motivated it: a thread
  closed after `claude --resume`. Check the next resumed session shows `↻ pending -> done` on the
  original item. Delta sees item texts, not the earlier conversation, so vague items can be linked
  wrongly; `extract.py --session <id>` forces a full re-read.
- **Duplicates within one session are not proposed.** `embed.candidates` skips same-session pairs to
  avoid "start/stop" noise, so a re-created item next to an edited one is never shown for review.
- **`resolves` verdicts are not applied automatically.** Closing a pending item resolved in another
  session needs a click ("Close as done"), and there is no bulk action.
- **Queued triggers lose their options.** A scheduled run that arrives while a hook refresh holds the
  lock is folded into that pass without `--min-idle`, so a live session can be extracted once.
- **The brief's resume command can point at the wrong folder.** It uses the session's last cwd; a
  session that moved folders is stored under its original project dir, where `claude --resume`
  finds it.

### Quality

- **Subject catalog needs a guided re-bootstrap.** The first draft had narrow subjects (single PR
  reviews) and overlaps, fixed by hand with merges. A re-bootstrap should learn from those merges.
- **No tests or linters.** Changes are verified against the real database. Fixtures for condense,
  delta extraction and merge/undo would catch regressions.
- **Thresholds are calibrated once.** The 0.85 cosine threshold was set from samples on 2026-10-20;
  re-tune it from merges (positives) and "not a duplicate" pairs (negatives) once there are enough.
- **Model choice is not evaluated.** Every step uses Sonnet; judge, cluster and section placement may
  work with Haiku at a lower cost.
- **English-only embeddings.** Correct because extraction writes items in English; a multilingual
  model would be needed if that changes.

### Features

- **Continue a subject in one click.** The brief is copied to the clipboard; starting a new Claude
  session seeded with it (and grouping the subject's sessions in the desktop sidebar) is not built.
- **Subagent transcripts are ignored.** Sidechain sessions under `subagents/` are skipped, so work
  done only inside subagents is invisible.
- **No export or tracker sync.** Pending items stay in the dashboard; pushing them to Linear (the
  other option considered) is not built.
- **No alerts.** The Monitor tab shows failures, but nothing notifies you when a refresh fails or the
  hook stops firing.

### Operations

- **The dashboard server is not persistent.** `mise run serve` (or the Claude app's preview) stops with
  its terminal or app session, and `http://localhost:8765` then fails as a whole. A launchd agent with
  `KeepAlive` would keep it up.
- **Transcripts expire.** Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30).
  The ledger keeps their items and condensed text, but `claude --resume` stops working; raise the
  setting if resuming old sessions matters.
- **Spend history starts on 2026-10-20.** About $35 of earlier extraction and judging only appears in
  `ledger/data/*.log`.
- **macOS only for the schedule.** The fallback uses launchd; Linux would need a systemd timer or cron.
- **Local and unauthenticated.** The dashboard binds 127.0.0.1 with no login, and `ledger/data/` holds
  condensed transcripts unencrypted. Keep it on a single-user machine.
- **Hard-coded exclusions.** `ingest.EXCLUDE_DIR` matches this machine's automated-run folders
  (archive, nightly, snaps, scratchpad); make it configurable before using it elsewhere.

