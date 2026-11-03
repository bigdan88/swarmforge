"""Outcome/cost evidence for self-healing; no additional model calls."""
from __future__ import annotations


def healing_summary(store, limit: int = 300) -> list[dict]:
    runs = store.list_runs(limit)
    by_id = {r["id"]: r for r in runs}
    chains: dict[str, list[dict]] = {}
    for run in runs:
        if not run.get("heal_of"):
            continue
        root = run
        visited = set()
        while root.get("heal_of") in by_id and root["id"] not in visited:
            visited.add(root["id"])
            root = by_id[root["heal_of"]]
        chains.setdefault(root["id"], []).append(run)
    groups: dict[str, dict] = {}
    for root, attempts in chains.items():
        fail = store.failure(root) or {}
        category = fail.get("category", "unknown")
        group = groups.setdefault(category, {"category": category, "chains": 0, "recovered": 0,
                                              "attempts": 0, "additional_cost_usd": 0.})
        group["chains"] += 1
        group["attempts"] += len(attempts)
        group["recovered"] += int(any(r["state"] == "COMPLETED" for r in attempts))
        group["additional_cost_usd"] += sum(store.cost_summary(r["id"])["total"] for r in attempts)
    for group in groups.values():
        group["additional_cost_usd"] = round(group["additional_cost_usd"], 4)
        group["cost_per_recovery_usd"] = (round(group["additional_cost_usd"] / group["recovered"], 4)
                                           if group["recovered"] else None)
    return [groups[k] for k in sorted(groups)]
