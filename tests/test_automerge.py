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
        self.merged, self.calls = [], []

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
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})


def _orch(tmp_path, monkeypatch, judge_answer=None, protected=()):
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    raw = base_raw(tmp_path / "w")
    raw["auto_merge"] = {"enabled": True, "max_lines": 400}
    cfg = parse_config(raw)
    store = Store(cfg.db_path)
    store.create_run("r1", state="COMPLETED", repair_cycle=0)
    store.record_test_run("r1", label="cycle0", cycle=0, exit_code=0, passed=10, failed=0, errors=0, skipped=0)
    o = Orchestrator.__new__(Orchestrator)
    o.cfg, o.store, o.run_id, o.echo = cfg, store, "r1", None
    o.issue = SimpleNamespace(title="Add export", body="Please add CSV export.")
    o.ws = SimpleNamespace(diff=lambda: "diff --git a/x b/x\n+print('hi')\n")
    o.protected_paths = list(protected)
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
    assert not gh2.merged and _last(store2, am.HELD)["judged"]


async def test_protected_files_and_conflicts_wait_for_you(tmp_path, monkeypatch):
    o, store, seen = _orch(tmp_path, monkeypatch, protected=["src/swarmforge/orchestrator.py"])
    gh_fake = FakeGitHub(additions=5, files=["src/swarmforge/orchestrator.py"])
    await o._auto_merge(_gh(gh_fake), "o/r", "https://github.com/o/r/pull/7")
    assert not gh_fake.merged and not seen and "protected" in _last(store, am.HELD)["reason"]

    o2, store2, _ = _orch(tmp_path / "b", monkeypatch)
    gh2 = FakeGitHub(additions=5, files=["a.py"], state="dirty")
    await o2._auto_merge(_gh(gh2), "o/r", "https://github.com/o/r/pull/7")
    assert not gh2.merged and "conflicts" in _last(store2, am.HELD)["reason"]


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


def test_pull_requests_page_lists_decisions_and_reverts(tmp_path, monkeypatch):
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
    store.record_intervention("r1", am.MERGED, json.dumps({"repo": "o/r", "number": 7, "title": "Add export",
                              "url": "https://github.com/o/r/pull/7", "reason": "small change (50 lines)"}))
    store.record_intervention("r1", am.HELD, json.dumps({"repo": "o/r", "number": 9, "title": "Rework queue",
                              "url": "https://github.com/o/r/pull/9", "reason": "touches protected file(s) x"}))
    monkeypatch.setattr(prs, "list_prs", lambda gh, repo, store=None, details=True: [])
    reverted = []
    monkeypatch.setattr(am, "revert", lambda gh, repo, n, method="squash": reverted.append(n) or "https://github.com/o/r/pull/10")
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"),
                           default_timeout=60)
    at.run()
    at.button(key="nav_pull-requests").click().run()
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    assert "✅ merged" in md and "small change (50 lines)" in md and "⏸ waiting for you" in md and "protected" in md
    rev = [b for b in at.button if (b.key or "").startswith("am_rev_") and not b.key.endswith("_yes")]
    assert len(rev) == 1                                              # only the merged one can be reverted
    rev[0].click().run()
    next(b for b in at.button if (b.key or "").endswith("_yes")).click().run()
    assert reverted == [7] and any(i["kind"] == "auto_merge_reverted" for i in store.interventions("r1"))
    assert "↩️ reverted" in " ".join(m.value for m in at.markdown)
