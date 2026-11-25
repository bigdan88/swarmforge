"""The project review page: what was built, how it was tested, checks, screenshots, report."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from swarmforge.db import Store
from swarmforge.projects import service
from swarmforge.projects.store import Projects
from swarmforge.webapp.app import create_app

from .test_projects import definition
from .test_web_artifacts import png


def built_project(tmp_path):
    db, root = tmp_path / "history.db", tmp_path / "runs"
    shots = root / "run-a" / "artifacts" / "visual"
    shots.mkdir(parents=True)
    (root / "run-a" / "report.md").write_text("# Report\n\n## Plan\n\nAdded a search box over transcripts.\n\nAcceptance criteria:\n- x\n\n## Tasks\n", encoding="utf-8")
    (shots / "c1-home-light.png").write_bytes(png())
    s = Store(db)
    ps = Projects(s)
    p = ps.create("MNEMOS")
    pid = p["id"]
    ps.draft(pid, definition())
    service.approve(s, pid)
    s.create_run("run-a", repo="owner/inventory")
    s.update_run("run-a", state="COMPLETED", state_reason="draft PR created", pr_url="https://github.com/owner/inventory/pull/7")
    qid = s.queue_add("Search box", "x", "owner/inventory", run_id="run-a", status="done")
    s.queue_update(qid, project_id=pid, milestone=0, merged_sha="abc")
    s._exec("INSERT INTO test_runs(run_id,ts,cycle,label,exit_code,passed,failed,errors,skipped,timed_out) VALUES ('run-a',1,0,'cycle0',0,42,0,0,0,0)")
    s._exec("UPDATE milestones SET status='checkpoint', report=? WHERE project_id=? AND idx=0",
            (json.dumps({"summary": "Search works on fixtures.", "checks": [
                {"text": "A known phrase is found by keyword", "status": "passed", "evidence": [{"test": "tests.test_search.test_phrase"}]},
                {"text": "Runs on the GPU", "status": "needs you", "evidence": []}]}), pid))
    ps.update(pid, status="at_checkpoint")
    s.close()
    return db, root, pid, qid


def test_review_shows_what_was_built_with_checks_screenshots_and_a_report(tmp_path):
    db, root, pid, qid = built_project(tmp_path)
    with TestClient(create_app(db, tmp_path, runs_root=root), base_url="http://127.0.0.1") as client:
        r = client.get(f"/api/v1/projects/{pid}/review").json()
        doc = client.get(f"/api/v1/projects/{pid}/review.md")
        shot = client.get(f"/api/v1/work/task-{qid}/screenshots/c1-home-light.png?run_id=run-a")
    m0 = r["milestones"][0]
    assert m0["in_review"] and m0["summary"] == "Search works on fixtures."
    assert [c["status"] for c in m0["checks"]] == ["passed", "needs you"] and m0["checks"][0]["evidence"] == ["tests.test_search.test_phrase"]
    t = r["tasks"][0]
    assert t["title"] == "Search box" and t["merged"] and t["pr_url"].endswith("/pull/7")
    assert t["tests"]["passed"] == 42 and t["screenshots"] == ["c1-home-light.png"]
    assert t["what_it_did"] == "Added a search box over transcripts."
    assert r["counts"] == {"tasks": 1, "merged": 1, "checks_passed": 1, "checks_need_you": 1, "checks_failed": 0}
    assert doc.status_code == 200 and "attachment" in doc.headers["content-disposition"]
    assert "✅ A known phrase is found by keyword" in doc.text and "👤 Runs on the GPU" in doc.text and "42 tests passed" in doc.text
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"


def test_review_of_an_unknown_project_is_404(tmp_path):
    Store(tmp_path / "h.db").close()
    with TestClient(create_app(tmp_path / "h.db", tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/projects/nope/review").status_code == 404


EXPLAINED = {
    "headline": "You can now search everything your recorder heard by typing a word.",
    "what_you_can_do": ["Type a word and see every moment it was said", "  "],
    "how_to_try": ["Run `mnemos serve`", "Open http://localhost:8000 and type a word"],
    "not_yet": ["Only checked on sample recordings, not your real recorder"],
    "needs_you": ["Try it on a day of real audio"],
    "architecture": {"nodes": [{"id": "rec", "label": "Recorder", "kind": "device", "note": "Captures audio all day", "where": ""},
                               {"id": "api", "label": "Search service", "kind": "service", "note": "Finds words", "where": "src/search.py"},
                               {"id": "db", "label": "Transcript store", "kind": "data", "note": "Keeps the text"},
                               {"id": "api", "label": "duplicate id is dropped"},
                               {"id": "x", "label": "Odd kind", "kind": "<script>"}],
                     "edges": [{"from": "rec", "to": "api", "label": "audio"}, {"from": "api", "to": "db", "label": "text"},
                               {"from": "api", "to": "ghost"}, {"from": "db", "to": "db"}]},
    "flow": {"title": "Searching", "nodes": [{"id": "s", "label": "You type a word", "kind": "start"},
                                            {"id": "d", "label": "Any matches?", "kind": "decision"},
                                            {"id": "e", "label": "Results with timestamps", "kind": "end"}],
             "edges": [{"from": "s", "to": "d"}, {"from": "d", "to": "e", "label": "yes"}]},
    "screenshots": [{"task": "TASK", "name": "c1-home-light.png", "caption": "The search page"},
                    {"task": "TASK", "name": "not-a-real-shot.png", "caption": "made up"}]}


def test_explainer_is_written_by_the_lead_cleaned_and_shown_on_top_of_the_review(tmp_path, cfg):
    import httpx

    from swarmforge.projects import explainer
    from swarmforge.webapp.artifacts import ArtifactReader
    from swarmforge.webapp.review import project_review

    db, root, pid, qid = built_project(tmp_path)
    answer = json.loads(json.dumps(EXPLAINED).replace("TASK", f"task-{qid}"))
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50, "estimated_cost": .01}})

    s = Store(db)
    review = project_review(s.conn, ArtifactReader(root), pid)
    assert review["explainer"] is None and review["explainer_scope"] == "m0"
    state = explainer.start(s, cfg, pid, review, background=False, transport=httpx.MockTransport(respond),
                            snapshot={"tree": ["src/search.py"], "key_files": {"README.md": "Run mnemos serve"}})
    assert state["status"] == "ready" and not state["stale"]
    user = json.loads(sent[0]["messages"][-1]["content"])
    assert user["explain"] == "the milestone under review" and user["repository"]["files"] == ["src/search.py"]
    assert user["delivered_tasks"][0]["screenshots"] == ["c1-home-light.png"]
    e = state["explainer"]
    assert e["what_you_can_do"] == ["Type a word and see every moment it was said"]
    assert [n["id"] for n in e["architecture"]["nodes"]] == ["rec", "api", "db", "x"]
    assert e["architecture"]["nodes"][3]["kind"] == "service"  # unknown kinds fall back, never rendered as given
    assert [(x["from"], x["to"]) for x in e["architecture"]["edges"]] == [("rec", "api"), ("api", "db")]
    assert e["flow"]["title"] == "Searching" and e["flow"]["nodes"][1]["kind"] == "decision"
    assert [x["name"] for x in e["screenshots"]] == ["c1-home-light.png"] and e["screenshots"][0]["run_id"] == "run-a"
    s.close()

    with TestClient(create_app(db, tmp_path, runs_root=root), base_url="http://127.0.0.1") as client:
        r = client.get(f"/api/v1/projects/{pid}/review").json()
        doc = client.get(f"/api/v1/projects/{pid}/review.md").text
    assert r["explainer"]["status"] == "ready" and r["explainer"]["explainer"]["headline"].startswith("You can now search")
    assert doc.index("## In plain English") < doc.index("## Milestone 1")
    assert "```mermaid\nflowchart LR" in doc and 'n0(["Recorder — Captures audio all day"])' not in doc
    assert 'n0["Recorder — Captures audio all day"]' in doc and 'n1 -- "text" --> n2' in doc
    assert "n1{\"Any matches?\"}" in doc and "1. Run `mnemos serve`" in doc

    # New delivered work marks the explainer out of date, so the page offers to rewrite it.
    s = Store(db)
    s.queue_update(qid, merged_sha=None)
    assert project_review(s.conn, ArtifactReader(root), pid)["explainer"]["stale"]
    s.close()


def test_a_failed_explainer_is_reported_and_can_be_retried(tmp_path, cfg):
    import httpx

    from swarmforge.projects import explainer
    from swarmforge.webapp.artifacts import ArtifactReader
    from swarmforge.webapp.review import project_review

    db, root, pid, _ = built_project(tmp_path)
    s = Store(db)
    review = project_review(s.conn, ArtifactReader(root), pid)
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                                                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "estimated_cost": 0}}))
    state = explainer.start(s, cfg, pid, review, background=False, transport=bad, snapshot={})
    assert state["status"] == "failed" and "no explanation" in state["error"]
    s.close()


def test_explain_project_action_starts_the_lead(tmp_path, cfg, monkeypatch):
    from swarmforge.projects import explainer
    from swarmforge.services.dashboard import DashboardActions

    db, root, pid, _ = built_project(tmp_path)
    started = []
    monkeypatch.setattr(explainer, "start", lambda store, config, target, review: started.append((target, review["explainer_scope"])) or {"status": "writing", "started_at": 1e12})
    s = Store(db)
    actions = DashboardActions(s, cfg)
    actions.review_artifacts = __import__("swarmforge.webapp.artifacts", fromlist=["ArtifactReader"]).ArtifactReader(root)
    result = actions.execute("0f8c2b8e-6d2a-4c1e-9a8e-1b2c3d4e5f60", "explain_project", pid, {})
    assert started == [(pid, "m0")] and result["ok"] and "explainer" in result["message"]
    s.close()
