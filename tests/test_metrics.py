"""Speed metrics: phases, waits, roles and settings fingerprints come out of what runs record."""
import json

from swarmforge import metrics as mx
from swarmforge.db import Store


def _run(st, rid, t0, cfg=None):
    st.create_run(rid, config_json=json.dumps(cfg or {
        "roles": {"developer_backend": {"model": "zai/GLM-5.3", "max_steps": 45},
                  "reviewer": {"model": "moon/Kimi-K2.6", "max_steps": 15}},
        "developer_roles": ["developer_backend"], "limits": {"max_repair_cycles": 1, "tester_every_cycle": False},
        "review": {"blocking_severities": ["critical"]}}))
    st._exec("UPDATE runs SET created_at = ?, issue_title = 'T' WHERE id = ?", (t0, rid))


def _state(st, rid, ts, to):
    st._exec("INSERT INTO events (run_id, ts, level, kind, message, data) VALUES (?, ?, 'info', 'state', ?, ?)",
             (rid, ts, f"-> {to}", json.dumps({"to": to})))


def _store_with_run(tmp_path):
    st = Store(tmp_path / "db")
    _run(st, "r1", 1000.0)
    st._exec("INSERT INTO events (run_id, ts, level, kind, message) VALUES ('r1', 1600, 'info', 'queue', "
             "'repository is free after 10 min; starting')")
    for ts, to in [(1660, "REPOSITORY_INSPECTION"), (1840, "PLANNING"), (2140, "TASK_EXECUTION"),
                   (3340, "INTEGRATION"), (3400, "TESTING"), (4000, "CODE_REVIEW"), (4600, "APPROVED"),
                   (4660, "PULL_REQUEST_CREATION"), (4900, "COMPLETED")]:
        _state(st, "r1", ts, to)
    st.update_run("r1", state="COMPLETED", finished_at=4900.0, repair_cycle=0)
    st._exec("INSERT INTO approvals (run_id, ts, question, status, decided_at) VALUES ('r1', 4670, 'push?', "
             "'approved', 4880)")
    st.upsert_task("r1", "T1", cycle=0, kind="code", title="t", role="developer_backend", model="zai/GLM-5.3",
                   state="INTEGRATED", attempts=2, started_at=2140.0)
    st._exec("UPDATE tasks SET ended_at = 3340 WHERE task_key = 'T1'")
    for i in range(3):
        st.record_llm_call("r1", task_key="review-c0", role="reviewer", model="moon/Kimi-K2.6",
                           started_at=4000.0 + 100 * i, ended_at=4060.0 + 100 * i, cost_usd=0.1)
    st._exec("INSERT INTO events (run_id, ts, level, kind, message) VALUES ('r1', 3000, 'warn', 'agent', "
             "'end: timeout after 27 step(s), 44 tool call(s), 1200s - Pi exceeded 1200s')")
    st._exec("INSERT INTO events (run_id, ts, level, kind, message) VALUES ('r1', 3001, 'warn', 'task', "
             "'attempt 1 failed: Pi exceeded 1200s')")
    for msg in ('sf browse: {"url": "http://localhost:8501"}', "sf run_tests: {}", "browse: http://localhost:1"):
        st._exec("INSERT INTO events (run_id, ts, level, kind, message) VALUES ('r1', 3100, 'info', 'agent', ?)", (msg,))
    return st


def test_run_metrics_splits_time_into_phases(tmp_path):
    st = _store_with_run(tmp_path)
    m = mx.run_metrics(st, "r1")
    p = m["phases_s"]
    assert m["total_s"] == 3900 and p["waiting"] == 600
    assert p["setup"] == 240 and p["planning"] == 300 and p["coding"] == 1260
    assert p["testing"] == 600 and p["review"] == 600 and p["delivery"] == 300
    assert sum(p.values()) == m["total_s"] and m["work_s"] == 3300
    assert m["approval_wait_s"] == 210 and m["agent_timeouts"] == 1
    assert m["sf_calls"] == 2 and m["browse_calls"] == 2
    assert m["roles"]["developer_backend"]["retries"] == 1 and m["roles"]["developer_backend"]["time_s"] == 1200
    assert m["roles"]["reviewer"]["time_s"] == 180 and round(m["cost_usd"], 2) == 0.3   # no task row: call time
    assert m["settings"] == "be=GLM-5.3/45 rev=Kimi-K2.6/15 cycles=1 block=critical tester=once"
    assert "coding" in mx.format_run(m) and "reviewer" in mx.format_run(m)


def test_collect_summary_csv_and_agent_rows(tmp_path):
    st = _store_with_run(tmp_path)
    _run(st, "r0", 10.0)
    st.update_run("r0", state="FAILED", finished_at=12.0)          # died at once: ignored
    rows = mx.collect(st)
    assert [r["run_id"] for r in rows] == ["r1"] and rows[0]["coding_min"] == 21
    s = mx.summarize(rows)
    assert s["all"]["runs"] == 1 and list(s["by_settings"]) == [rows[0]["settings"]]
    csv = mx.to_csv(rows)
    assert csv.splitlines()[0].startswith("run_id,title,state,settings,total_min")
    a = mx.agent_rows(st)
    assert a == [{"run_id": "r1", "task": "T1", "role": "developer_backend", "model": "GLM-5.3", "cycle": 0,
                  "minutes": 20.0, "calls": 0, "sec_per_call": None, "retries": 1, "state": "INTEGRATED"}]
    path = mx.write_run_metrics(st, "r1", tmp_path)
    assert json.loads(path.read_text())["phases_s"]["coding"] == 1260


def test_metrics_cli(tmp_path, capsys):
    from swarmforge import cli
    st = _store_with_run(tmp_path)
    cfg = type("C", (), {"db_path": tmp_path / "db"})()
    assert cli.cmd_metrics(type("A", (), {"run_id": "r1", "json": False, "last": 50, "csv": None})(), cfg) == 0
    assert "coding" in capsys.readouterr().out
    out_csv = tmp_path / "m.csv"
    assert cli.cmd_metrics(type("A", (), {"run_id": None, "json": False, "last": 50, "csv": str(out_csv)})(), cfg) == 0
    assert out_csv.exists() and (tmp_path / "m-agents.csv").exists()
    assert cli.cmd_metrics(type("A", (), {"run_id": None, "json": False, "last": 50, "csv": None})(), cfg) == 0
    assert "median" in capsys.readouterr().out
    st.close()
