"""One locked pass: ingest -> extract -> embed -> judge. Triggered by the SessionEnd hook.

Several sessions can end at once. Only one refresh runs; others leave a "pending" marker
and exit, and the running pass loops until no marker is left, so no change is missed.
"""
import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
LOCK = os.path.join(DATA, "refresh.lock")
PENDING = os.path.join(DATA, "refresh.pending")
STEPS = [["ingest.py"], ["extract.py", "--limit", "50"], ["embed.py"], ["judge.py"]]


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


def main():
    os.makedirs(DATA, exist_ok=True)
    open(PENDING, "w").close()
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("refresh already running; queued")
        return
    while os.path.exists(PENDING):
        os.remove(PENDING)
        start = time.time()
        for step in STEPS:
            rc = subprocess.run([sys.executable, *step], cwd=HERE).returncode
            if rc != 0:
                log(f"{step[0]} failed with exit code {rc}")
        log(f"refresh pass done in {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
