"""Classify embedding candidate pairs with Claude: duplicate, resolves, related or unrelated.

'resolves' is cross-session reconciliation: one item (done/decision/abandoned) closes the
other (pending). Verdicts are cached per pair and invalidated when either text changes.

Usage: python judge.py [--model sonnet] [--batch 25] [--workers 4] [--limit N]
"""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import embed
from db import connect

SYSTEM = """You review pairs of items from a work ledger built from Claude Code sessions.
Each item has a type (decision, done, pending, abandoned), a date and a text.
Label each pair:
- duplicate: both describe the same decision, the same piece of work, or the same open thread. Different wording is fine; different tickets, PRs, runs, dates of a recurring job, or components are NOT duplicates.
- resolves: one item closes the other: a pending item followed by a done/decision/abandoned item that settles exactly that thread. Give the id of the closing item as resolver.
- related: same subject, but distinct outcomes (e.g. consecutive steps, sibling tickets, the same nightly job on different dates).
- unrelated: no meaningful link.
Be strict: when unsure between duplicate and related, choose related. Reason: one short sentence."""

SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "pair": {"type": "integer"},
            "label": {"type": "string", "enum": ["duplicate", "resolves", "related", "unrelated"]},
            "resolver": {"type": ["integer", "null"]},
            "reason": {"type": "string"},
        },
        "required": ["pair", "label", "reason"]}}},
    "required": ["verdicts"],
}


def pair_hash(ta, tb):
    return hashlib.sha256((ta + "\x00" + tb).encode()).hexdigest()


def run_batch(batch, items, model):
    lines = []
    for n, p in enumerate(batch):
        lines.append(f"PAIR {n}")
        for iid in (p["a"], p["b"]):
            it = items[iid]
            lines.append(f"  #{iid} [{it['type']}, {(it['raised_at'] or '')[:10]}, {it['project']}] {it['text']}")
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--no-session-persistence", "--tools", "",
             "--setting-sources", "", "--system-prompt", SYSTEM,
             "--output-format", "json", "--json-schema", json.dumps(SCHEMA)],
            input="\n".join(lines), capture_output=True, text=True, cwd=cwd, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:] or proc.stdout.strip()[-500:])
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"no structured output: {str(out.get('result'))[:300]}")
    return out["structured_output"]["verdicts"], out.get("total_cost_usd", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args()

    conn = connect()
    items = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM item WHERE merged_into IS NULL")}
    judged = {(r["a"], r["b"]): r["pair_hash"] for r in conn.execute("SELECT a, b, pair_hash FROM pair_judgment")}
    todo = [p for p in embed.candidates(conn)
            if judged.get((p["a"], p["b"])) != pair_hash(items[p["a"]]["text"], items[p["b"]]["text"])][:args.limit]
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"judging {len(todo)} pairs in {len(batches)} batches with {args.model}", file=sys.stderr)

    cost, counts = 0.0, {}
    with ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(run_batch, b, items, args.model): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                verdicts, c = fut.result()
            except Exception as e:  # a failed batch is retried on the next run
                print(f"  FAIL batch: {e}", file=sys.stderr)
                continue
            cost += c
            for v in verdicts:
                if not 0 <= v["pair"] < len(batch):
                    continue
                p = batch[v["pair"]]
                resolver = v.get("resolver") if v["label"] == "resolves" and v.get("resolver") in (p["a"], p["b"]) else None
                closed = p["b"] if resolver == p["a"] else p["a"]
                if resolver and items[closed]["type"] != "pending":
                    resolver = None  # only a pending item can be resolved; decision -> done is just related
                label = v["label"] if v["label"] != "resolves" or resolver else "related"
                counts[label] = counts.get(label, 0) + 1
                conn.execute(
                    """INSERT OR REPLACE INTO pair_judgment (a, b, label, resolver, reason, pair_hash)
                       VALUES (?,?,?,?,?,?)""",
                    (p["a"], p["b"], label, resolver, v["reason"],
                     pair_hash(items[p["a"]]["text"], items[p["b"]]["text"])))
            conn.commit()
    print(f"labels {counts}; cost ${cost:.2f}", file=sys.stderr)


if __name__ == "__main__":
    main()
