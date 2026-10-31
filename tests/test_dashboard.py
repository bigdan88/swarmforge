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


def test_new_run_form_validates_before_starting(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.sidebar.radio[0].set_value("New run").run()
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    at.text_input(key="nr_repo").input(str(tmp_path / "missing-folder"))
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert not at.exception
    assert any("neither an existing folder" in e.value for e in at.error)
    assert Store(cfg.db_path).list_runs() == []
    at.radio(key="nr_source").set_value("GitHub issue").run()
    at.text_input(key="nr_url").input("https://github.com/bigdan88/commander_forge/tree/main")
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert any("is not a GitHub issue URL" in e.value for e in at.error)
    assert Store(cfg.db_path).list_runs() == []


def test_queue_page_import_and_reorder(tmp_path, monkeypatch, sample_repo):
    from .conftest import ROOT
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.sidebar.radio[0].set_value("Queue").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("queue is empty" in i.value for i in at.info)
    at.text_input(key="qi_path").input(str(ROOT / "docs" / "SELF_IMPROVEMENT_BACKLOG.md")).run()
    at.text_input(key="qi_repo").input(str(sample_repo)).run()
    next(b for b in at.button if b.label == "Import into queue").click().run()
    assert not at.exception, [e.value for e in at.exception]
    items = Store(cfg.db_path).queue_items()
    assert len(items) == 7 and items[0]["title"] == "Make every dashboard section fail-safe"
    assert all(i["repo"] == str(sample_repo.resolve()) and i["chain"] == 1 for i in items)
    # move the 2nd task up
    next(b for b in at.button if b.key == f"q_up_{items[1]['id']}").click().run()
    assert Store(cfg.db_path).queue_items()[0]["id"] == items[1]["id"]
    assert any("Start queue" in b.label for b in at.button)


def test_background_queue_worker_processes_and_exits(tmp_path, monkeypatch, sample_repo):
    from swarmforge import runqueue as rq
    from swarmforge.launcher import pid_alive
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    iid = store.queue_add("Add export", "Add CSV export with tests.", str(sample_repo), budget=1)
    store.queue_add("Second", "Something else.", str(sample_repo))
    pid = rq.start_worker(cfg, store)
    assert pid
    for _ in range(240):
        it = Store(cfg.db_path).queue_item(iid)
        if it["status"] not in ("queued", "running") and not pid_alive(pid):
            break
        time.sleep(0.5)
    it = Store(cfg.db_path).queue_item(iid)
    # the fake provider is unreachable, so the run fails at planning and the queue pauses (policy: stop)
    assert it["status"] == "failed" and it["run_id"], it
    assert Store(cfg.db_path).kv_get(rq.PAUSED_KEY) == "1"
    assert [i["status"] for i in Store(cfg.db_path).queue_items()][1] == "queued"
    assert not pid_alive(pid)
    log = (cfg.workdir / "logs" / "queue-worker.log").read_text()
    assert "queue: starting #" in log and "queue paused" in log
