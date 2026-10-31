"""`swarmforge watch`: labelled GitHub issues become queued runs; the issue is kept up to date."""
import json

import httpx
import pytest

from swarmforge import runqueue as rq
from swarmforge import watch as W
from swarmforge.db import Store
from swarmforge.github import GitHubClient


class FakeGitHub:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        if req.method == "GET" and req.url.path.endswith("/issues"):
            assert req.url.params["labels"] == "swarmforge" and req.url.params["state"] == "open"
            return httpx.Response(200, json=self.issues)
        if req.url.path.endswith("/comments"):
            return httpx.Response(201, json={"html_url": "https://github.com/c/1"})
        return httpx.Response(200, json=[])


def _issue(n, title="Fix the export", pr=False):
    d = {"number": n, "title": title, "body": f"body {n}", "labels": [{"name": "swarmforge"}],
         "html_url": f"https://github.com/bigdan88/app/issues/{n}"}
    if pr:
        d["pull_request"] = {}
    return d


@pytest.fixture
def gh(cfg, monkeypatch):
    monkeypatch.setenv(cfg.github.token_env, "tok")
    fake = FakeGitHub([_issue(5), _issue(6, pr=True)])
    cfg.watch.repos = ["bigdan88/app"]
    return GitHubClient(cfg.github, transport=httpx.MockTransport(fake)), fake


def test_poll_queues_labelled_issues_once_and_updates_them(cfg, tmp_path, gh):
    client, fake = gh
    st = Store(tmp_path / "db.sqlite")
    added = W.poll_once(cfg, st, client, echo=None)
    assert len(added) == 1                                     # the pull request is ignored
    item = st.queue_item(added[0])
    assert item["issue_url"] == "https://github.com/bigdan88/app/issues/5"
    assert item["repo"] == "https://github.com/bigdan88/app" and item["chain"] == 0
    assert item["approval"] == "dashboard"                     # pushing still asks by default
    paths = [(m, p) for m, p, _ in fake.calls]
    assert ("DELETE", "/repos/bigdan88/app/issues/5/labels/swarmforge") in paths
    assert ("POST", "/repos/bigdan88/app/issues/5/labels") in paths
    assert any(p.endswith("/issues/5/comments") and "queued this issue as task" in b["body"]
               for _, p, b in fake.calls if b)
    assert W.poll_once(cfg, st, client, echo=None) == []      # still queued: not added twice
    st.queue_update(added[0], status="done")
    assert len(W.poll_once(cfg, st, client, echo=None)) == 1  # label added again later = run it again


def test_run_end_comments_summary_and_sets_final_label(cfg, tmp_path, gh):
    client, fake = gh
    st = Store(tmp_path / "db.sqlite")
    st.create_run("r1", issue_title="Fix the export", issue_body="body 5", state="COMPLETED",
                  state_reason="draft PR created", pr_url="https://github.com/bigdan88/app/pull/9")
    W.on_run_end(cfg, st, {"issue_url": "https://github.com/bigdan88/app/issues/5"}, "r1", client)
    comment = next(b["body"] for _, p, b in fake.calls if b and p.endswith("/comments"))
    assert "draft PR https://github.com/bigdan88/app/pull/9" in comment and "What was done" in comment
    assert "What you asked for" not in comment
    labels = [b["labels"] for _, p, b in fake.calls if b and p.endswith("/labels")]
    assert labels == [["swarmforge:done"]]


def test_watch_needs_repos(cfg, tmp_path):
    out = []
    assert W.watch(cfg, Store(tmp_path / "db"), once=True, echo=out.append) == 2 and "nothing to watch" in out[0]


async def test_queue_runs_issue_items_from_github_and_reports_back(cfg, tmp_path, monkeypatch):
    st = Store(tmp_path / "db.sqlite")
    seen, hooks = {}, []

    class FakeOrch:
        def __init__(self, c, store, **kw):
            self.store = store

        async def execute(self, **kw):
            seen.update(kw)
            self.store.create_run(kw["run_id"], state="COMPLETED", state_reason="ok")
            return kw["run_id"]

    import swarmforge.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "Orchestrator", FakeOrch)
    monkeypatch.setattr(W, "on_run_start", lambda *a: hooks.append("start"))
    monkeypatch.setattr(W, "on_run_end", lambda *a: hooks.append("end"))
    iid = st.queue_add("T", "B", "https://github.com/bigdan88/app",
                       issue_url="https://github.com/bigdan88/app/issues/5", chain=0)
    item = await rq.run_item(cfg, st, st.queue_item(iid))
    assert seen["issue_url"] == "https://github.com/bigdan88/app/issues/5" and "issue_file" not in seen
    assert hooks == ["start", "end"] and item["status"] == "done"
