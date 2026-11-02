"""Auto-merge: fixed rules for small changes, a judge model for larger ones, never protected files."""
import json
from types import SimpleNamespace

import httpx
import pytest

from swarmforge import automerge as am
from swarmforge.config import GitHubConfig, parse_config
from swarmforge.db import Store
from swarmforge.github import GitHubClient
from swarmforge.llm import Budget, LLMClient
from swarmforge.orchestrator import Orchestrator

from .conftest import FakeProvider, base_raw

OK_RUN = {"state": "COMPLETED"}


def _d(**kw):
    args = dict(run=OK_RUN, tests_ok=True, open_findings=[], files=["src/app/ui.py"], lines=120,
                protected=["src/swarmforge/orchestrator.py", "src/swarmforge/agent/*"], max_lines=400,
                judge_max_lines=3000)
    args.update(kw)
    return am.rule_decision(**args)


def test_rules():
    assert _d().merge and "small change (120 lines)" in _d().reason
    assert not _d(run={"state": "FAILED"}).merge
    assert not _d(tests_ok=False).merge
    held = _d(open_findings=[{"severity": "major", "title": "Race in add_message"}])
    assert not held.merge and "Race in add_message" in held.reason
    assert _d(open_findings=[{"severity": "minor", "title": "naming"}]).merge          # minors don't hold
    p = _d(files=["src/app/ui.py", "src/swarmforge/agent/tools.py"])
    assert not p.merge and "src/swarmforge/agent/tools.py" in p.reason                # protected, any size
    assert _d(lines=900) is None                                                       # large: ask the judge
    assert not _d(lines=5000).merge                                                    # huge: always you


def test_judgement_parsing_defaults_to_hold():
    assert am.parse_judgement('{"decision": "merge", "risk": "low", "reason": "tidy"}') == (True, "low", "tidy")
    assert am.parse_judgement('```json\n{"decision": "MERGE", "risk": "medium", "reason": "ok"}\n```')[0]
    assert not am.parse_judgement('{"decision": "merge", "risk": "high", "reason": "x"}')[0]  # high risk: hold
    assert not am.parse_judgement('{"decision": "hold", "risk": "low"}')[0]
    assert not am.parse_judgement("looks fine to me!")[0]


def test_repo_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "w")
    assert not am.repo_enabled(parse_config(raw), "a/b")                              # off by default
    raw["auto_merge"] = {"enabled": True, "repos": ["bigdan88/SwarmForge"]}
    cfg = parse_config(raw)
    assert am.repo_enabled(cfg, "bigdan88/swarmforge") and not am.repo_enabled(cfg, "bigdan88/other")


# --------------------------------------------------------------------- full path against a fake GitHub
class FakeGitHub:
    def __init__(self, additions, files, state="clean"):
        self.additions, self.files, self.state = additions, files, state
        self.merged, self.calls, self.closed = [], [], []

    def __call__(self, request: httpx.Request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        pr = {"number": 7, "title": "Add export", "html_url": "https://github.com/o/r/pull/7", "draft": True,
              "head": {"ref": "swarmforge/task-x", "sha": "abc"}, "base": {"ref": "main"}, "user": {"login": "o"},
              "node_id": "PR_7", "mergeable": True, "mergeable_state": self.state,
              "additions": self.additions, "deletions": 0, "changed_files": len(self.files)}
        if path == "/repos/o/r/pulls/7" and method == "GET":
            return httpx.Response(200, json=pr)
        if path == "/repos/o/r/pulls/7/files":
            return httpx.Response(200, json=[{"filename": f} for f in self.files])
        if path == "/graphql":
            return httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}})
        if path == "/repos/o/r/pulls/7/merge":
            self.merged.append(json.loads(request.content))
            return httpx.Response(200, json={"sha": "m1", "merged": True})
        if method == "DELETE":
            return httpx.Response(204)
        if method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={})
        if method == "PATCH" and path.startswith("/repos/o/r/pulls/"):
            self.closed.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"state": "closed"})
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})


