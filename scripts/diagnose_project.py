"""Read-only look at why a project is stuck: status, last error, milestones, tasks, recent events.

Usage:  python scripts/diagnose_project.py "horse tinder" [--db PATH]

Opens the database read-only (it never writes), so it is safe while SwarmForge is running.
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def default_db() -> Path:
    for p in (Path.cwd() / ".swarmforge" / "swarmforge.db", Path.home() / ".swarmforge" / "swarmforge.db"):
        if p.is_file():
            return p
    sys.exit("Could not find swarmforge.db; pass --db PATH")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="project name (or part of it) or id")
    ap.add_argument("--db", type=Path)
    a = ap.parse_args()
    db = a.db or default_db()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM projects WHERE id=? OR lower(name) LIKE ? ORDER BY created_at DESC",
                       (a.name, f"%{a.name.lower()}%")).fetchall()
    if not rows:
        sys.exit(f"No project matches {a.name!r} in {db}")
    for p in rows:
        print(f"\n=== {p['name']}  [{p['id']}]  status={p['status']}  milestone={p['current_milestone'] + 1}")
        print(f"repo: {p['repo']}   created {when(p['created_at'])}")
        if p["last_error"]:
            print(f"LAST ERROR: {p['last_error']}")
        print("\nMilestones:")
        for m in con.execute("SELECT idx,name,status,spent_usd,started_at,finished_at FROM milestones WHERE project_id=? ORDER BY idx", (p["id"],)):
            print(f"  {m['idx'] + 1}. {m['name']}  [{m['status']}]  ${m['spent_usd'] or 0:.2f}  {when(m['started_at'])} -> {when(m['finished_at'])}")
        print("\nTasks (latest 15):")
        for q in con.execute("SELECT q.id,q.title,q.status,q.run_id,r.state,r.state_reason FROM queue_items q LEFT JOIN runs r ON r.id=q.run_id "
                             "WHERE q.project_id=? ORDER BY q.id DESC LIMIT 15", (p["id"],)):
            reason = f" — {q['state_reason'][:160]}" if q["state_reason"] else ""
            print(f"  #{q['id']} [{q['status']}/{q['state'] or '-'}] {q['title'][:70]}  run={q['run_id'] or '-'}{reason}")
        print(f"\nSpec size: {len(p['spec'] or ''):,} characters")
        print("\nProject lead model calls (latest 10):")
        for c in con.execute("SELECT * FROM llm_calls WHERE run_id=? ORDER BY id DESC LIMIT 10", ("project:" + p["id"],)):
            c = dict(c)
            took = (c.get("ended_at") or 0) - (c.get("started_at") or 0)
            print(f"  {when(c.get('started_at'))}  {c.get('model')}  status={c.get('status')}  "
                  f"prompt_tokens={c.get('prompt_tokens')}  {took:.0f}s  {str(c.get('error') or '')[:160]}")
        print("\nProject lead model events (latest 10):")
        for e in con.execute("SELECT ts,level,substr(message,1,200) AS m FROM events WHERE run_id=? ORDER BY id DESC LIMIT 10", ("project:" + p["id"],)):
            print(f"  {when(e['ts'])}  [{e['level']}] {e['m']}")
        print("\nEvents (latest 12):")
        for e in con.execute("SELECT ts,kind,substr(detail,1,200) AS d FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT 12", (p["id"],)):
            print(f"  {when(e['ts'])}  {e['kind']}: {(e['d'] or '').splitlines()[0] if e['d'] else ''}")


if __name__ == "__main__":
    main()
