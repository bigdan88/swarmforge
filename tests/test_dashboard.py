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


def _go(at, page):
    at.button(key="nav_" + page.lower().replace(" ", "-")).click().run()
    return at


def _open(at, n=0):
    """Click the n-th task row in the Work list (expands it underneath)."""
    rows = [b for b in at.button if (b.key or "").startswith("op_")]
    rows[n].click().run()
    return at


def _write_cfg(tmp_path, monkeypatch):
    import streamlit as st
    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return load_config(str(p))


async def test_dashboard_pages_render(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store, run_id, _ = await _run(cfg, sample_repo)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.metric  # rows start collapsed
    _open(at)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["State"] == "Completed" and metrics["Repair cycle"] == "1"
    # delegation graph + transcripts rendered
    assert any("Planner" in g.proto.spec for g in at.get("graphviz_chart"))
    assert any("Brief handed to this agent" in m.value for m in at.markdown)
    assert any("Click a task to open it" in c.value for c in at.caption)
    # the request and the summary of what was done are shown on the run
    assert any("Task & summary" in e.label for e in at.expander)
    assert any("What you asked for" in m.value and "What you need to do" in m.value for m in at.markdown)
    for page in ("Knowledge", "New task", "Benchmarks", "Speed"):
        _go(at, page)
        assert not at.exception, (page, [e.value for e in at.exception])
    _go(at, "Speed")   # the scripted run above lasts seconds, below the 1-minute floor for speed stats
    assert any("No finished runs yet" in i.value for i in at.info)
    _go(at, "Knowledge")
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


@pytest.mark.slow  # starts real background processes (~1 min)
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


@pytest.mark.slow  # starts real background processes (~1 min)
def test_start_run_button_switches_to_runs(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "New task")
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    Store(cfg.db_path).remember_repo(str(sample_repo))  # AppTest can only pick listed options
    _go(at, "Work"); _go(at, "New task")
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    at.selectbox(key="nr_repo").set_value(str(sample_repo))
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "Work"
    assert at.session_state["sel_item"].startswith("r:")
    # The run row is registered asynchronously by the detached background process; the
    # dashboard's fixed 1.5 s grace is not enough when the box is busy (xdist workers +
    # e2e swarm runs). Poll generously for the registration instead of racing it once.
    runs = []
    deadline = time.time() + 90
    while time.time() < deadline:
        runs = Store(cfg.db_path).list_runs()
        if runs and runs[0]["issue_title"] == "Add export":
            break
        time.sleep(0.5)
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
    _go(at, "New task")
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    Store(cfg.db_path).remember_repo(str(tmp_path / "missing-folder"))
    _go(at, "Work"); _go(at, "New task")
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    at.selectbox(key="nr_repo").set_value(str(tmp_path / "missing-folder"))
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert not at.exception
    assert any("neither an existing folder" in e.value for e in at.error)
    assert Store(cfg.db_path).list_runs() == []
    at.radio(key="nr_source").set_value("GitHub issue").run()
    at.text_input(key="nr_url").input("https://github.com/bigdan88/commander_forge/tree/main")
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert any("is not a GitHub issue URL" in e.value for e in at.error)
    assert Store(cfg.db_path).list_runs() == []


def test_queue_import_then_edit_and_reorder_on_work_page(tmp_path, monkeypatch, sample_repo):
    from .conftest import ROOT
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Nothing here yet" in i.value for i in at.info)
    _go(at, "New task")
    at.radio(key="nt_how").set_value("Add to the queue").run()
    at.text_input(key="qi_path").input(str(ROOT / "docs" / "SELF_IMPROVEMENT_BACKLOG.md")).run()
    Store(cfg.db_path).remember_repo(str(sample_repo))
    _go(at, "Work"); _go(at, "New task")
    at.radio(key="nt_how").set_value("Add to the queue").run()
    at.text_input(key="qi_path").input(str(ROOT / "docs" / "SELF_IMPROVEMENT_BACKLOG.md")).run()
    at.selectbox(key="qi_repo").set_value(str(sample_repo)).run()
    next(b for b in at.button if b.label == "Import into queue").click().run()
    assert not at.exception, [e.value for e in at.exception]
    store = Store(cfg.db_path)
    items = store.queue_items()
    assert len(items) == 7 and items[0]["title"] == "Make every dashboard section fail-safe"
    assert all(i["repo"] == str(sample_repo.resolve()) and i["chain"] == 1 for i in items)
    assert at.session_state["page"] == "Work"
    assert any("Start queue" in b.label for b in at.button)
    # the first waiting task opens by default; open the 2nd one and move it up
    _open(at, 1)  # the 2nd waiting task
    assert at.session_state["sel_item"] == f"q:{items[1]['id']}"
    next(b for b in at.button if b.key == f"q_up_{items[1]['id']}").click().run()
    assert store.queue_items()[0]["id"] == items[1]["id"]
    # edit its settings
    iid = items[1]["id"]
    at.number_input(key=f"qe_b_{iid}").set_value(12.5)
    at.number_input(key=f"qe_h_{iid}").set_value(10.0)
    at.text_input(key=f"qe_t_{iid}").input("Renamed task")
    next(b for b in at.button if "Save changes" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    it = store.queue_item(iid)
    assert it["title"] == "Renamed task" and it["budget"] == 12.5 and it["max_runtime_s"] == 36000
    # filters
    at.text_input(key="w_search").input("no such task").run()
    assert any("No tasks match" in c.value for c in at.caption)
    # the imported repository is remembered for next time
    assert store.recent_repos()[0] == str(sample_repo.resolve())


def test_live_settings_for_running_run(tmp_path, monkeypatch):
    import json
    import os
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run("20990101-000000-abcdef", state="TASK_EXECUTION", issue_title="Busy run", repo="x/y",
                     budget_usd=5, pid=os.getpid(), max_runtime_s=3600, max_repair_cycles=2,
                     config_json=json.dumps({"limits": {"max_runtime_s": 3600, "max_repair_cycles": 2}}))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    rid = "20990101-000000-abcdef"
    _open(at)
    assert at.session_state["sel_item"] == f"r:{rid}"
    at.number_input(key=f"ls_b_{rid}").set_value(9.0)
    at.number_input(key=f"ls_h_{rid}").set_value(8.0)
    at.number_input(key=f"ls_c_{rid}").set_value(4)
    next(b for b in at.button if b.label == "Apply").click().run()
    assert not at.exception, [e.value for e in at.exception]
    run = store.get_run(rid)
    assert run["budget_usd"] == 9.0 and run["max_runtime_s"] == 8 * 3600 and run["max_repair_cycles"] == 4
    assert store.interventions(rid)[-1]["kind"] == "settings"
    next(b for b in at.button if "Cancel this run" in b.label).click().run()
    assert store.get_run(rid)["cancel_requested"] == 1


@pytest.mark.slow  # starts real background processes (~1 min)
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
    assert rq.lane_paused(Store(cfg.db_path), "main")
    assert [i["status"] for i in Store(cfg.db_path).queue_items()][1] == "queued"
    assert not pid_alive(pid)
    log = (cfg.workdir / "logs" / "queue-worker.log").read_text()
    assert "queue[main]: starting #" in log and "queue[main] paused" in log


async def test_failed_run_offers_start_again_and_continue(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    cfg.limits.max_repair_cycles = 0
    store, run_id, _ = await _run(cfg, sample_repo)
    assert store.get_run(run_id)["state"] == "FAILED"
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _open(at)
    labels = {b.label: b for b in at.button}
    assert "↻ Start again from scratch" in labels
    cont = labels["⏩ Continue from where it stopped"]
    assert not cont.disabled
    assert any(n.label == "Time limit (hours)" for n in at.number_input)


def test_failures_page_and_hidden_attempts(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run("20990101-000000-aaaaaa", state="FAILED", issue_title="Flaky task", repo="x/y", budget_usd=5,
                     state_reason="maximum runtime of 5400s exceeded", config_json="{}")
    store.create_run("20990101-010000-bbbbbb", state="COMPLETED", issue_title="Flaky task", repo="x/y",
                     budget_usd=7.5, config_json="{}", heal_of="20990101-000000-aaaaaa", heal_attempt=1)
    store.failure_save("20990101-000000-aaaaaa", title="Flaky task", repo="x/y", state="FAILED",
                       reason="maximum runtime of 5400s exceeded", category="time_limit",
                       root_cause="The tester ran the whole suite repeatedly.", fix="Run only the new tests.",
                       retry=1, diagnosed_by="planner/qwen", evidence="...", heal_run_id="20990101-010000-bbbbbb",
                       outcome="fixed itself on attempt 1")
    store.create_run("20990101-020000-cccccc", state="FAILED", issue_title="Old failure", repo="x/y",
                     budget_usd=5, state_reason="repair limit reached", config_json="{}")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # the healed attempt is hidden from the Work list; the successful retry says it fixed itself
    rows = [b for b in at.button if (b.key or "").startswith("op_")]
    texts = [m.value for m in at.markdown]
    assert any("Done (fixed itself, attempt 1)" in t for t in texts) and len(rows) == 2
    _go(at, "Failures")
    assert not at.exception, [e.value for e in at.exception]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Failures"] == "1" and metrics["Fixed themselves"] == "1"
    assert any("have no post-mortem yet" in i.value for i in at.info)
    assert any("The tester ran the whole suite repeatedly." in m.value for m in at.markdown)
    # open the healing chain from the failure
    next(b for b in at.button if b.label == "Open the retry").click().run()
    assert at.session_state["page"] == "Work" and at.session_state["sel_item"] == "r:20990101-010000-bbbbbb"
    assert any("Self-healing attempt 1" in m.value for m in at.markdown)


def test_password_gate(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARMFORGE_DASH_PASSWORD", "open sesame")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    assert not [b for b in at.button if (b.key or "").startswith("nav_")]  # nothing before the password
    at.text_input(key="_pw").input("wrong").run()
    assert any("Wrong password" in e.value for e in at.error)
    at.text_input(key="_pw").input("open sesame").run()
    assert not at.exception and at.session_state["page"] == "Work"
    assert [b for b in at.button if (b.key or "").startswith("nav_")]


def test_bulk_select_and_delete(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    run_dir = cfg.workdir / "runs" / "20990101-000000-aaaaaa"
    (run_dir / "repo").mkdir(parents=True)
    store.create_run("20990101-000000-aaaaaa", state="FAILED", issue_title="Old one", repo="x/y", budget_usd=5,
                     state_reason="boom", config_json="{}", run_dir=str(run_dir))
    store.event("20990101-000000-aaaaaa", "run", "hello")
    keep = store.queue_add("Keep me", "body", "x/y")
    gone = store.queue_add("Remove me", "body", "x/y")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [b for b in at.button if b.key == "bk_del"]  # no bulk bar until something is ticked
    at.checkbox(key=f"ck_q_{gone}").check().run()
    at.checkbox(key="ck_r_20990101-000000-aaaaaa").check().run()
    assert any("2 selected" in m.value for m in at.markdown)
    at.button(key="bk_del").click().run()
    assert any("can't be undone" in w.value for w in at.warning)
    at.button(key="bk_yes").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [i["id"] for i in store.queue_items()] == [keep]
    assert store.get_run("20990101-000000-aaaaaa") is None and not store.events("20990101-000000-aaaaaa")
    assert not run_dir.exists()
    assert any("Deleted 2 task(s)" in s.value for s in at.success)


def test_new_task_offers_recent_repos(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.remember_repo("bigdan88/old")
    store.remember_repo("bigdan88/swarmforge")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _go(at, "New task")
    sb = at.selectbox(key="nr_repo")
    assert sb.value == "bigdan88/swarmforge" and list(sb.options)[:2] == ["bigdan88/swarmforge", "bigdan88/old"]
    store.remember_repo("bigdan88/old")
    assert store.recent_repos()[:2] == ["bigdan88/old", "bigdan88/swarmforge"]
