"""AppTest coverage for the run status card and the "Run again" pre-fill (dashboard.py)."""
import json
import os
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.config import load_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402

from .conftest import base_raw  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"

DELIVERED = "approved; delivered to branch swarmforge/task-x in /repo"
BLOCKED_REASON = "cannot access repository foo/bar: permission denied. Check the name and credentials."
FAILED_REASON = ("repair limit reached (2 cycle(s)); 1 unresolved problem(s): "
                 "[major] crash when budget is NULL")


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


def _seed(store, run_id, **fields):
    run = {"state": "COMPLETED", "issue_title": "Add export", "repo": "/repo", "budget_usd": 5.0,
           "config_json": json.dumps({"limits": {"max_repair_cycles": 2, "max_runtime_s": 3600}})}
    run.update(fields)
    store.create_run(run_id, **run)


def _open(at, run_id):
    """Open the run's detail panel from the Work list."""
    at.button(key=f"op_r_{run_id}").click().run()
    return at


def _nodes(at):
    """Every rendered node of the main area, in DOM order (blocks and elements)."""
    return list(at.main)


def _idx(nodes, pred):
    for i, n in enumerate(nodes):
        if pred(n):
            return i
    return -1


def _alert_idx(nodes, headline):
    return _idx(nodes, lambda n: n.type in ("success", "info", "warning", "error")
                and headline in (getattr(n, "value", "") or ""))


def _metric_idx(nodes, label):
    return _idx(nodes, lambda n: n.type == "metric" and getattr(n, "label", None) == label)


def _card_above_metrics(at, headline):
    """The card's headline alert renders before the State metric."""
    nodes = _nodes(at)
    h, m = _alert_idx(nodes, headline), _metric_idx(nodes, "State")
    assert h >= 0, f"headline {headline!r} not rendered"
    assert m >= 0, "State metric not rendered"
    assert h < m, f"card (#{h}) must come before the State metric (#{m})"


# ------------------------------------------------------------------ COMPLETED

def test_completed_card_above_metrics_with_git_commands(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-000000-aaaaaa", state="COMPLETED", state_reason=DELIVERED,
          integration_branch="swarmforge/task-x", base_branch="main")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _open(at, "20990101-000000-aaaaaa")
    assert not at.exception, [e.value for e in at.exception]
    headline = "Done — the work is on a new local branch"
    assert any(headline in s.value for s in at.success)
    _card_above_metrics(at, headline)
    # detail text and the pre-existing state_reason banner both show
    assert any("technical reason" in m.value and DELIVERED in m.value for m in at.markdown)
    assert any(DELIVERED in s.value for s in at.success)
    # the git commands are copyable code blocks
    code = [c.value for c in at.code]
    assert "git diff main...swarmforge/task-x" in code
    assert "git merge swarmforge/task-x" in code
    assert "git show swarmforge/task-x" in code
    # bulleted actions render as markdown bullets
    assert any(m.value.startswith("- Review the changes:") for m in at.markdown)
    # no retry button for a successful run
    assert not [b for b in at.button if "Run again with the same settings" in b.label]


def test_completed_with_pr_card(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-010000-bbbbbb", state="COMPLETED", state_reason="approved; PR created",
          pr_url="https://github.com/o/r/pull/12")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-010000-bbbbbb")
    assert not at.exception, [e.value for e in at.exception]
    headline = "Done — a draft pull request is ready to review"
    assert any(headline in s.value for s in at.success)
    _card_above_metrics(at, headline)
    assert "https://github.com/o/r/pull/12" in [c.value for c in at.code]


