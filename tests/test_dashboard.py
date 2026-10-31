"""Dashboard, launcher and approval-channel tests."""
import threading
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.approvals import dashboard_approver  # noqa: E402
from swarmforge.config import load_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402

from .conftest import ISSUE, base_raw  # noqa: E402
from .test_e2e import _run  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"


def _write_cfg(tmp_path, monkeypatch):
    import streamlit as st
    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return load_config(str(p))


async def test_dashboard_pages_render(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store, run_id, _ = await _run(cfg, sample_repo)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["State"] == "Completed" and metrics["Repair cycle"] == "1"
    # delegation graph + transcripts rendered
    assert any("Planner" in g.proto.spec for g in at.get("graphviz_chart"))
    assert any("Brief handed to this agent" in m.value for m in at.markdown)
    for page in ("Knowledge", "New run", "Benchmarks"):
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])
    at.sidebar.radio[0].set_value("Knowledge").run()
    assert any("Exporters must wrap every OSError" in m.value for m in at.markdown)


def test_dashboard_approval_channel(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.create_run("r1")
    approve = dashboard_approver(store, lambda: "r1", timeout_s=20, poll_s=0.05)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("v", approve("Push branch x?")))
    t.start()
    for _ in range(100):
        pend = store.pending_approvals("r1")
        if pend:
            break
        time.sleep(0.05)
    assert pend and pend[0]["question"] == "Push branch x?"
    store.decide_approval(pend[0]["id"], True)
    t.join(5)
    assert result["v"] is True and not store.pending_approvals()
    # timeout -> declined
    quick = dashboard_approver(store, lambda: "r1", timeout_s=0.2, poll_s=0.05)
    assert quick("again?") is False


def test_launcher_starts_background_run(tmp_path, monkeypatch, sample_repo):
    from swarmforge.launcher import pid_alive, start_run
    cfg = _write_cfg(tmp_path, monkeypatch)
    cfg.limits.budget_usd = 1
    run_id, log = start_run(cfg, repo=str(sample_repo), issue_title="Add export",
                            issue_body=ISSUE.read_text(), approval="dashboard")
    store = Store(cfg.db_path)
    deadline = time.time() + 90
    run = None
    while time.time() < deadline:
        run = store.get_run(run_id)
        if run and run["state"] in ("FAILED", "COMPLETED"):
            break
        time.sleep(0.5)
    # the fake provider host doesn't exist, so the real background process must fail cleanly at planning
    assert run is not None and run["state"] == "FAILED", run
    assert "planner" in (run["state_reason"] or "")
    assert log.is_file() and run_id in log.read_text()
    for _ in range(120):  # the process exits after writing its report / knowledge notes
        if not pid_alive(run["pid"]):
            break
        time.sleep(0.5)
    assert not pid_alive(run["pid"])


def test_start_run_button_switches_to_runs(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.sidebar.radio[0].set_value("New run").run()
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    at.text_input(key="nr_repo").input(str(sample_repo))
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "Runs"
    runs = Store(cfg.db_path).list_runs()
    assert runs and runs[0]["issue_title"] == "Add export"
    from swarmforge.launcher import pid_alive
    for _ in range(240):  # let the background run (which fails on the fake provider) finish
        r = Store(cfg.db_path).get_run(runs[0]["id"])
        if r["state"] in ("FAILED", "COMPLETED") and not pid_alive(r["pid"]):
            break
        time.sleep(0.5)
