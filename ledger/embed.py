"""Embed active ledger items with a local model, and find likely duplicate pairs.

Embeddings give recall (paraphrases with few shared words); judge.py decides on precision.
Vectors are L2-normalised float32 BLOBs, compared by brute-force dot product: a few
thousand items take milliseconds, so no vector index is needed.

Usage: python embed.py
"""
import hashlib
import os
import sys

import numpy as np

from db import connect

MODEL = "BAAI/bge-small-en-v1.5"
CACHE = os.path.expanduser("~/.cache/fastembed")
THRESHOLD = 0.85   # cosine; calibrated 2026-10-20: >=0.90 mostly duplicates, 0.85-0.90 mixed, <0.85 mostly unrelated
TOP_K = 5


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def embed_pending(conn):
    rows = [r for r in conn.execute(
        """SELECT i.id, i.text, e.text_hash, e.model FROM item i
           LEFT JOIN item_embedding e ON e.item_id = i.id
           WHERE i.merged_into IS NULL""")
        if r["text_hash"] != text_hash(r["text"]) or r["model"] != MODEL]
    if not rows:
        return 0
    from fastembed import TextEmbedding  # slow import; only when there is work
    model = TextEmbedding(MODEL, cache_dir=CACHE)
    vecs = np.array(list(model.embed([r["text"] for r in rows])), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    conn.executemany(
        "INSERT OR REPLACE INTO item_embedding (item_id, model, text_hash, vector) VALUES (?,?,?,?)",
        [(r["id"], MODEL, text_hash(r["text"]), v.tobytes()) for r, v in zip(rows, vecs)])
    conn.commit()
    return len(rows)


def candidates(conn, threshold=THRESHOLD, top_k=TOP_K):
    """Pairs of active items from different sessions whose embeddings are close.

    Same-session pairs are skipped: extraction already merges restatements within a session,
    so close same-session items are mostly related steps (start/stop), not duplicates.
    """
    rows = list(conn.execute(
        """SELECT i.id, e.vector FROM item i JOIN item_embedding e ON e.item_id = i.id
           WHERE i.merged_into IS NULL AND e.model = ?""", (MODEL,)))
    if len(rows) < 2:
        return []
    ids = np.array([r["id"] for r in rows])
    mat = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    sims = mat @ mat.T
    np.fill_diagonal(sims, -1)

    sessions = {}
    for r in conn.execute("SELECT item_id, session_id FROM provenance"):
        sessions.setdefault(r["item_id"], set()).add(r["session_id"])
    children = {}
    for r in conn.execute("SELECT id, merged_into FROM item WHERE merged_into IS NOT NULL"):
        children.setdefault(r["merged_into"], []).append(r["id"])

    def sess(iid):
        out, stack = set(), [iid]
        while stack:
            i = stack.pop()
            out |= sessions.get(i, set())
            stack += children.get(i, [])
        return out

    rejected = {(r["a"], r["b"]) for r in conn.execute("SELECT a, b FROM not_duplicate")}
    pairs = {}
    for row in range(len(ids)):
        for col in np.argsort(-sims[row])[:top_k]:
            s = float(sims[row, col])
            if s < threshold:
                break
            a, b = sorted((int(ids[row]), int(ids[col])))
            if (a, b) in rejected or (a, b) in pairs:
                continue
            if sess(a) == sess(b) and len(sess(a)) == 1:
                continue
            pairs[(a, b)] = s
    return [{"a": a, "b": b, "score": round(s, 3)} for (a, b), s in sorted(pairs.items(), key=lambda kv: -kv[1])]


if __name__ == "__main__":
    conn = connect()
    n = embed_pending(conn)
    c = candidates(conn)
    print(f"embedded {n} items; {len(c)} candidate pairs at cosine >= {THRESHOLD}", file=sys.stderr)
