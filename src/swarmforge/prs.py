"""Pull requests: see what the swarm opened and merge it from the dashboard.

Works on GitHub repositories SwarmForge knows about (watched repos, recently used repos, and every repo
a run opened a PR on). Draft PRs are marked ready for review first (GitHub can't merge drafts), then
merged with the method you pick. "Merge all" goes oldest first and re-checks each PR after the previous
merge, skipping any that now conflict.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .github import GitHubClient, GitHubError

READY = {"clean", "unstable", "has_hooks"}          # mergeable_state values GitHub will merge
LABELS = {
    "clean": ("✅", "Ready to merge"),
    "unstable": ("✅", "Ready (some checks failing)"),
    "has_hooks": ("✅", "Ready"),
    "draft": ("📝", "Draft — will be marked ready, then merged"),
    "behind": ("↻", "Behind main — update the branch first"),
    "dirty": ("⚠️", "Conflicts with main"),
    "blocked": ("⛔", "Blocked by branch protection / reviews"),
    "unknown": ("⏳", "GitHub is still checking"),
}


@dataclass
class PR:
    repo: str
    number: int
    title: str
    url: str
    head: str
    base: str
    draft: bool
    author: str
    created_at: str
    node_id: str
    head_sha: str
    state: str = "unknown"            # GitHub mergeable_state
    mergeable: bool | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    run_id: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state in READY or (self.draft and self.mergeable is True)

    @property
    def label(self) -> tuple[str, str]:
        if self.draft and self.mergeable is True:
            return LABELS["draft"]
        return LABELS.get(self.state, ("❔", self.state))


def known_repos(cfg, store) -> list[str]:
    """owner/name of every GitHub repository SwarmForge has worked on or watches."""
    out: list[str] = []

    def add(spec: str | None) -> None:
        if not spec:
            return
        m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", spec) or re.fullmatch(r"([\w.-]+)/([\w.-]+)", spec.strip())
        if m:
            name = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
            if name.lower() not in {x.lower() for x in out}:
                out.append(name)
    for spec in getattr(cfg.watch, "repos", []) or []:
        add(spec)
    for r in store.list_runs(300):
        add(r.get("pr_url"))
    for spec in store.recent_repos():
        add(spec)
    return out


def list_prs(gh: GitHubClient, repo: str, store=None, details: bool = True) -> list[PR]:
    r = gh._client.get(f"/repos/{repo}/pulls", params={"state": "open", "per_page": 50, "sort": "created",
                                                       "direction": "asc"})
    if r.status_code == 404:
        raise GitHubError(f"{repo}: not found or no access")
    if r.status_code >= 400:
        raise GitHubError(f"{repo}: HTTP {r.status_code} {r.text[:200]}")
    runs = {}
    if store is not None:
        for run in store.list_runs(500):
            if run.get("integration_branch"):
                runs[run["integration_branch"]] = run["id"]
    prs = []
    for d in r.json():
        pr = PR(repo=repo, number=d["number"], title=d["title"], url=d["html_url"], head=d["head"]["ref"],
                base=d["base"]["ref"], draft=bool(d.get("draft")), author=(d.get("user") or {}).get("login", ""),
                created_at=d.get("created_at", ""), node_id=d.get("node_id", ""), head_sha=d["head"]["sha"],
                run_id=runs.get(d["head"]["ref"]))
        if details:
            refresh(gh, pr)
        prs.append(pr)
    return prs


def refresh(gh: GitHubClient, pr: PR, tries: int = 1, sleep=time.sleep) -> PR:
    """Load mergeability (GitHub computes it lazily, so it may take a moment)."""
    for i in range(max(1, tries)):
        r = gh._client.get(f"/repos/{pr.repo}/pulls/{pr.number}")
        if r.status_code >= 400:
            raise GitHubError(f"#{pr.number}: HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        pr.mergeable, pr.state = d.get("mergeable"), d.get("mergeable_state") or "unknown"
        pr.draft, pr.head_sha = bool(d.get("draft")), d["head"]["sha"]
        pr.additions, pr.deletions, pr.changed_files = d.get("additions", 0), d.get("deletions", 0), \
            d.get("changed_files", 0)
        if pr.mergeable is not None or i == tries - 1:
            break
        sleep(2)
    return pr


def mark_ready(gh: GitHubClient, pr: PR) -> None:
    base = gh.cfg.api_url.rstrip("/")
    url = (base[:-3] if base.endswith("/v3") else base) + "/graphql"
    q = "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
    r = gh._client.post(url, json={"query": q, "variables": {"id": pr.node_id}})
    if r.status_code >= 400 or (r.json() or {}).get("errors"):
        raise GitHubError(f"#{pr.number}: could not mark it ready for review: {r.text[:300]}")
    pr.draft = False


def merge(gh: GitHubClient, pr: PR, method: str = "squash", delete_branch: bool = True,
          branch_prefix: str = "swarmforge/") -> str:
    if not gh.token:
        raise GitHubError("no GitHub token: add GITHUB_TOKEN to .env or run `gh auth login`")
    if pr.draft:
        mark_ready(gh, pr)
    body = {"merge_method": method, "sha": pr.head_sha}
    if method == "squash":
        body["commit_title"] = f"{pr.title} (#{pr.number})"
    r = gh._client.put(f"/repos/{pr.repo}/pulls/{pr.number}/merge", json=body)
    if r.status_code >= 400:
        msg = (r.json() or {}).get("message", r.text[:300]) if r.headers.get("content-type", "").startswith(
            "application/json") else r.text[:300]
        raise GitHubError(f"#{pr.number} not merged: {msg}")
    if delete_branch and pr.head.startswith(branch_prefix):
        gh._client.delete(f"/repos/{pr.repo}/git/refs/heads/{pr.head}")   # best effort
    return (r.json() or {}).get("sha", "")


def update_branch(gh: GitHubClient, pr: PR) -> None:
    """GitHub's 'Update branch': merge the base into the PR branch (only works without conflicts)."""
    r = gh._client.put(f"/repos/{pr.repo}/pulls/{pr.number}/update-branch", json={"expected_head_sha": pr.head_sha})
    if r.status_code >= 400:
        raise GitHubError(f"#{pr.number}: could not update the branch: {r.text[:300]}")


def merge_all(gh: GitHubClient, prs: list[PR], method: str = "squash", delete_branch: bool = True,
              sleep=time.sleep) -> list[tuple[PR, bool, str]]:
    """Merge oldest first; re-check each PR right before merging (the previous merge may have changed it)."""
    results = []
    for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
        try:
            refresh(gh, pr, tries=5, sleep=sleep)
            if pr.state == "dirty" or pr.mergeable is False:
                results.append((pr, False, "conflicts with main now; skipped"))
                continue
            if pr.state == "behind":
                update_branch(gh, pr)
                sleep(3)
                refresh(gh, pr, tries=5, sleep=sleep)
            merge(gh, pr, method, delete_branch)
            results.append((pr, True, "merged"))
            sleep(1)
        except GitHubError as e:
            results.append((pr, False, str(e)))
    return results
