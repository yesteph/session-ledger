"""Scan Claude Code transcripts and store a condensed, message-referenced version of each session.

The condensed text keeps what a reader needs to know what was asked, decided and done:
user prompts, assistant prose and one line per tool call. Tool output is dropped.
Each message gets a short ref (m1, m2, ...) mapped to its uuid and timestamp, so extracted
items can be dated from the transcript itself rather than by the model.
"""
import glob
import hashlib
import json
import os
import re
import sys

from db import connect

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
# Automated runs (benchmarks, archives, headless scratchpads) are not conversations worth tracking.
EXCLUDE_DIR = re.compile(r"archive|nightly|snaps|scratchpad", re.I)
MIN_USER_MSGS = 2
MSG_CHARS = 1500
TOOL_CHARS = 160
SESSION_CHARS = 120_000

TAG_BLOCK = re.compile(r"<(system-reminder|local-command-stdout|command-message|command-args)>.*?</\1>", re.S)


def project_of(cwd):
    if not cwd:
        return None
    parts = cwd.split("/")
    if "GIT" in parts and parts.index("GIT") + 1 < len(parts):
        return parts[parts.index("GIT") + 1]
    return parts[-1] or None


def clean(text):
    text = TAG_BLOCK.sub("", text).strip()
    return text[:MSG_CHARS] + (" …[truncated]" if len(text) > MSG_CHARS else "")


def tool_line(block):
    inp = block.get("input") or {}
    detail = (inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url")
              or inp.get("description") or inp.get("prompt") or "")
    detail = " ".join(str(detail).split())
    return f"{block.get('name')}: {detail[:TOOL_CHARS]}"


def condense(path):
    meta = {"title": None, "ai_title": None, "cwd": None, "branch": None, "started_at": None,
            "ended_at": None, "first_prompt": None, "user_msgs": 0}
    lines, refs = [], {}
    for raw in open(path, encoding="utf-8"):
        try:
            o = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "custom-title":
            meta["title"] = o.get("customTitle")
        elif t == "ai-title":
            meta["ai_title"] = o.get("aiTitle") or o.get("title")
        if t not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
            continue
        ts = o.get("timestamp")
        meta["started_at"] = meta["started_at"] or ts
        meta["ended_at"] = ts or meta["ended_at"]
        meta["cwd"] = o.get("cwd") or meta["cwd"]
        meta["branch"] = o.get("gitBranch") or meta["branch"]
        content = o.get("message", {}).get("content")
        parts = []
        if isinstance(content, str):
            parts.append(clean(content))
        elif isinstance(content, list):
            for b in content:
                if b.get("type") == "text":
                    parts.append(clean(b.get("text", "")))
                elif b.get("type") == "tool_use":
                    parts.append("→ " + tool_line(b))
        parts = [p for p in parts if p]
        if not parts:
            continue  # tool results, thinking-only turns
        if t == "user":
            meta["user_msgs"] += 1
            meta["first_prompt"] = meta["first_prompt"] or parts[0][:300]
        ref = f"m{len(refs) + 1}"
        refs[ref] = [o.get("uuid"), ts]
        lines.append(f"[{ref} {(ts or '')[:16]}] {t.upper()}: " + "\n".join(parts))
    text = "\n".join(lines)
    if len(text) > SESSION_CHARS:
        # Keep the opening (intent) and the end (latest state); the middle is the most expendable.
        head, tail = SESSION_CHARS // 4, SESSION_CHARS * 3 // 4
        text = text[:head] + "\n[… middle of session elided …]\n" + text[-tail:]
    meta["title"] = meta["title"] or meta["ai_title"]
    return meta, text, refs


def run():
    conn = connect()
    seen = updated = 0
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        if EXCLUDE_DIR.search(os.path.basename(os.path.dirname(path))):
            continue
        seen += 1
        sid = os.path.splitext(os.path.basename(path))[0]
        mtime = os.path.getmtime(path)
        row = conn.execute("SELECT file_mtime FROM session WHERE id=?", (sid,)).fetchone()
        if row and row["file_mtime"] == mtime:
            continue
        meta, text, refs = condense(path)
        if meta["user_msgs"] < MIN_USER_MSGS:
            continue
        conn.execute(
            """INSERT INTO session (id, path, project, cwd, branch, title, first_prompt, started_at, ended_at,
                                    user_msgs, file_mtime, condensed, refs, content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET path=excluded.path, project=excluded.project, cwd=excluded.cwd,
                 branch=excluded.branch, title=excluded.title, first_prompt=excluded.first_prompt,
                 started_at=excluded.started_at, ended_at=excluded.ended_at, user_msgs=excluded.user_msgs,
                 file_mtime=excluded.file_mtime, condensed=excluded.condensed, refs=excluded.refs,
                 content_hash=excluded.content_hash""",
            (sid, path, project_of(meta["cwd"]), meta["cwd"], meta["branch"], meta["title"], meta["first_prompt"],
             meta["started_at"], meta["ended_at"], meta["user_msgs"], mtime, text, json.dumps(refs),
             hashlib.sha256(text.encode()).hexdigest()))
        updated += 1
    conn.commit()
    total = conn.execute("SELECT count(*) FROM session").fetchone()[0]
    print(f"scanned {seen} transcripts, updated {updated}, sessions stored {total}", file=sys.stderr)


if __name__ == "__main__":
    run()
