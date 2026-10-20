"""Group ledger items into subjects: named workstreams that span sessions.

Subjects come from a catalog with stable ids rather than from raw clustering, so they keep
their names and identity as new sessions arrive:
1. bootstrap: when no subject exists, one Claude call drafts the catalog from session summaries.
2. assign: unassigned items are matched to catalog subjects in parallel batches (or to none).
3. propose: items no subject fits get one call that creates new subjects for them.
4. sections: subjects are grouped into sections, drafted once, then new subjects are placed.

Human choices win: items with subject_locked are never moved, and subject merges and renames
are made in the dashboard. Items that fit nothing after two passes stay unclassified.

Usage: python cluster.py [--model sonnet] [--batch 120] [--workers 4]
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from claude_cli import structured
from db import connect

MAX_ATTEMPTS = 2
GRANULARITY = """A subject is a workstream someone would track across several working sessions: an initiative,
a feature, an incident, a recurring operation, an infrastructure component being built or fixed.
Good: "Checkout service migration to Postgres", "Nightly test suite reliability",
"Developer workstation provisioning". Too broad: "backend", "Infrastructure". Too narrow: one PR or one bug."""

BOOTSTRAP_SYSTEM = f"""You define a catalog of subjects from summaries of Claude Code working sessions.
{GRANULARITY}
Produce 15 to 60 subjects covering the sessions. A session may touch several subjects. Subjects may span
repositories. Name: at most 8 words. Description: one sentence on scope, naming key tickets or components."""

BOOTSTRAP_SCHEMA = {
    "type": "object",
    "properties": {"subjects": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
        "required": ["name", "description"]}}},
    "required": ["subjects"],
}

ASSIGN_SYSTEM = f"""You file work-ledger items under subjects from a fixed catalog.
{GRANULARITY}
For each item, give the id of the subject it belongs to, or null when no subject in the catalog fits.
Prefer null over a forced fit. Use only subject ids from the catalog."""

ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {"assignments": {"type": "array", "items": {
        "type": "object",
        "properties": {"item": {"type": "integer"}, "subject": {"type": ["integer", "null"]}},
        "required": ["item", "subject"]}}},
    "required": ["assignments"],
}

PROPOSE_SYSTEM = f"""Some work-ledger items fit no existing subject. Create new subjects for them.
{GRANULARITY}
Do not duplicate an existing subject. Group items into as few new subjects as is faithful; leave an item out
when it is a one-off with no workstream. Name: at most 8 words. Description: one sentence."""

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {"subjects": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                       "items": {"type": "array", "items": {"type": "integer"}}},
        "required": ["name", "description", "items"]}}},
    "required": ["subjects"],
}


SECTION_HINT = """A section groups subjects into a product area, initiative family or standing responsibility,
e.g. "Payments product", "Platform operations", "Build and release pipeline", "Developer environment",
"Benchmarking". Name: at most 5 words."""

SECTIONS_SYSTEM = f"""You organise a catalog of work subjects into sections.
{SECTION_HINT}
Produce 4 to 10 sections and put every subject in exactly one section."""

SECTIONS_SCHEMA = {
    "type": "object",
    "properties": {"sections": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "subjects": {"type": "array", "items": {"type": "integer"}}},
        "required": ["name", "subjects"]}}},
    "required": ["sections"],
}

PLACE_SYSTEM = f"""You file new work subjects under existing sections.
{SECTION_HINT}
For each subject give the id of its section, or null when no section fits. Use only section ids from the list."""

PLACE_SCHEMA = {
    "type": "object",
    "properties": {"placements": {"type": "array", "items": {
        "type": "object",
        "properties": {"subject": {"type": "integer"}, "section": {"type": ["integer", "null"]}},
        "required": ["subject", "section"]}}},
    "required": ["placements"],
}


def catalog(conn):
    return [dict(r) for r in conn.execute("SELECT id, name, description FROM subject WHERE merged_into IS NULL ORDER BY id")]


def catalog_text(subjects):
    return "\n".join(f"S{s['id']}: {s['name']} - {s['description'] or ''}" for s in subjects)


def item_line(it):
    return f"#{it['id']} [{it['project']}, {it['type']}] {it['text'][:300]}"


def bootstrap(conn, model):
    sessions = conn.execute("SELECT title, project, topics, summary FROM session WHERE summary IS NOT NULL ORDER BY started_at").fetchall()
    prompt = "\n\n".join(f"[{s['project']}] {s['title'] or '-'}\ntopics: {s['topics']}\n{s['summary']}" for s in sessions)
    out, cost = structured(prompt, BOOTSTRAP_SYSTEM, BOOTSTRAP_SCHEMA, model)
    conn.executemany("INSERT INTO subject (name, description) VALUES (?, ?)",
                     [(s["name"], s["description"]) for s in out["subjects"]])
    conn.commit()
    print(f"bootstrap: {len(out['subjects'])} subjects from {len(sessions)} sessions  ${cost:.2f}", file=sys.stderr)
    return cost


def unassigned(conn):
    return [dict(r) for r in conn.execute(
        """SELECT id, project, type, text FROM item
           WHERE merged_into IS NULL AND subject_locked = 0 AND subject_attempts < ?
             AND (subject_id IS NULL OR subject_id NOT IN (SELECT id FROM subject))
           ORDER BY raised_at""", (MAX_ATTEMPTS,))]


def assign(conn, model, batch, workers):
    subjects = catalog(conn)
    valid = {s["id"] for s in subjects}
    todo = unassigned(conn)
    if not todo or not subjects:
        return 0.0
    header = "CATALOG\n" + catalog_text(subjects) + "\n\nITEMS\n"
    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    cost, filed = 0.0, 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(structured, header + "\n".join(item_line(i) for i in b),
                               ASSIGN_SYSTEM, ASSIGN_SCHEMA, model): b for b in batches}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                out, c = fut.result()
            except Exception as e:  # items stay unassigned and are retried next run
                print(f"  FAIL assign batch: {e}", file=sys.stderr)
                continue
            cost += c
            ids = {i["id"] for i in b}
            got = {a["item"]: a["subject"] for a in out["assignments"] if a["item"] in ids}
            for iid in ids:
                sid = got.get(iid)
                if sid in valid:
                    conn.execute("UPDATE item SET subject_id = ? WHERE id = ?", (sid, iid))
                    filed += 1
                else:
                    conn.execute("UPDATE item SET subject_attempts = subject_attempts + 1 WHERE id = ?", (iid,))
            conn.commit()
    print(f"assign: {filed}/{len(todo)} items filed in {len(batches)} batches  ${cost:.2f}", file=sys.stderr)
    return cost


def propose(conn, model, limit=400):
    leftovers = [i for i in unassigned(conn)][:limit]
    if not leftovers:
        return 0.0
    ids = {i["id"] for i in leftovers}
    prompt = ("EXISTING SUBJECTS\n" + catalog_text(catalog(conn)) +
              "\n\nUNFILED ITEMS\n" + "\n".join(item_line(i) for i in leftovers))
    out, cost = structured(prompt, PROPOSE_SYSTEM, PROPOSE_SCHEMA, model)
    created = filed = 0
    for s in out["subjects"]:
        members = [i for i in s["items"] if i in ids]
        if not members:
            continue
        sid = conn.execute("INSERT INTO subject (name, description) VALUES (?, ?)", (s["name"], s["description"])).lastrowid
        conn.executemany("UPDATE item SET subject_id = ? WHERE id = ?", [(sid, i) for i in members])
        ids -= set(members)
        created += 1
        filed += len(members)
    conn.commit()
    print(f"propose: {created} new subjects, {filed}/{len(leftovers)} items filed  ${cost:.2f}", file=sys.stderr)
    return cost


def place_sections(conn, model):
    """Bootstrap sections when none exist; otherwise place new subjects into existing sections."""
    subjects = [dict(r) for r in conn.execute(
        """SELECT id, name, description FROM subject
           WHERE merged_into IS NULL AND section_locked = 0 AND section_attempts < ?
             AND (section_id IS NULL OR section_id NOT IN (SELECT id FROM section))""", (MAX_ATTEMPTS,))]
    if not subjects:
        return 0.0
    sections = [dict(r) for r in conn.execute("SELECT id, name FROM section ORDER BY position, id")]
    ids = {s["id"] for s in subjects}
    if not sections:
        out, cost = structured(catalog_text(subjects), SECTIONS_SYSTEM, SECTIONS_SCHEMA, model)
        for pos, sec in enumerate(out["sections"]):
            members = [i for i in sec["subjects"] if i in ids]
            if members:
                sid = conn.execute("INSERT INTO section (name, position) VALUES (?, ?)", (sec["name"], pos)).lastrowid
                conn.executemany("UPDATE subject SET section_id = ? WHERE id = ?", [(sid, i) for i in members])
        conn.commit()
        print(f"sections: bootstrapped {len(out['sections'])} sections  ${cost:.2f}", file=sys.stderr)
        return cost
    prompt = ("SECTIONS\n" + "\n".join(f"{s['id']}: {s['name']}" for s in sections) +
              "\n\nNEW SUBJECTS\n" + catalog_text(subjects))
    out, cost = structured(prompt, PLACE_SYSTEM, PLACE_SCHEMA, model)
    valid = {s["id"] for s in sections}
    got = {p["subject"]: p["section"] for p in out["placements"]}
    placed = 0
    for sid in ids:
        if got.get(sid) in valid:
            conn.execute("UPDATE subject SET section_id = ? WHERE id = ?", (got[sid], sid))
            placed += 1
        else:
            conn.execute("UPDATE subject SET section_attempts = section_attempts + 1 WHERE id = ?", (sid,))
    conn.commit()
    print(f"sections: placed {placed}/{len(ids)} new subjects  ${cost:.2f}", file=sys.stderr)
    return cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch", type=int, default=120)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    conn = connect()
    cost = 0.0
    if not catalog(conn):
        cost += bootstrap(conn, args.model)
    cost += assign(conn, args.model, args.batch, args.workers)
    cost += propose(conn, args.model)
    cost += place_sections(conn, args.model)
    left = conn.execute("SELECT count(*) FROM item WHERE merged_into IS NULL AND subject_id IS NULL").fetchone()[0]
    print(f"cluster: {len(catalog(conn))} subjects, {left} items unclassified; total ${cost:.2f}", file=sys.stderr)


if __name__ == "__main__":
    main()
