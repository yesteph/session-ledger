"""One locked pass: ingest -> extract -> embed -> judge -> cluster.

Triggered by the SessionEnd hook, and hourly by a launchd agent as a fallback for sessions that
never fire SessionEnd (killed, crashed). Scheduled runs pass --min-idle so live sessions are not
re-extracted every hour.

Several sessions can end at once. Only one refresh runs; others leave a "pending" marker
and exit, and the running pass loops until no marker is left, so no change is missed.
Every invocation is recorded in the run table (queued ones too) for the Monitor tab; Claude
calls made by the steps are tied to it through LEDGER_RUN_ID.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from db import connect

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
LOCK = os.path.join(DATA, "refresh.lock")
PENDING = os.path.join(DATA, "refresh.pending")


def steps(min_idle):
    return [["ingest.py"], ["extract.py", "--limit", "50", "--min-idle", str(min_idle)],
            ["embed.py"], ["judge.py"], ["cluster.py"]]


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-idle", type=int, default=0, metavar="MINUTES",
                    help="leave sessions changed in the last MINUTES for a later pass (scheduled runs)")
    ap.add_argument("--trigger", choices=["manual", "hook", "schedule"], default="manual")
    ap.add_argument("--hook-input", default="", help="SessionEnd hook payload (JSON), recorded with the run")
    args = ap.parse_args()
    try:
        hook = json.loads(args.hook_input) if args.hook_input else {}
    except json.JSONDecodeError:
        hook = {}

    os.makedirs(DATA, exist_ok=True)
    conn = connect()
    run_id = conn.execute(
        "INSERT INTO run (trigger, session_id, reason, started_at, status) VALUES (?,?,?,?, 'queued')",
        (args.trigger, hook.get("session_id"), hook.get("reason"), now())).lastrowid
    conn.commit()

    open(PENDING, "w").close()
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        conn.execute("UPDATE run SET ended_at=? WHERE id=?", (now(), run_id))
        conn.commit()
        log(f"refresh already running; queued ({args.trigger})")
        return

    conn.execute("UPDATE run SET status='running' WHERE id=?", (run_id,))
    conn.commit()
    env = {**os.environ, "LEDGER_RUN_ID": str(run_id)}
    failed, passes = False, 0
    while os.path.exists(PENDING):
        os.remove(PENDING)
        passes += 1
        start, record = time.time(), []
        for step in steps(args.min_idle):
            t0 = time.time()
            rc = subprocess.run([sys.executable, *step], cwd=HERE, env=env).returncode
            record.append({"step": step[0].removesuffix(".py"), "exit": rc, "seconds": round(time.time() - t0, 1)})
            if rc != 0:
                failed = True
                log(f"{step[0]} failed with exit code {rc}")
        conn.execute("UPDATE run SET passes=?, steps=? WHERE id=?", (passes, json.dumps(record), run_id))
        conn.commit()
        log(f"refresh pass done in {time.time() - start:.0f}s ({args.trigger})")
    conn.execute("UPDATE run SET status=?, ended_at=? WHERE id=?", ("failed" if failed else "ok", now(), run_id))
    conn.commit()


if __name__ == "__main__":
    main()
