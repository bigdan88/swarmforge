"""Run summaries: the original request, what was done, the next step for the user, lessons."""
import json
import time

from swarmforge.db import Store
from swarmforge.report import build_pr_body, build_summary, lessons_recorded, next_step, request_text


def _store(tmp_path):
    return Store(tmp_path / "db.sqlite")


def _run(st, rid="r1", **kw):
    fields = dict(issue_title="Add export", issue_body="Please add CSV export.\n\nWith tests.",
                  repo="bigdan88/app", state="COMPLETED", repair_cycle=0)
    fields.update(kw)
    st.create_run(rid, **fields)
    return st.get_run(rid)


def test_summary_has_request_work_tests_and_next_step(tmp_path):
    st = _store(tmp_path)
    _run(st, pr_url="https://github.com/bigdan88/app/pull/7", state_reason="draft PR created",
         plan_json=json.dumps({"summary": "Add an exporter module and a CLI flag."}))
    st.upsert_task("r1", "T1", kind="implement", title="Exporter", state="INTEGRATED", summary="added exporters.py")
    st.upsert_task("r1", "T2", kind="implement", title="CLI", state="FAILED", error="tests failed")
    st.record_test_run("r1", label="cycle", cycle=0, exit_code=0, passed=20, failed=0, errors=0, skipped=0)
    st.record_acceptance("r1", cycle=0, criterion_id="AC1", text="CSV export works", status="satisfied", evidence="t")
    st.record_acceptance("r1", cycle=0, criterion_id="AC2", text="CLI flag", status="failed", evidence="t")
    s = build_summary(st, "r1")
    assert "**Add export**" in s and "Please add CSV export." in s
    assert "✅ Completed: draft PR created" in s and "Add an exporter module" in s
    assert "✅ T1 Exporter: added exporters.py" in s and "❌ T2 CLI: tests failed" in s
    assert "20 passed, 0 failed" in s and "1/2 satisfied" in s and "AC2 failed" in s
    assert "merge the draft pull request yourself" in s and "pull/7" in s


def test_next_step_for_each_outcome(tmp_path):
    st = _store(tmp_path)
    r = _run(st, "a", state_reason="approved; delivered to branch swarmforge/task-1 in C:/repo", base_branch="main")
    assert "`git diff main...swarmforge/task-1`" in next_step(st, r)
    r = _run(st, "b", state_reason="approved; PR not created (no push access). ...")
    assert "swarmforge publish b" in next_step(st, r)
    r = _run(st, "c", state="PLANNING")
    assert "Still running" in next_step(st, r)
    st.request_approval("c", "Push?")
    assert "approve or decline" in next_step(st, st.get_run("c"))
    r = _run(st, "d", state="FAILED", state_reason="maximum runtime exceeded")
    st.failure_save("d", category="time_limit", fix="Give it more time.", lesson="Keep tasks small.")
    assert "Give it more time." in next_step(st, r)
    assert "[failure] Keep tasks small." in lessons_recorded(st, "d")
    r = _run(st, "e", state="CANCELLED")
    assert "nothing was published" in next_step(st, r)


def test_lessons_come_from_knowledge_events(tmp_path):
    st = _store(tmp_path)
    _run(st)
    st.event("r1", "knowledge", "wrote 1 note", data=[{"stem": "a", "title": "Mock the clock in tests",
                                                       "type": "pitfall"},
                                                      {"stem": "b", "title": "Run r1", "type": "run-log"}])
    assert lessons_recorded(st, "r1") == ["[pitfall] Mock the clock in tests"]
    assert "Mock the clock in tests" in build_summary(st, "r1")
    assert "Lessons recorded" not in build_summary(st, "r1", lessons=False)


def test_request_falls_back_to_the_issue_file(tmp_path):
    f = tmp_path / "issue.md"
    f.write_text("# Title here\n\nBody from the file.\n", encoding="utf-8")
    st = _store(tmp_path)
    r = _run(st, issue_body=None, issue_url=str(f))
    assert request_text(r) == "Body from the file."


def test_pr_body_includes_the_task(tmp_path):
    st = _store(tmp_path)
    _run(st)
    body = build_pr_body(st, "r1")
    assert "## Task" in body and "Please add CSV export." in body


def test_non_blocking_majors_are_listed_as_known_limitations(tmp_path):
    st = _store(tmp_path)
    _run(st, config_json=json.dumps({"review": {"blocking_severities": ["critical"]}}))
    st.record_finding("r1", cycle=0, source="reviewer", severity="major", file="a.py", line=None,
                      title="Race in add_message", description="d", recommendation="r")
    st.record_finding("r1", cycle=0, source="reviewer", severity="minor", file="b.py", line=None,
                      title="Naming", description="d", recommendation="r")
    body = build_pr_body(st, "r1")
    assert "(0 blocking)" in body
    assert body.index("**[major]** `a.py`: Race in add_message") < body.index("**[minor]** `b.py`: Naming")
    _run(st, "r2", config_json=None)                      # older runs: critical + major blocked
    st.record_finding("r2", cycle=0, source="reviewer", severity="major", file="a.py", line=None,
                      title="X", description="d", recommendation="r")
    assert "(1 blocking)" in build_pr_body(st, "r2") and "Known limitations" not in build_pr_body(st, "r2")
