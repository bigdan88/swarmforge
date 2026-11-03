"""Realistic demo data for the dashboard, so screenshots of UI changes show filled pages.

`swarmforge seed-demo PATH` creates a fresh database at PATH with a handful of runs in different
states (finished with a PR, failed, running, blocked), their tasks, tests, findings, costs, a failure
diagnosis and two queues. Used by SwarmForge's own `preview:` (swarmforge.project.yml)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .db import Store

REPO = "bigdan88/demo-app"
FLOW = ["REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION", "TASK_EXECUTION", "INTEGRATION", "TESTING",
        "CODE_REVIEW"]


def _run(st: Store, rid: str, title: str, state: str, start: float, minutes: float, *, reason: str = "",
         pr: int | None = None, cycles: int = 0, visited: list[str] | None = None) -> None:
    end = start + minutes * 60
    st.create_run(rid, state=state, issue_title=title, repo=REPO, budget_usd=20, mode="swarm",
                  state_reason=reason, repair_cycle=cycles, issue_body=f"{title}.\n\nWith tests.",
                  pr_url=f"https://github.com/{REPO}/pull/{pr}" if pr else None,
                  integration_branch=f"swarmforge/task-{rid[-6:]}", base_branch="main",
                  config_json=json.dumps({"roles": {"developer_backend": {"model": "zai-org/GLM-5.3", "max_steps": 45},
                                                    "reviewer": {"model": "moonshotai/Kimi-K2.6", "max_steps": 15}},
                                          "developer_roles": ["developer_backend"],
                                          "limits": {"max_repair_cycles": 1, "tester_every_cycle": False},
                                          "review": {"blocking_severities": ["critical"]}}))
    finished = state in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED")
    st._exec("UPDATE runs SET created_at = ?, updated_at = ?, finished_at = ? WHERE id = ?",
             (start, end, end if finished else None, rid))
    states = visited or FLOW
    step = (end - start) / (len(states) + 1)
    prev = "ISSUE_RECEIVED"
    for i, s in enumerate(states + ([state] if finished else [])):
        st._exec("INSERT INTO events (run_id, ts, level, kind, message, data) VALUES (?, ?, 'info', 'state', ?, ?)",
                 (rid, start + step * (i + 1), f"{prev} -> {s}", json.dumps({"from": prev, "to": s, "reason": ""})))
        prev = s


def seed(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    st = Store(path)
    now = time.time()
    runs = [
        ("20261101-090000-a1b2c3", "Add CSV and JSON export", "COMPLETED", 180, 52, "draft PR created", 41, 0),
        ("20261101-110000-d4e5f6", "Dark mode for the settings page", "COMPLETED", 120, 38, "draft PR created", 42, 1),
        ("20261101-130000-0a1b2c", "Paginate the orders API", "FAILED", 75, 64,
         "repair limit reached (1 cycle(s)); 1 unresolved problem(s): test suite not passing", None, 1),
        ("20261101-143000-3c4d5e", "Show live progress for long jobs", "TASK_EXECUTION", 12, 12, "", None, 0),
        ("20261101-144500-6f7a8b", "Cache GitHub API responses", "BLOCKED", 40, 0.2,
         "Docker is not responding. Start Docker Desktop.", None, 0),
    ]
    for rid, title, state, ago, minutes, reason, pr, cycles in runs:
        visited = FLOW[:4] if state == "TASK_EXECUTION" else FLOW[:1] if state == "BLOCKED" else None
        _run(st, rid, title, state, now - ago * 60, minutes, reason=reason, pr=pr, cycles=cycles, visited=visited)
        if state == "BLOCKED":
            continue
        t0 = now - ago * 60 + 300
        for i, (key, role, model, tstate) in enumerate([
                ("T1", "developer_backend", "zai-org/GLM-5.3", "INTEGRATED"),
                ("T2", "developer_frontend", "zai-org/GLM-5.3",
                 "RUNNING" if state == "TASK_EXECUTION" else "INTEGRATED"),
                ("tester-c0", "tester", "Qwen/Qwen3.8-Flash", "INTEGRATED")]):
            if state == "TASK_EXECUTION" and key == "tester-c0":
                continue
            st.upsert_task(rid, key, cycle=0, kind="test" if key.startswith("tester") else "implement",
                           title={"T1": "Backend change", "T2": "UI change"}.get(key, "Acceptance testing"),
                           role=role, model=model, state=tstate, attempts=1, started_at=t0 + i * 120,
                           ended_at=None if tstate == "RUNNING" else t0 + i * 120 + 400,
                           summary="Implemented and tested." if tstate == "INTEGRATED" else None,
                           depends_on=[] if key != "tester-c0" else ["T1", "T2"])
            for c in range(3):
                st.record_llm_call(rid, task_key=key, role=role, provider="deepinfra", model=model,
                                   started_at=t0 + i * 120 + c * 30, ended_at=t0 + i * 120 + c * 30 + 12,
                                   prompt_tokens=9000, completion_tokens=800, total_tokens=9800,
                                   cost_usd=0.012, cost_source="measured", status="ok")
        if state != "TASK_EXECUTION":
            ok = state == "COMPLETED"
            st.record_test_run(rid, label="cycle0", cycle=0, command="python -m pytest -q", exit_code=0 if ok else 1,
                               passed=212 if ok else 205, failed=0 if ok else 3, errors=0, skipped=2, duration_s=96.0,
                               timed_out=0)
            st.record_finding(rid, cycle=cycles, source="reviewer", severity="minor", file="app/export.py", line=40,
                              title="Name the magic number", description="Use a constant for the 1000-row page size.",
                              recommendation="EXPORT_PAGE_SIZE = 1000")
    st.record_finding("20261101-110000-d4e5f6", cycle=1, source="visual", severity="major",
                      file="settings (dark)", line=None, title="Save button label is cut off",
                      description="The Save button shows 'Sa…' in the dark theme.", recommendation="Widen the column.")
    st.failure_save("20261101-130000-0a1b2c", title="Paginate the orders API", repo=REPO, state="FAILED",
                    reason="repair limit reached", category="tests_failing",
                    root_cause="The new cursor parameter broke three existing API tests.",
                    fix="Keep the old offset parameter working and add the cursor next to it.",
                    lesson="Keep API changes backwards compatible.", retry=1, diagnosed_by="planner/demo",
                    outcome="needs you: not retried automatically", cost_usd=1.42, elapsed_s=3840)
    for title, status, queue in [("Show live progress for long jobs", "running", "main"),
                                 ("Rate-limit the public API", "queued", "main"),
                                 ("Translate the UI to Spanish", "queued", "main"),
                                 ("Tidy the settings layout", "queued", "self-improvement")]:
        qid = st.queue_add(title, f"{title}.", REPO, queue=queue)
        st.queue_update(qid, status=status,
                        run_id="20261101-143000-3c4d5e" if status == "running" else None)
    st.remember_repo(REPO)
    _seed_chat(st)
    st.close()
    write_config(path.parent / "swarmforge.yaml", path.parent)
    return path


def _seed_chat(st: Store) -> None:
    """One demo chat conversation with a persisted tool call, so the Chat page
    shows a real caption. Guarded so seeding never breaks the demo CLI."""
    try:
        from . import chatbot
        conv = chatbot.ensure_default_conversation(st)
        chatbot.add_message(
            st, conv["id"], "user", "what does https://example.com/docs/quickstart say?"
        )
        chatbot.add_message(
            st, conv["id"], "assistant",
            "It's a quickstart guide: install, configure and run your first task.",
            tool_calls=[{"name": "fetch_url",
                        "args": {"url": "https://example.com/docs/quickstart"},
                        "ok": True,
                        "preview": "# Quickstart\nInstall, configure, run."}],
        )
    except Exception:  # noqa: BLE001 - demo seeding must never fail the CLI
        pass


def write_config(dest: Path, workdir: Path) -> Path:
    """A self-contained config for the demo dashboard (no keys needed: nothing calls a model)."""
    import yaml
    role = lambda m: {"provider": "deepinfra", "model": m}  # noqa: E731
    dest.write_text(yaml.safe_dump({
        "providers": {"deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai",
                                    "api_key_env": "DEEPINFRA_API_KEY"}},
        "roles": {"planner": role("Qwen/Qwen3.8-Flash"), "developer_backend": role("zai-org/GLM-5.3"),
                  "developer_frontend": role("zai-org/GLM-5.3"), "reviewer": role("moonshotai/Kimi-K2.6"),
                  "tester": role("Qwen/Qwen3.8-Flash"), "designer": role("Qwen/Qwen3-VL-235B-A22B-Instruct")},
        "developer_roles": ["developer_backend", "developer_frontend"],
        "sandbox": {"mode": "local"},
        "workdir": str(workdir),
        "knowledge": {"vault": str(workdir / "knowledge")},
    }, sort_keys=False), encoding="utf-8")
    return dest