def _orch(tmp_path, monkeypatch, judge_answer=None, paths=(), body="Please add CSV export.", **am_cfg):
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    raw = base_raw(tmp_path / "w")
    raw["auto_merge"] = {"enabled": True, "max_lines": 400, **am_cfg}
    cfg = parse_config(raw)
    store = Store(cfg.db_path)
    store.create_run("r1", state="COMPLETED", repair_cycle=0)
    store.record_test_run("r1", label="cycle0", cycle=0, exit_code=0, passed=10, failed=0, errors=0, skipped=0)
    o = Orchestrator.__new__(Orchestrator)
    o.cfg, o.store, o.run_id, o.echo = cfg, store, "r1", None
    o.issue = SimpleNamespace(title="Add export", body=body)
    o.ws = SimpleNamespace(diff=lambda: "diff --git a/x b/x\n+print('hi')\n")
    o.protected_paths = list(paths)
    seen = []

    def judge(body):
        seen.append(body)
        return {"content": json.dumps(judge_answer or {"decision": "hold", "risk": "high", "reason": "?"})}
    fake = FakeProvider({"glm-fake": judge, "qwen-fake": judge})
    o.llm = LLMClient(cfg, store, "r1", Budget(5), transport=fake.transport)
    return o, store, seen


def _gh(fake):
    return GitHubClient(GitHubConfig(token_env="GITHUB_TOKEN"), transport=httpx.MockTransport(fake))


def _last(store, kind):
    rows = [i for i in store.interventions("r1") if i["kind"] == kind]
    return json.loads(rows[-1]["detail"]) if rows else None


