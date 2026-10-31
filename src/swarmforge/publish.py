"""Publish an already-approved run (used when credentials were missing during the run)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .config import Config
from .db import Store
from .github import GitHubClient, push_branch
from .report import build_pr_body, write_report


def _was_approved(store: Store, run_id: str) -> bool:
    return any(e["kind"] == "state" and (e["message"].split(" -> ")[-1].startswith("APPROVED"))
               for e in store.events(run_id))


async def publish_run(cfg: Config, store: Store, run_id: str, approve) -> int:
    run = store.get_run(run_id)
    if not run:
        print(f"run not found: {run_id}")
        return 2
    run_id = run["id"]
    if run["pr_url"]:
        print(f"already published: {run['pr_url']}")
        return 0
    if run["state"] != "COMPLETED" or not _was_approved(store, run_id):
        print(f"refusing: run {run_id} was not approved by the gates (state {run['state']})")
        return 1
    if not run["repo"] or "/" not in run["repo"] or Path(run["repo"]).exists():
        print("refusing: this run's repository is not a GitHub repository")
        return 1
    owner, repo = run["repo"].split("/", 1)
    gh = GitHubClient(cfg.github)
    if not gh.token or not gh.can_push(owner, repo):
        msg = f"no push access to {owner}/{repo}; set {cfg.github.token_env} or run `gh auth login`"
        print(msg)
        store.event(run_id, "pr", msg, level="warn")
        return 1
    q = (f"Push branch '{run['integration_branch']}' to {owner}/{repo} and open a DRAFT PR "
         f"against '{run['base_branch']}'?")
    ok = bool(approve(q))  # in the dashboard, clicking the Publish button is the approval
    store.record_intervention(run_id, "push_approval", f"{'approved' if ok else 'declined'} (manual publish): {q}")
    if not ok:
        print("declined")
        return 1
    repo_dir = Path(run["run_dir"]) / "repo"
    await asyncio.to_thread(push_branch, repo_dir, owner, repo, run["integration_branch"], gh.token,
                            run["base_branch"], cfg.github.branch_prefix)
    issue_title = run["issue_title"] or "SwarmForge change"
    url = gh.create_draft_pr(owner, repo, run["integration_branch"], run["base_branch"],
                             f"[SwarmForge] {issue_title}" + (f" (#{run['issue_number']})" if run["issue_number"] else ""),
                             build_pr_body(store, run_id))
    store.update_run(run_id, pr_url=url, state_reason=f"draft PR created: {url}")
    store.event(run_id, "pr", f"draft pull request (manual publish): {url}")
    write_report(store, run_id, Path(run["run_dir"]))
    print(f"draft PR: {url}")
    return 0
