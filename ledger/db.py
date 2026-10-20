"""SQLite store for the session ledger: sessions, extracted items, provenance and merge history."""
import os
import sqlite3

DB_PATH = os.environ.get("LEDGER_DB", os.path.join(os.path.dirname(__file__), "data", "ledger.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
  id            TEXT PRIMARY KEY,
  path          TEXT NOT NULL,
  project       TEXT,
  cwd           TEXT,
  branch        TEXT,
  title         TEXT,
  first_prompt  TEXT,
  started_at    TEXT,
  ended_at      TEXT,
  user_msgs     INTEGER,
  file_mtime    REAL,
  condensed     TEXT,
  refs          TEXT,              -- JSON: {"m12": ["uuid", "timestamp"], ...}
  extracted_mtime REAL,            -- file_mtime at last successful extraction
  content_hash  TEXT,              -- hash of condensed text; renames and other metadata leave it unchanged
  extracted_hash TEXT,             -- content_hash at last successful extraction
  summary       TEXT,
  topics        TEXT,              -- JSON array
  extract_error TEXT
);

CREATE TABLE IF NOT EXISTS section (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  position      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS subject (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  description   TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  merged_into   INTEGER REFERENCES subject(id),  -- non-destructive subject merge; readers follow the chain
  edited        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item (
  id            INTEGER PRIMARY KEY,
  type          TEXT NOT NULL CHECK (type IN ('decision','done','pending','abandoned')),
  text          TEXT NOT NULL,
  evidence      TEXT,
  project       TEXT,
  raised_at     TEXT,
  last_seen_at  TEXT,
  resolved_at   TEXT,
  merged_into   INTEGER REFERENCES item(id),
  edited        INTEGER NOT NULL DEFAULT 0  -- 1 once a human changed it; re-extraction keeps it
);
CREATE INDEX IF NOT EXISTS item_merged ON item(merged_into);

CREATE TABLE IF NOT EXISTS provenance (
  item_id       INTEGER NOT NULL REFERENCES item(id),
  session_id    TEXT NOT NULL REFERENCES session(id),
  message_uuid  TEXT,
  ts            TEXT,
  role          TEXT                -- raised | seen | resolved
);
CREATE INDEX IF NOT EXISTS prov_item ON provenance(item_id);
CREATE INDEX IF NOT EXISTS prov_session ON provenance(session_id);

CREATE TABLE IF NOT EXISTS merge_event (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  canonical_id  INTEGER NOT NULL,
  absorbed_ids  TEXT NOT NULL,      -- JSON array
  before_state  TEXT NOT NULL,      -- JSON snapshot of the canonical item, for undo
  undone_at     TEXT
);

CREATE TABLE IF NOT EXISTS item_embedding (
  item_id       INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
  model         TEXT NOT NULL,
  text_hash     TEXT NOT NULL,      -- re-embed when the item text changes (edit, merge)
  vector        BLOB NOT NULL       -- float32, L2-normalised
);

CREATE TABLE IF NOT EXISTS pair_judgment (
  a INTEGER NOT NULL, b INTEGER NOT NULL,   -- a < b
  label         TEXT NOT NULL CHECK (label IN ('duplicate','resolves','related','unrelated')),
  resolver      INTEGER,                    -- for 'resolves': the item that closes the other
  reason        TEXT,
  pair_hash     TEXT NOT NULL,              -- texts judged; a changed text invalidates the verdict
  at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS run (
  id            INTEGER PRIMARY KEY,
  trigger       TEXT NOT NULL,      -- hook | schedule | manual
  session_id    TEXT,               -- hook runs: the session that ended
  reason        TEXT,               -- hook runs: SessionEnd reason
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  status        TEXT NOT NULL,      -- queued (folded into a running pass) | running | ok | failed
  passes        INTEGER NOT NULL DEFAULT 0,
  steps         TEXT                -- JSON: [{"step", "exit", "seconds"}] of the last pass
);

CREATE TABLE IF NOT EXISTS llm_call (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  run_id        INTEGER,            -- set when called from a refresh run
  step          TEXT NOT NULL,      -- extract | judge | cluster | ...
  model         TEXT,
  ok            INTEGER NOT NULL,
  input_tokens  INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost_usd      REAL,
  seconds       REAL,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS llm_call_at ON llm_call(at);

CREATE TABLE IF NOT EXISTS not_duplicate (
  a INTEGER NOT NULL, b INTEGER NOT NULL, PRIMARY KEY (a, b)
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(session)")}
    for col in ("content_hash", "extracted_hash"):
        if col not in cols:
            conn.execute(f"ALTER TABLE session ADD COLUMN {col} TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subject)")}
    for col, decl in (("section_id", "INTEGER REFERENCES section(id)"),
                      ("section_locked", "INTEGER NOT NULL DEFAULT 0"),     # set by a human; placement never moves it
                      ("section_attempts", "INTEGER NOT NULL DEFAULT 0")):  # placement passes that found no section
        if col not in cols:
            conn.execute(f"ALTER TABLE subject ADD COLUMN {col} {decl}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(item)")}
    for col, decl in (("subject_id", "INTEGER REFERENCES subject(id)"),
                      ("subject_locked", "INTEGER NOT NULL DEFAULT 0"),    # set by a human; clustering never moves it
                      ("subject_attempts", "INTEGER NOT NULL DEFAULT 0")):  # assignment passes that found no subject
        if col not in cols:
            conn.execute(f"ALTER TABLE item ADD COLUMN {col} {decl}")
    return conn
