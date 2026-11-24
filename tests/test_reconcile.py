"""Reconciliation: interrupted pushes, PRs and merges are checked on GitHub, with evidence kept."""
import asyncio
import json
import time

import httpx
import pytest

from swarmforge import reconcile
from swarmforge.db import Store
from swarmforge.execution import ExecutionStopped, external_once
from swarmforge.github import GitHubClient
from swarmforge.reconcile import CONFLICT, DONE, NOT_DONE, UNKNOWN, Verdict, check_external, check_request


class FakeGitHub:
    """Just enough of the GitHub REST API: branches and pull requests."""

    def __init__(self):
        self.branches: dict[str, str] = {}
        self.pulls: dict[int, dict] = {}
        self.down = False
        self.requests = []

    def pr(self, number, head, *, state="open", merged=False, head_sha="h1", merge_sha=None):
        self.pulls[number] = {"number": number, "state": state, "merged": merged, "head": {"ref": head, "sha": head_sha},
                              "merge_commit_sha": merge_sha, "html_url": f"https://github.com/o/r/pull/{number}",
                              "merged_at": "2026-11-22T12:00:00Z" if merged else None, "closed_at": None}

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        if self.down:
            return httpx.Response(502, json={"message": "Bad gateway"})
        path = request.url.path
        if path.startswith("/repos/o/r/git/ref/heads/"):
            name = path.removeprefix("/repos/o/r/git/ref/heads/")
            if name in self.branches:
                return httpx.Response(200, json={"ref": f"refs/heads/{name}", "object": {"sha": self.branches[name]}})
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/repos/o/r/pulls":
            head = request.url.params.get("head", "").partition(":")[2]
            return httpx.Response(200, json=[p for p in self.pulls.values() if p["head"]["ref"] == head])
        if path.startswith("/repos/o/r/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.pulls[number]) if number in self.pulls else httpx.Response(404, json={})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github(cfg, monkeypatch):
    fake = FakeGitHub()
    real = GitHubClient

    def client(config, transport=None):
        return real(config, transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr("swarmforge.github.GitHubClient", client)
    fake.client = client(cfg.github)
    return fake


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    s.create_run("r1", repo="o/r", issue_title="t")
    yield s
    s.close()


def test_push_verdicts_compare_the_remote_commit_with_the_one_pushed(github):
    gh = github.client
    assert check_external(gh, "push:o/r:swarmforge/t1", {"sha": "abc"}).outcome == NOT_DONE
    github.branches["swarmforge/t1"] = "abc"
    done = check_external(gh, "push:o/r:swarmforge/t1", {"sha": "abc"})
    assert done.outcome == DONE and done.evidence["remote_sha"] == "abc"
    github.branches["swarmforge/t1"] = "zzz"
    conflict = check_external(gh, "push:o/r:swarmforge/t1", {"sha": "abc"})
    assert conflict.outcome == CONFLICT and "not the pushed commit" in conflict.note
    github.down = True
    unknown = check_external(gh, "push:o/r:swarmforge/t1", {"sha": "abc"})
    assert unknown.outcome == UNKNOWN and unknown.evidence["http_status"] == 502


def test_pr_merge_and_close_verdicts(github):
    gh = github.client
    assert check_external(gh, "pr:o/r:swarmforge/t1").outcome == NOT_DONE
    github.pr(7, "swarmforge/t1", head_sha="h7")
    opened = check_external(gh, "pr:o/r:swarmforge/t1")
    assert opened.outcome == DONE and opened.result == "https://github.com/o/r/pull/7"  # the action's own result shape
    assert check_external(gh, "merge:o/r:7", {"head_sha": "h7"}).outcome == NOT_DONE
    github.pr(7, "swarmforge/t1", state="closed", merged=True, head_sha="h7", merge_sha="m7")
    merged = check_external(gh, "merge:o/r:7", {"head_sha": "h7"})
    assert merged.outcome == DONE and merged.result == "m7"
    assert check_external(gh, "merge:o/r:7", {"head_sha": "other"}).outcome == CONFLICT  # merged, but not what we reviewed
    assert check_external(gh, "close:https://github.com/o/r/pull/7").outcome == DONE
    assert check_external(gh, "local:swarmforge/t1").outcome == UNKNOWN


def _checkpoint(store, name):
    return store._one("SELECT * FROM execution_checkpoints WHERE execution_id='r1' AND name=?", ("external:" + name,))


def test_a_failed_call_is_uncertain_and_a_retry_recovers_the_real_result_without_repeating_it(cfg, store, github):
    async def timeout():
        raise httpx.ReadTimeout("no answer")
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(external_once(store, "r1", "pr:o/r:swarmforge/t1", timeout, cfg=cfg))
    assert _checkpoint(store, "pr:o/r:swarmforge/t1")["status"] == "uncertain"
    with pytest.raises(ExecutionStopped):  # without configuration nothing can be checked: stop, never repeat
        asyncio.run(external_once(store, "r1", "pr:o/r:swarmforge/t1", timeout))
    github.pr(9, "swarmforge/t1")  # GitHub did create it before the timeout

    async def must_not_run():
        pytest.fail("the PR would be opened twice")
    assert asyncio.run(external_once(store, "r1", "pr:o/r:swarmforge/t1", must_not_run, cfg=cfg)) == "https://github.com/o/r/pull/9"
    assert _checkpoint(store, "pr:o/r:swarmforge/t1")["status"] == "completed"
    trail = reconcile.evidence(store, "checkpoint", "r1|external:pr:o/r:swarmforge/t1")
    assert trail[-1]["outcome"] == DONE and trail[-1]["evidence"]["number"] == 9


def test_an_operation_that_did_not_happen_runs_again_and_a_conflict_stops(cfg, store, github):
    async def crash():
        raise ConnectionError("dropped")
    with pytest.raises(ConnectionError):
        asyncio.run(external_once(store, "r1", "push:o/r:swarmforge/t1", crash, intent={"sha": "abc"}, cfg=cfg))
    ran = []

    async def push():
        ran.append(1)
        github.branches["swarmforge/t1"] = "abc"
    asyncio.run(external_once(store, "r1", "push:o/r:swarmforge/t1", push, intent={"sha": "abc"}, cfg=cfg))
    assert ran == [1] and _checkpoint(store, "push:o/r:swarmforge/t1")["status"] == "completed"

    with pytest.raises(ConnectionError):
        asyncio.run(external_once(store, "r1", "push:o/r:swarmforge/t2", crash, intent={"sha": "abc"}, cfg=cfg))
    github.branches["swarmforge/t2"] = "someone-else"
    with pytest.raises(ExecutionStopped) as stopped:
        asyncio.run(external_once(store, "r1", "push:o/r:swarmforge/t2", push, intent={"sha": "abc"}, cfg=cfg))
    assert stopped.value.kind == "external_conflict" and len(ran) == 1
    assert _checkpoint(store, "push:o/r:swarmforge/t2")["status"] == "conflict"


def test_reconcile_all_settles_crashed_attempts_but_leaves_live_ones_and_unknowns(cfg, store, github):
    store._exec("INSERT INTO execution_checkpoints VALUES ('r1','external:merge:o/r:5','r1',?,'pending',?)",
                (time.time(), json.dumps({"intent": {"head_sha": "h5"}})))
    store._exec("INSERT INTO execution_checkpoints VALUES ('r1','external:local:swarmforge/x','r1',?,'uncertain','{}')", (time.time(),))
    github.pr(5, "swarmforge/t5", state="closed", merged=True, head_sha="h5", merge_sha="m5")
    report = reconcile.reconcile_all(store, cfg)
    assert sorted(r["outcome"] for r in report) == [DONE, UNKNOWN]
    assert _checkpoint(store, "merge:o/r:5")["status"] == "completed" and json.loads(_checkpoint(store, "merge:o/r:5")["detail"]) == "m5"
    assert _checkpoint(store, "local:swarmforge/x")["status"] == "uncertain"  # stays open for a human
    assert reconcile.evidence(store, "checkpoint", "r1|external:local:swarmforge/x")[-1]["outcome"] == UNKNOWN
    # A pending record of an attempt that is still running is never touched.
    import os
    store._exec("INSERT INTO execution_allowances(id,created_at,deadline,budget_usd,active_run,owner_pid,max_recoveries,no_progress_s,repeat_limit) "
                "VALUES ('r1',0,1e12,5,'r1',?,1,60,3)", (os.getpid(),))
    store._exec("INSERT INTO execution_checkpoints VALUES ('r1','external:pr:o/r:swarmforge/live','r1',?,'pending','{}')", (time.time(),))
    assert reconcile.reconcile_all(store, cfg) == [{"source": "task", "run_id": "r1", "operation": "local:swarmforge/x",
                                                   "outcome": UNKNOWN, "note": "local cannot be checked from GitHub; inspect it by hand"}]


def test_an_interrupted_web_merge_is_settled_from_github_on_the_next_identical_request(cfg, store):
    from swarmforge.services.dashboard import ActionConflict, DashboardActions

    from .test_web_parity import FakeApi
    api = FakeApi()

    def lost_connection(pr, method, delete_branch):
        raise httpx.RemoteProtocolError("connection dropped mid-merge")
    api.merge = lost_connection
    verdicts = []
    actions = DashboardActions(store, cfg, pr_api=api, reconcile_on_start=False,
                               reconciler=lambda op, target, args: verdicts.pop(0))
    merge = {"method": "squash", "reviewed": True, "expected_sha": "sha-ready-1"}
    with pytest.raises(ActionConflict):
        actions.execute("pr-merge-lost00001", "merge_pr", "owner/app#1", merge)
    assert store._one("SELECT status,intent FROM dashboard_requests")["status"] == "uncertain"
    verdicts.append(Verdict(UNKNOWN, note="GitHub unreachable"))
    with pytest.raises(ActionConflict):  # GitHub could not tell: still never repeated
        actions.execute("pr-merge-lost00001", "merge_pr", "owner/app#1", merge)
    verdicts.append(Verdict(DONE, "m1", {"merged": True}, "#1 was merged"))
    result = actions.execute("pr-merge-lost00001", "merge_pr", "owner/app#1", merge)
    assert result["ok"] and "confirmed on GitHub" in result["message"]
    assert actions.execute("pr-merge-lost00001", "merge_pr", "owner/app#1", merge) == result  # now a plain replay
    assert [e["outcome"] for e in reconcile.evidence(store, "request", "pr-merge-lost00001")] == [UNKNOWN, DONE]


def test_web_request_verdicts_from_github(github):
    gh = github.client
    github.pr(4, "swarmforge/t4", head_sha="reviewed")
    assert check_request(gh, "merge_pr", "o/r#4", {"expected_sha": "reviewed"}).outcome == NOT_DONE
    assert check_request(gh, "update_pr", "o/r#4", {"expected_sha": "reviewed"}).outcome == NOT_DONE
    github.pr(4, "swarmforge/t4", head_sha="after-update")
    assert check_request(gh, "update_pr", "o/r#4", {"expected_sha": "reviewed"}).outcome == DONE
    assert check_request(gh, "start_queue", "", {}).outcome == UNKNOWN


def test_cli_lists_and_reconciles(cfg, store, github, capsys, monkeypatch):
    from swarmforge import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    assert cli.cmd_reconcile(type("A", (), {"list": True})(), cfg) == 0
    assert "Nothing to reconcile" in capsys.readouterr().out
    store._exec("INSERT INTO execution_checkpoints VALUES ('r1','external:push:o/r:swarmforge/c','r1',?,'uncertain',?)",
                (time.time(), json.dumps({"intent": {"sha": "s1"}})))
    cli.cmd_reconcile(type("A", (), {"list": True})(), cfg)
    assert "push:o/r:swarmforge/c" in capsys.readouterr().out
    github.branches["swarmforge/c"] = "s1"
    cli.cmd_reconcile(type("A", (), {"list": False})(), cfg)
    assert "done" in capsys.readouterr().out and _checkpoint(store, "push:o/r:swarmforge/c")["status"] == "completed"
