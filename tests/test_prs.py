"""Pull requests page logic: list, mergeability, drafts marked ready, merge, merge-all ordering."""
import json

import httpx
import pytest

from swarmforge import prs as P
from swarmforge.db import Store
from swarmforge.github import GitHubClient, GitHubError


class FakeGitHub:
    def __init__(self, prs):
        self.prs = {p["number"]: p for p in prs}
        self.calls = []

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        path = req.url.path
        if req.method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=[self._summary(p) for p in self.prs.values() if p["open"]])
        if req.method == "GET" and "/pulls/" in path:
            p = self.prs[int(path.rsplit("/", 1)[1])]
            return httpx.Response(200, json={**self._summary(p), "mergeable": p["mergeable"],
                                             "mergeable_state": p["state"], "additions": 3, "deletions": 1,
                                             "changed_files": 2})
        if path == "/graphql":
            p = next(p for p in self.prs.values() if p["node"] == body["variables"]["id"])
            p["draft"], p["state"] = False, "clean"
            return httpx.Response(200, json={"data": {}})
        if req.method == "PUT" and path.endswith("/merge"):
            n = int(path.split("/")[-2])
            p = self.prs[n]
            if p["draft"]:
                return httpx.Response(405, json={"message": "Pull Request is still a draft"})
            p["open"] = False
            for q in self.prs.values():          # merging one PR makes the overlapping one conflict
                if q["open"] and q.get("conflicts_after") == n:
                    q["state"], q["mergeable"] = "dirty", False
            return httpx.Response(200, json={"sha": "abc", "merged": True})
        if req.method == "PUT" and path.endswith("/update-branch"):
            self.prs[int(path.split("/")[-2])]["state"] = "clean"
            return httpx.Response(202, json={})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "nope"})

    @staticmethod
    def _summary(p):
        return {"number": p["number"], "title": p["title"], "html_url": f"https://github.com/o/r/pull/{p['number']}",
                "head": {"ref": p["head"], "sha": f"sha{p['number']}"}, "base": {"ref": "main"},
                "draft": p["draft"], "user": {"login": "bigdan88"}, "created_at": "2026-11-01T01:00:00Z",
                "node_id": p["node"]}


def pr(n, state="clean", draft=False, **kw):
    return {"number": n, "title": f"Task {n}", "head": f"swarmforge/task-{n}", "draft": draft, "node": f"N{n}",
            "state": state, "mergeable": state not in ("dirty",), "open": True, **kw}


@pytest.fixture
def gh(cfg, monkeypatch):
    monkeypatch.setenv(cfg.github.token_env, "tok")
    fake = FakeGitHub([pr(2), pr(3, "draft", draft=True), pr(4, "behind"), pr(5, conflicts_after=2), pr(6, "dirty")])
    return GitHubClient(cfg.github, transport=httpx.MockTransport(fake)), fake


def test_list_links_runs_and_reads_mergeability(cfg, tmp_path, gh):
    client, _ = gh
    st = Store(tmp_path / "db")
    st.create_run("r3", integration_branch="swarmforge/task-3")
    prs = P.list_prs(client, "o/r", st)
    by = {p.number: p for p in prs}
    assert by[3].run_id == "r3" and by[3].draft and by[3].ready         # a mergeable draft can be merged
    assert by[2].ready and not by[6].ready and by[6].label[1] == "Conflicts with main"
    assert by[4].label[1].startswith("Behind main")


def test_merge_marks_drafts_ready_and_deletes_the_branch(gh):
    client, fake = gh
    p = P.refresh(client, P.PR("o/r", 3, "Task 3", "", "swarmforge/task-3", "main", True, "", "", "N3", "sha3"))
    P.merge(client, p, "squash")
    methods = [(m, path) for m, path, _ in fake.calls]
    assert ("POST", "/graphql") in methods and ("PUT", "/repos/o/r/pulls/3/merge") in methods
    assert ("DELETE", "/repos/o/r/git/refs/heads/swarmforge/task-3") in methods
    merge_body = next(b for m, path, b in fake.calls if path.endswith("/merge"))
    assert merge_body["merge_method"] == "squash" and merge_body["commit_title"] == "Task 3 (#3)"


def test_merge_all_goes_oldest_first_updates_behind_and_skips_new_conflicts(gh):
    client, fake = gh
    prs = [p for p in P.list_prs(client, "o/r") if p.ready or p.state == "behind"]
    res = {p.number: (ok, msg) for p, ok, msg in P.merge_all(client, prs, sleep=lambda s: None)}
    assert res[2] == (True, "merged") and res[3][0] and res[4][0]          # behind -> updated -> merged
    assert res[5] == (False, "conflicts with main now; skipped")           # conflicted after #2 merged
    order = [path for m, path, _ in fake.calls if m == "PUT" and path.endswith("/merge")]
    assert order == ["/repos/o/r/pulls/2/merge", "/repos/o/r/pulls/3/merge", "/repos/o/r/pulls/4/merge"]


def test_merge_without_token_is_refused(cfg, monkeypatch):
    monkeypatch.delenv(cfg.github.token_env, raising=False)
    monkeypatch.setattr("swarmforge.github.shutil.which", lambda n: None)
    client = GitHubClient(cfg.github, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(GitHubError, match="no GitHub token"):
        P.merge(client, P.PR("o/r", 1, "t", "", "b", "main", False, "", "", "N", "s"))


def test_known_repos_collects_watched_runs_and_recent(cfg, tmp_path):
    st = Store(tmp_path / "db")
    cfg.watch.repos = ["bigdan88/swarmforge"]
    st.create_run("a", pr_url="https://github.com/bigdan88/commander_forge/pull/1")
    st.remember_repo("https://github.com/BigDan88/SwarmForge.git")
    st.remember_repo("C:/Users/danie/swarmforge-sample")
    assert P.known_repos(cfg, st) == ["bigdan88/swarmforge", "bigdan88/commander_forge"]
