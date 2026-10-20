"""Headless `claude -p` call with structured output, shared by the pipeline steps.

Never persists the run as a session (it would pollute the corpus) and loads no settings
(user hooks would re-trigger the SessionEnd refresh). No tools: inputs are untrusted transcript text.
"""
import json
import subprocess
import tempfile


def structured(prompt, system, schema, model="sonnet", timeout=600):
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
    return out["structured_output"], out.get("total_cost_usd", 0)
