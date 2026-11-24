"""Compare Work data assembly on disposable history; never import dashboard startup.

This is not a browser benchmark. The legacy view loads full queue rows and at most
300 runs, with different grouping/order rules. Timings are not a UI speed claim.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import statistics
import tempfile
import time
from pathlib import Path

from swarmforge.db import Store
from swarmforge.launcher import pid_alive
from swarmforge.webapp.queries import WorkReader


def legacy_work_items(store: Store):
    """Compile actual local assembly helpers, without running Streamlit startup.

    No config loading, controller launch, session setup or live Store is executed.
    Fail if the helper inventory changes instead of silently faking a baseline.
    """
    path = Path(__file__).resolve().parents[1] / "src/swarmforge/dashboard.py"
    source = path.read_text(encoding="utf-8")
    functions = {"work_items", "_run_status", "_short_repo", "_restarted_as", "_merge_states"}
    constants = {"TERMINAL", "STATE_ICON", "QSTATUS"}
    selected, found = [], set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in constants:
                ast.literal_eval(node.value)
                selected.append(node)
                found.add(target.id)
    if found != functions | constants:
        raise RuntimeError("Legacy Work helpers changed; review the benchmark adapter")
    namespace = {"store": store, "re": re, "pid_alive": pid_alive}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["work_items"], hashlib.sha256(source.encode()).hexdigest()


def seed(store: Store, count: int, mixed: bool):
    states = ["COMPLETED", "FAILED", "CANCELLED", "BUDGET_EXCEEDED", "PLANNING", "TESTING"]
    statuses = ["done", "failed", "cancelled", "queued", "running", "tentative", "skipped"]
    with store.conn:
        if mixed:
            store.conn.executemany(
                "INSERT INTO runs(id,created_at,updated_at,finished_at,state,issue_title,repo,config_json) VALUES (?,?,?,?,?,?,?,?)",
                [(f"fixture-{i}", float(i), float(i), float(i) if i % 6 < 4 else None,
                  states[i % 6], f"Direct work {i}", "fixture/repo", json.dumps({"fixture": "x" * 512}))
                 for i in range(count)],
            )
        store.conn.executemany(
            "INSERT INTO queue_items(position,title,body,repo,created_at,status,run_id) VALUES (?,?,?,?,?,?,?)",
            [(i, f"Fixture task {i}", "x" * 512, "fixture/repo", float(i),
              statuses[i % 7] if mixed else "done", f"fixture-{i}" if mixed and i % 2 == 0 else None)
             for i in range(count)],
        )


def measure(operation, samples):
    operation()  # Warm imports/caches consistently; startup is outside this comparison.
    timings = []
    result = None
    for _ in range(samples):
        start = time.perf_counter()
        result = operation()
        timings.append((time.perf_counter() - start) * 1000)
    return result, {"median_ms": round(statistics.median(timings), 2), "max_ms": round(max(timings), 2)}


def benchmark(count: int, samples: int, mixed: bool, explain: bool):
    with tempfile.TemporaryDirectory(prefix="sf-web-benchmark-") as directory:
        path = Path(directory) / "history.db"
        store = Store(path)
        try:
            seed(store, count, mixed)
            legacy, source_hash = legacy_work_items(store)
            old, old_timing = measure(legacy, samples)
            reader = WorkReader(path)
            page, new_timing = measure(lambda: reader.list(limit=25), samples)
            result = {
                "fixture": "mixed" if mixed else "queue-only", "queue_rows": count,
                "run_rows": count if mixed else 0, "samples": samples,
                "legacy": {**old_timing, "assembled_rows": len(old), "dashboard_sha256": source_hash},
                "web": {**new_timing, "total": page["total"], "returned_rows": len(page["items"])},
                "scope": "Warm data assembly only, not browser/Streamlit rendering or end-to-end latency.",
                "semantic_difference": "Legacy reads latest 300 runs; web includes all detached runs. Ordering/grouping also differ.",
            }
            if explain:
                with reader.connection() as conn:
                    result["query_plans"] = {
                        "projection": [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + reader.projection)],
                        "task_detail": [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + reader.queue_projection + " WHERE q.id=?", (1,))],
                    }
            return result
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=10000, help="Rows per table (mixed has this many queue rows AND runs)")
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--fixture", choices=["queue", "mixed", "both"], default="both")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.items <= 100000 or not 1 <= args.samples <= 1000:
        parser.error("Use 1-100000 items and 1-1000 samples")
    fixtures = [False, True] if args.fixture == "both" else [args.fixture == "mixed"]
    print(json.dumps([benchmark(args.items, args.samples, mixed, args.explain) for mixed in fixtures], indent=2))


if __name__ == "__main__":
    main()