async def test_small_change_merges_without_the_judge(tmp_path, monkeypatch):
    o, store, seen = _orch(tmp_path, monkeypatch)
    gh_fake = FakeGitHub(additions=50, files=["src/app/ui.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    assert gh_fake.merged and gh_fake.merged[0]["merge_method"] == "squash" and not seen
    info = _last(store, am.MERGED)
    assert info["number"] == 7 and info["sha"] == "m1" and info["node_id"] == "PR_7" and not info["judged"]
    assert "auto-merged" in store.get_run("r1")["state_reason"]


async def test_large_change_merges_only_when_the_judge_says_so(tmp_path, monkeypatch):
    o, store, seen = _orch(tmp_path, monkeypatch, judge_answer={"decision": "merge", "risk": "low",
                                                                "reason": "Focused UI change with tests."})
    gh_fake = FakeGitHub(additions=900, files=["src/app/ui.py", "tests/test_ui.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    assert gh_fake.merged and len(seen) == 1
    user = seen[0]["messages"][1]["content"]
    assert "900 lines" in user and "<diff>" in user and "exit 0: 10 passed" in user
    info = _last(store, am.MERGED)
    assert info["judged"] and info["risk"] == "low" and "Focused UI change" in info["reason"]

    o2, store2, _ = _orch(tmp_path / "b", monkeypatch)                   # judge says hold
    gh2 = FakeGitHub(additions=900, files=["src/app/ui.py"])
    await o2._auto_merge(_gh(gh2), "o/r", "https://github.com/o/r/pull/7")
    assert not gh2.merged and _last(store2, am.FIXING)["judged"]         # held -> automatic fix-up task


async def test_core_files_need_a_low_risk_verdict(tmp_path, monkeypatch):
    o, store, seen = _orch(tmp_path, monkeypatch, judge_answer={"decision": "merge", "risk": "medium", "reason": "ok"},
                           paths=["src/swarmforge/orchestrator.py"], auto_fix=False)
    gh_fake = FakeGitHub(additions=5, files=["src/swarmforge/orchestrator.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    user = seen[0]["messages"][1]["content"]
    assert "CORE FILES touched" in user and not gh_fake.merged                  # medium isn't enough for core
    assert "core files need low" in _last(store, am.HELD)["reason"]
    o2, store2, _ = _orch(tmp_path / "b", monkeypatch, judge_answer={"decision": "merge", "risk": "low", "reason": "tiny"},
                          paths=["src/swarmforge/orchestrator.py"])
    gh2 = FakeGitHub(additions=5, files=["src/swarmforge/orchestrator.py"])
    await o2._auto_merge(_gh(gh2), "o/r", "https://github.com/o/r/pull/7")
    assert gh2.merged
    o3, store3, seen3 = _orch(tmp_path / "c", monkeypatch, paths=["src/swarmforge/orchestrator.py"],
                              protected="hold")
    gh3 = FakeGitHub(additions=5, files=["src/swarmforge/orchestrator.py"])
    await o3._auto_merge(_gh(gh3), "o/r", "https://github.com/o/r/pull/7")
    assert not seen3 and "protected" in _last(store3, am.FIXING)["reason"]       # hold mode: no judge


async def test_major_findings_go_to_the_judge_with_their_details(tmp_path, monkeypatch):
    o, store, seen = _orch(tmp_path, monkeypatch, judge_answer={"decision": "merge", "risk": "low",
                                                                "reason": "the finding is cosmetic"})
    store.record_finding("r1", cycle=0, source="reviewer", severity="major", file="ui.py", line=1,
                         title="Tools section not rendered", description="never shown", recommendation="render it")
    gh_fake = FakeGitHub(additions=50, files=["ui.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    assert "Tools section not rendered" in seen[0]["messages"][1]["content"] and gh_fake.merged


async def test_held_pr_gets_one_fix_up_task_that_replaces_it(tmp_path, monkeypatch):
    o, store, _ = _orch(tmp_path, monkeypatch)                               # judge says hold
    store.queue_add("Add export", "Please add CSV export.", "o/r", queue="self-improvement")
    store.queue_update(1, status="done", run_id="r1")
    store.queue_add("Next thing", "b", "o/r", queue="self-improvement")
    store.record_finding("r1", cycle=0, source="reviewer", severity="minor", file="a.py", line=1, title="naming",
                         description="d", recommendation="rename")
    gh_fake = FakeGitHub(additions=900, files=["a.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    info = _last(store, am.FIXING)
    fix = store.queue_item(info["fix_task"])
    assert fix["title"] == "Fix up: Add export" and fix["seed_run_id"] == "r1" and fix["queue"] == "self-improvement"
    assert "why https://github.com/o/r/pull/7 was not merged" in fix["body"] and "naming" in fix["body"]
    assert am.fix_depth(fix["body"]) == (1, "https://github.com/o/r/pull/7")
    waiting = [q["title"] for q in store.queue_items() if q["status"] == "queued"]
    assert waiting[0] == "Fix up: Add export"                                # front of the line
    # the fix-up's own run: its PR closes the one it replaces; a second hold waits for you
    o2, store2, _ = _orch(tmp_path / "b", monkeypatch, body=fix["body"])
    gh2 = FakeGitHub(additions=900, files=["a.py"])
    await o2._auto_merge(_gh(gh2), "o/r", "https://github.com/o/r/pull/8")
    assert gh2.closed == [7] and _last(store2, am.CLOSED)["replaced_by"] == "https://github.com/o/r/pull/8"
    assert _last(store2, am.HELD) and not _last(store2, am.FIXING)


async def test_conflict_is_resolved_against_main_then_merged(tmp_path, monkeypatch):
    from swarmforge import orchestrator as orch_mod
    o, store, _ = _orch(tmp_path, monkeypatch)
    gh_fake = FakeGitHub(additions=5, files=["a.py"], state="dirty")
    calls = []

    async def update(cycle):
        calls.append(cycle)
        return []
    o._update_with_base = update
    o.ws = SimpleNamespace(diff=lambda: "", repo=tmp_path, integration_branch="swarmforge/task-x", base_branch="main")
    monkeypatch.setattr(orch_mod, "push_branch", lambda *a, **k: setattr(gh_fake, "state", "clean"))
    real_sleep = orch_mod.asyncio.sleep
    monkeypatch.setattr(orch_mod.asyncio, "sleep", lambda s: real_sleep(0))
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    assert calls == [0] and gh_fake.merged and _last(store, am.MERGED)

    o2, store2, _ = _orch(tmp_path / "b", monkeypatch, auto_fix=False)       # resolution fails: hold
    gh2 = FakeGitHub(additions=5, files=["a.py"], state="dirty")

    async def broken(cycle):
        return [{"summary": "could not merge the latest main into this branch"}]
    o2._update_with_base = broken
    o2.ws = o.ws
    await o2._auto_merge(_gh(gh2), "o/r", "https://github.com/o/r/pull/7")
    assert not gh2.merged and "resolving it failed" in _last(store2, am.HELD)["reason"]


async def test_disabled_does_nothing(tmp_path, monkeypatch):
    o, store, _ = _orch(tmp_path, monkeypatch)
    o.cfg.auto_merge.enabled = False
    gh_fake = FakeGitHub(additions=5, files=["a.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    assert gh_fake.calls == [] and store.interventions("r1") == []


def test_revert_creates_and_merges_a_revert_pr(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    calls = []

    def handler(request):
        p, m = request.url.path, request.method
        calls.append((m, p))
        n = 7 if p.endswith("/7") else 8
        if m == "GET" and p.startswith("/repos/o/r/pulls/"):
            return httpx.Response(200, json={"number": n, "title": "Add export" if n == 7 else 'Revert "Add export"',
                                             "html_url": f"https://github.com/o/r/pull/{n}", "draft": False,
                                             "head": {"ref": f"revert-{n}", "sha": "s"}, "base": {"ref": "main"},
                                             "user": {}, "node_id": f"PR_{n}", "mergeable": True,
                                             "mergeable_state": "clean", "additions": 1, "deletions": 1})
        if p == "/graphql":
            body = json.loads(request.content)
            assert body["variables"]["id"] == "PR_7"
            return httpx.Response(200, json={"data": {"revertPullRequest": {"revertPullRequest": {
                "number": 8, "url": "https://github.com/o/r/pull/8"}}}})
        if p == "/repos/o/r/pulls/8/merge":
            return httpx.Response(200, json={"sha": "r1"})
        return httpx.Response(404)
    gh = GitHubClient(GitHubConfig(token_env="GITHUB_TOKEN"), transport=httpx.MockTransport(handler))
    assert am.revert(gh, "o/r", 7) == "https://github.com/o/r/pull/8"
    assert ("PUT", "/repos/o/r/pulls/8/merge") in calls


@pytest.mark.parametrize("bad", [{"enabled": True, "colour": 1}])
def test_unknown_keys_are_rejected(tmp_path, monkeypatch, bad):
    from swarmforge.config import ConfigError
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "w")
    raw["auto_merge"] = bad
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_pull_requests_page_lists_decisions_with_buttons(tmp_path, monkeypatch):
    import streamlit as st_
    import yaml
    from streamlit.testing.v1 import AppTest

    from swarmforge import prs
    from pathlib import Path
    st_.cache_resource.clear()
    raw = base_raw(tmp_path / "work")
    raw["auto_merge"] = {"enabled": True, "repos": ["o/r"]}
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    for k, v in {"FAKE_KEY": "k", "GITHUB_TOKEN": "t", "SWARMFORGE_CONFIG": str(p), "SWARMFORGE_DASH_AUTOREFRESH": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    cfg = parse_config(raw)
    store = Store(cfg.db_path)
    store.create_run("r1", state="COMPLETED", pr_url="https://github.com/o/r/pull/7")
    store.create_run("r2", state="COMPLETED", pr_url="https://github.com/o/r/pull/9", issue_title="Rework queue",
                     issue_body="Make the queue faster.", repair_cycle=0)
    store.create_run("r3", state="COMPLETED", pr_url="https://github.com/o/r/pull/5")
    store.record_intervention("r1", am.MERGED, json.dumps({"repo": "o/r", "number": 7, "title": "Add export",
                              "url": "https://github.com/o/r/pull/7", "reason": "small change (50 lines)"}))
    store.record_intervention("r2", am.HELD, json.dumps({"repo": "o/r", "number": 9, "title": "Rework queue",
                              "url": "https://github.com/o/r/pull/9", "reason": "touches protected file(s) x"}))
    store.record_intervention("r3", am.HELD, json.dumps({"repo": "o/r", "number": 5, "title": "Old one",
                              "url": "https://github.com/o/r/pull/5", "reason": "x"}))
    open_pr = prs.PR(repo="o/r", number=9, title="Rework queue", url="https://github.com/o/r/pull/9", head="h",
                     base="main", draft=True, author="o", created_at="", node_id="PR_9", head_sha="s", state="clean",
                     mergeable=True)
    monkeypatch.setattr(prs, "list_prs", lambda gh, repo, store=None, details=True: [open_pr])
    from swarmforge import runqueue as rq
    started = []
    monkeypatch.setattr(rq, "start_worker", lambda c, s, keep_pause=False: started.append(1) or 4242)
    reverted = []
    monkeypatch.setattr(am, "revert", lambda gh, repo, n, method="squash": reverted.append(n) or "https://github.com/o/r/pull/10")
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"),
                           default_timeout=60)
    at.run()
    at.button(key="nav_pull-requests").click().run()
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    assert "✅ merged" in md and "⏸ waiting for you" in md and "protected" in md
    assert "no longer open" in md                                    # #5 isn't open on GitHub any more
    keys = [b.key or "" for b in at.button]
    assert sum(k.endswith("_merge") for k in keys) == 1 and sum(k.endswith("_fix") for k in keys) == 1
    # Fix with the swarm: a fix-up task continuing r2's code, replacing #9
    next(b for b in at.button if (b.key or "").endswith("_fix")).click().run()
    assert not at.exception, [e.value for e in at.exception]
    fix = [q for q in store.queue_items() if q["title"] == "Fix up: Rework queue"][0]
    assert fix["seed_run_id"] == "r2" and am.fix_depth(fix["body"]) == (1, "https://github.com/o/r/pull/9")
    assert "🔧 fix-up queued" in " ".join(m.value for m in at.markdown) and started
    # revert the merged one
    next(b for b in at.button if (b.key or "").endswith("_rev")).click().run()
    next(b for b in at.button if (b.key or "").endswith("_yes")).click().run()
    assert reverted == [7] and "↩️ reverted" in " ".join(m.value for m in at.markdown)
