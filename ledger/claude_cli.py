"""Headless `claude -p` call with structured output, shared by the pipeline steps.

Never persists the run as a session (it would pollute the corpus) and loads no settings
(user hooks would re-trigger the SessionEnd refresh). No tools: inputs are untrusted transcript text.
Every call, failed or not, is recorded in llm_call (tokens, cost, duration) for the Monitor tab.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

from db import connect


def record(step, model, ok, usage=None, cost=None, seconds=None, error=None):
    usage = usage or {}
    run_id = os.environ.get("LEDGER_RUN_ID")
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO llm_call (run_id, step, model, ok, input_tokens, output_tokens, cache_read_tokens,
                                     cache_write_tokens, cost_usd, seconds, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (int(run_id) if run_id else None, step, model, int(ok), usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"), cost, seconds,
             (error or "")[:500] or None))
        conn.commit()
    finally:
        conn.close()


def structured(prompt, system, schema, model="sonnet", timeout=600, step=None):
    step = step or os.path.splitext(os.path.basename(sys.argv[0]))[0] or "unknown"
    start = time.time()
    try:
        out, cost, usage, used_model = _call(prompt, system, schema, model, timeout)
    except Exception as e:
        record(step, model, False, seconds=time.time() - start, error=str(e))
        raise
    record(step, used_model, True, usage, cost, time.time() - start)
    return out, cost


def _call(prompt, system, schema, model, timeout):
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--no-session-persistence", "--tools", "",
             "--setting-sources", "", "--system-prompt", system,
             "--output-format", "json", "--json-schema", json.dumps(schema)],
            input=prompt, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:] or proc.stdout.strip()[-500:])
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"no structured output: {str(out.get('result'))[:300]}")
    # modelUsage also lists helper models the CLI used; the main model is the one that cost the most.
    usage_by_model = out.get("modelUsage") or {}
    used_model = max(usage_by_model, key=lambda m: usage_by_model[m].get("costUSD") or 0, default=model)
    return out["structured_output"], out.get("total_cost_usd", 0), out.get("usage"), used_model