def test_completed_null_budget_card_renders(tmp_path, monkeypatch):
    """budget_usd NULL must not crash the card (regression for the BUDGET_EXCEEDED TypeError)."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-020000-cccccc", state="COMPLETED",
          state_reason="approved; PR creation disabled in config", budget_usd=None)
    assert store.get_run("20990101-020000-cccccc")["budget_usd"] is None
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-020000-cccccc")
    assert not at.exception, [e.value for e in at.exception]
    assert not [c for c in at.caption if "explanation unavailable" in c.value]
    headline = "Done — the run finished successfully"
    assert any(headline in s.value for s in at.success)
    _card_above_metrics(at, headline)
    assert any("$0.00" in m.value for m in at.markdown)


# ------------------------------------------------------------------- BLOCKED

def test_blocked_card_above_metrics(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-030000-dddddd", state="BLOCKED", state_reason=BLOCKED_REASON)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-030000-dddddd")
    assert not at.exception, [e.value for e in at.exception]
    headline = "Blocked — the repository could not be reached"
    assert any(headline in w.value for w in at.warning)
    _card_above_metrics(at, headline)
    assert any(BLOCKED_REASON[:40] in m.value for m in at.markdown)  # detail keeps the raw reason
    assert any("GITHUB_TOKEN" in m.value for m in at.markdown)        # bulleted next steps
    assert [b for b in at.button if "Run again with the same settings" in b.label]


# -------------------------------------------------------------------- FAILED

def test_failed_card_and_run_again_prefills_new_run(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-040000-eeeeee", state="FAILED", state_reason=FAILED_REASON,
          budget_usd=7.0, issue_title="Crash fix", issue_body="Fix the NULL budget crash.",
          repo="/repo")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-040000-eeeeee")
    assert not at.exception, [e.value for e in at.exception]
    headline = "Finished with unresolved problems"
    assert any(headline in e.value for e in at.error)
    _card_above_metrics(at, headline)
    assert any("[major] crash when budget is NULL" in m.value for m in at.markdown)  # problems summarized
    # the pre-existing state_reason banner is still there
    assert any(FAILED_REASON in e.value for e in at.error)

    btn = next(b for b in at.button if "Run again with the same settings" in b.label)
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "New task"
    # the form is pre-filled with the original run's settings
    assert at.session_state["nr_source"] == "Describe it here"
    assert at.session_state["nr_title"] == "Crash fix"
    assert at.session_state["nr_body"] == "Fix the NULL budget crash."
    assert at.session_state["nr_repo"] == "/repo"
    assert at.session_state["nr_budget"] == 7.0
    assert at.session_state["nr_cycles"] == 2
    assert at.radio(key="nr_source").value == "Describe it here"
    assert at.text_input(key="nr_title").value == "Crash fix"
    assert at.text_area(key="nr_body").value == "Fix the NULL budget crash."
    assert at.selectbox(key="nr_repo").value == "/repo"
    assert at.number_input(key="nr_budget").value == 7.0
    assert at.number_input(key="nr_cycles").value == 2
    # the repository was remembered so the selectbox can offer it
    assert "/repo" in Store(cfg.db_path).recent_repos()


def test_run_again_button_on_blocked_and_cancelled(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-050000-ffffff", state="BLOCKED", state_reason=BLOCKED_REASON)
    _seed(store, "20990101-060000-999999", state="CANCELLED", state_reason="interrupted by user")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-060000-999999")
    assert not at.exception, [e.value for e in at.exception]
    assert any("The run was cancelled" in i.value for i in at.info)
    assert [b for b in at.button if "Run again with the same settings" in b.label]
    _open(at, "20990101-050000-ffffff")  # close the cancelled run, open the blocked one
    assert not at.exception, [e.value for e in at.exception]
    assert [b for b in at.button if "Run again with the same settings" in b.label]


# ------------------------------------------------------- other run states

def test_running_card_above_metrics(tmp_path, monkeypatch):
    """A live run gets the card too, with the current phase in words."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-070000-111111", state="TASK_EXECUTION", pid=os.getpid(),
          max_runtime_s=3600, max_repair_cycles=2)
    store.event("20990101-070000-111111", "task", "RUNNING: coding", task_key="t1")
    store.event("20990101-070000-111111", "task", "SUCCEEDED", task_key="t2")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-070000-111111")
    assert not at.exception, [e.value for e in at.exception]
    headline = "The run is in progress"
    assert any(headline in i.value for i in at.info)
    _card_above_metrics(at, headline)
    # t1 is still running, t2 already succeeded -> exactly one task
    assert any("Developers are coding 1 task." in m.value for m in at.markdown)
    # no retry button while the run is live
    assert not [b for b in at.button if "Run again with the same settings" in b.label]


def test_budget_exceeded_null_budget_card(tmp_path, monkeypatch):
    """BUDGET_EXCEEDED with budget_usd NULL must not crash the card (regression)."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-080000-222222", state="BUDGET_EXCEEDED",
          state_reason="budget of $5.00 exceeded", budget_usd=None)
    assert store.get_run("20990101-080000-222222")["budget_usd"] is None
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-080000-222222")
    assert not at.exception, [e.value for e in at.exception]
    assert not [c for c in at.caption if "explanation unavailable" in c.value]
    headline = "Stopped — the budget was used up"
    assert any(headline in e.value for e in at.error)
    _card_above_metrics(at, headline)
    assert any("$0.00" in m.value for m in at.markdown)


def test_run_again_github_issue_prefills_url(tmp_path, monkeypatch):
    """A run sourced from a GitHub issue re-opens the form in 'GitHub issue' mode."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed(store, "20990101-090000-333333", state="FAILED", state_reason=FAILED_REASON,
          issue_url="https://github.com/o/r/issues/42", issue_title=None, issue_body=None,
          repo=None, budget_usd=None)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at, "20990101-090000-333333")
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if "Run again with the same settings" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "New task"
    assert at.session_state["nr_source"] == "GitHub issue"
    assert at.session_state["nr_url"] == "https://github.com/o/r/issues/42"
    assert at.radio(key="nr_source").value == "GitHub issue"
    assert at.text_input(key="nr_url").value == "https://github.com/o/r/issues/42"
    # NULL budget falls back to the configured default, clamped into the widget range
    assert at.number_input(key="nr_budget").value == float(cfg.limits.budget_usd)