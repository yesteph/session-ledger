"""Local review server for the session ledger: JSON API over SQLite plus the dashboard page.

Merges are non-destructive: absorbed items point at the canonical one (merged_into),
each merge is logged with a snapshot of the canonical item, and undo restores it.

Usage: python3 server.py [--port 8765]
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import embed
import judge
from db import connect

HERE = Path(__file__).parent


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- reads ----------

def items(conn):
    rows = [dict(r) for r in conn.execute("SELECT * FROM item WHERE merged_into IS NULL")]
    # Sessions of an item include those of every item merged into it, transitively.
    children = defaultdict(list)
    for r in conn.execute("SELECT id, merged_into FROM item WHERE merged_into IS NOT NULL"):
        children[r["merged_into"]].append(r["id"])
    sessions = defaultdict(set)
    for r in conn.execute("SELECT item_id, session_id FROM provenance"):
        sessions[r["item_id"]].add(r["session_id"])

    def family(iid):
        out = [iid]
        for c in children.get(iid, []):
            out += family(c)
        return out

    for r in rows:
        fam = family(r["id"])
        r["merged_count"] = len(fam) - 1
        r["sessions"] = sorted({s for i in fam for s in sessions[i]})
    return rows


def sessions(conn):
    return [dict(r) for r in conn.execute(
        """SELECT id, project, cwd, branch, title, first_prompt, started_at, ended_at, user_msgs, summary, topics,
                  extract_error, extracted_mtime IS NOT NULL AS extracted
           FROM session ORDER BY ended_at DESC""")]


def candidates(conn):
    """Embedding candidates annotated with the judge's verdict; verdicts on changed texts are dropped."""
    text = {r["id"]: r["text"] for r in conn.execute("SELECT id, text FROM item WHERE merged_into IS NULL")}
    verdicts = {(r["a"], r["b"]): dict(r) for r in conn.execute("SELECT * FROM pair_judgment")}
    out = []
    for p in embed.candidates(conn):
        v = verdicts.get((p["a"], p["b"]))
        if v and v["pair_hash"] == judge.pair_hash(text[p["a"]], text[p["b"]]):
            p.update(label=v["label"], resolver=v["resolver"], reason=v["reason"])
        else:
            p.update(label=None, resolver=None, reason=None)
        out.append(p)
    return out


def merges(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM merge_event ORDER BY id DESC LIMIT 100")]


# ---------- writes ----------

def merge(conn, canonical, absorbed, text=None, type_=None):
    absorbed = [int(a) for a in absorbed if int(a) != int(canonical)]
    ids = [int(canonical)] + absorbed
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT * FROM item WHERE id IN ({','.join('?' * len(ids))}) AND merged_into IS NULL", ids)}
    if len(rows) != len(ids):
        raise ValueError("some items are missing or already merged")
    c = rows[int(canonical)]
    allr = list(rows.values())
    raised = min(r["raised_at"] for r in allr if r["raised_at"])
    seen = max(r["last_seen_at"] or r["raised_at"] for r in allr)
    new_type = type_ or c["type"]
    resolved = None if new_type == "pending" else (c["resolved_at"] or max(
        (r["resolved_at"] for r in allr if r["resolved_at"]), default=seen))
    conn.execute("INSERT INTO merge_event (canonical_id, absorbed_ids, before_state) VALUES (?,?,?)",
                 (c["id"], json.dumps(absorbed), json.dumps(c)))
    conn.execute("""UPDATE item SET text=?, type=?, raised_at=?, last_seen_at=?, resolved_at=?, edited=1 WHERE id=?""",
                 (text or c["text"], new_type, raised, seen, resolved, c["id"]))
    conn.executemany("UPDATE item SET merged_into=? WHERE id=?", [(c["id"], a) for a in absorbed])
    conn.commit()


def undo(conn, event_id):
    ev = conn.execute("SELECT * FROM merge_event WHERE id=? AND undone_at IS NULL", (event_id,)).fetchone()
    if not ev:
        raise ValueError("merge not found or already undone")
    before = json.loads(ev["before_state"])
    conn.execute("""UPDATE item SET text=?, type=?, raised_at=?, last_seen_at=?, resolved_at=?, edited=? WHERE id=?""",
                 (before["text"], before["type"], before["raised_at"], before["last_seen_at"],
                  before["resolved_at"], before["edited"], before["id"]))
    conn.executemany("UPDATE item SET merged_into=NULL WHERE id=? AND merged_into=?",
                     [(a, ev["canonical_id"]) for a in json.loads(ev["absorbed_ids"])])
    conn.execute("UPDATE merge_event SET undone_at=? WHERE id=?", (now(), event_id))
    conn.commit()


def not_duplicate(conn, a, b):
    a, b = sorted((int(a), int(b)))
    conn.execute("INSERT OR IGNORE INTO not_duplicate (a, b) VALUES (?,?)", (a, b))
    conn.commit()


def resolve(conn, pending_id, resolver_id):
    """Close a pending item with the outcome recorded in another session."""
    p = conn.execute("SELECT * FROM item WHERE id=? AND type='pending'", (pending_id,)).fetchone()
    r = conn.execute("SELECT * FROM item WHERE id=?", (resolver_id,)).fetchone()
    if not p or not r:
        raise ValueError("pending item or resolver not found")
    note = f"resolved by #{r['id']}: {r['text'][:200]}"
    conn.execute("""UPDATE item SET type='done', resolved_at=?, last_seen_at=max(coalesce(last_seen_at,''), ?),
                    evidence=trim(coalesce(evidence || ' | ', '') || ?), edited=1 WHERE id=?""",
                 (r["resolved_at"] or r["last_seen_at"] or now(), r["last_seen_at"] or "", note, pending_id))
    conn.commit()


def edit(conn, iid, text=None, type_=None):
    row = conn.execute("SELECT * FROM item WHERE id=?", (iid,)).fetchone()
    if not row:
        raise ValueError("item not found")
    t = type_ or row["type"]
    resolved = None if t == "pending" else (row["resolved_at"] or now())
    conn.execute("UPDATE item SET text=?, type=?, resolved_at=?, edited=1 WHERE id=?",
                 (text or row["text"], t, resolved, iid))
    conn.commit()


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        conn = connect()
        try:
            if path == "/api/state":
                return self._send(200, {"items": items(conn), "sessions": sessions(conn),
                                        "candidates": candidates(conn), "merges": merges(conn)})
            self._send(404, {"error": "not found"})
        finally:
            conn.close()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        conn = connect()
        try:
            if self.path == "/api/merge":
                merge(conn, body["canonical"], body["absorbed"], body.get("text"), body.get("type"))
            elif self.path == "/api/undo":
                undo(conn, body["event"])
            elif self.path == "/api/not-duplicate":
                not_duplicate(conn, body["a"], body["b"])
            elif self.path == "/api/resolve":
                resolve(conn, body["pending"], body["resolver"])
            elif self.path == "/api/item":
                edit(conn, body["id"], body.get("text"), body.get("type"))
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})
        finally:
            conn.close()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    print(f"ledger dashboard on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
