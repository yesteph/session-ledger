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
from urllib.parse import parse_qs, urlparse

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

    resolve = subject_resolver(conn)
    for r in rows:
        fam = family(r["id"])
        r["merged_count"] = len(fam) - 1
        r["sessions"] = sorted({s for i in fam for s in sessions[i]})
        r["subject_id"] = resolve(r["subject_id"])
    return rows


def subject_resolver(conn):
    """Map a subject id to the active subject it was (possibly transitively) merged into."""
    parent = {r["id"]: r["merged_into"] for r in conn.execute("SELECT id, merged_into FROM subject")}

    def resolve(sid):
        seen = set()
        while sid is not None and parent.get(sid) is not None and sid not in seen:
            seen.add(sid)
            sid = parent[sid]
        return sid if sid in parent else None
    return resolve


def subjects(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM subject ORDER BY id")]


def sections(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM section ORDER BY position, id")]


def brief(conn, sid):
    """Markdown context pack to continue a subject in a fresh session."""
    subj = conn.execute("SELECT * FROM subject WHERE id=?", (sid,)).fetchone()
    if not subj:
        raise ValueError("subject not found")
    its = [i for i in items(conn) if i["subject_id"] == subj["id"]]
    by_sid = {s["id"]: s for s in sessions(conn)}
    sess = sorted({s for i in its for s in i["sessions"] if s in by_sid}, key=lambda s: by_sid[s]["started_at"] or "")
    day = lambda ts: (ts or "?")[:10]
    out = [f"# {subj['name']}", "", subj["description"] or "", "",
           f"Context brief from the session ledger, {now()[:10]}: what was decided, done and left open "
           "across the sessions below. Continue from the pending items.", "", f"## Sessions ({len(sess)})"]
    for s in sess:
        x = by_sid[s]
        out.append(f"- {day(x['started_at'])} → {day(x['ended_at'])} · {x['title'] or x['first_prompt'][:60]} "
                   f"· {x['project']} · `cd {json.dumps(x['cwd'] or '~')} && claude --resume {x['id']}`")
    sections = (("Decisions", "decision", "resolved_at", True), ("Done", "done", "resolved_at", True),
                ("Pending (oldest first)", "pending", "raised_at", False), ("Abandoned", "abandoned", "resolved_at", True))
    for title, t, key, desc in sections:
        group = sorted((i for i in its if i["type"] == t), key=lambda i: i[key] or "", reverse=desc)
        if not group:
            continue
        out += ["", f"## {title} ({len(group)})"]
        for i in group:
            when = (f"raised {day(i['raised_at'])}, last seen {day(i['last_seen_at'])}" if t == "pending"
                    else day(i["resolved_at"]))
            out.append(f"- [{when}] {i['text']}")
    return "\n".join(out) + "\n"


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


def edit_subject(conn, sid, name=None, description=None):
    row = conn.execute("SELECT * FROM subject WHERE id=?", (sid,)).fetchone()
    if not row:
        raise ValueError("subject not found")
    conn.execute("UPDATE subject SET name=?, description=?, edited=1 WHERE id=?",
                 (name or row["name"], row["description"] if description is None else description, sid))
    conn.commit()


def save_section(conn, sid=None, name=None):
    """Create a section (no id) or rename one; returns its id."""
    if not (name or "").strip():
        raise ValueError("section name required")
    if sid is None:
        pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM section").fetchone()[0]
        sid = conn.execute("INSERT INTO section (name, position) VALUES (?, ?)", (name.strip(), pos)).lastrowid
    else:
        conn.execute("UPDATE section SET name=? WHERE id=?", (name.strip(), sid))
    conn.commit()
    return sid


def delete_section(conn, sid):
    """Remove a section; its subjects become unsectioned and stay there (locked) until moved."""
    conn.execute("UPDATE subject SET section_id=NULL, section_locked=1 WHERE section_id=?", (sid,))
    conn.execute("DELETE FROM section WHERE id=?", (sid,))
    conn.commit()


def move_subject(conn, subject_id, section_id):
    if section_id is not None and not conn.execute("SELECT 1 FROM section WHERE id=?", (section_id,)).fetchone():
        raise ValueError("section not found")
    conn.execute("UPDATE subject SET section_id=?, section_locked=1 WHERE id=?", (section_id, subject_id))
    conn.commit()


def merge_subject(conn, src, into):
    resolve = subject_resolver(conn)
    if resolve(int(into)) == int(src) or int(src) == int(into):
        raise ValueError("cannot merge a subject into itself")
    conn.execute("UPDATE subject SET merged_into=? WHERE id=? AND merged_into IS NULL", (resolve(int(into)), src))
    conn.commit()


def unmerge_subject(conn, sid):
    conn.execute("UPDATE subject SET merged_into=NULL WHERE id=?", (sid,))
    conn.commit()


def move_item(conn, iid, sid):
    if sid is not None and not conn.execute("SELECT 1 FROM subject WHERE id=?", (sid,)).fetchone():
        raise ValueError("subject not found")
    conn.execute("UPDATE item SET subject_id=?, subject_locked=1 WHERE id=?", (sid, iid))
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
                return self._send(200, {"items": items(conn), "sessions": sessions(conn), "subjects": subjects(conn), "sections": sections(conn),
                                        "candidates": candidates(conn), "merges": merges(conn)})
            if path == "/api/brief":
                sid = int(parse_qs(urlparse(self.path).query).get("subject", ["0"])[0])
                return self._send(200, brief(conn, sid).encode(), "text/markdown; charset=utf-8")
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
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
            elif self.path == "/api/subject":
                edit_subject(conn, body["id"], body.get("name"), body.get("description"))
            elif self.path == "/api/section":
                return self._send(200, {"ok": True, "id": save_section(conn, body.get("id"), body.get("name"))})
            elif self.path == "/api/section-delete":
                delete_section(conn, body["id"])
            elif self.path == "/api/subject-section":
                move_subject(conn, body["id"], body.get("section"))
            elif self.path == "/api/subject-merge":
                merge_subject(conn, body["from"], body["into"])
            elif self.path == "/api/subject-unmerge":
                unmerge_subject(conn, body["id"])
            elif self.path == "/api/item-subject":
                move_item(conn, body["id"], body.get("subject"))
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
