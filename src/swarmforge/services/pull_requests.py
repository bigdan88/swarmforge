"""Open pull requests for the React dashboard, with reviewed-commit merge protection.

Listing is cached briefly because GitHub computes mergeability lazily and rate-limits. Every
mutation names the head commit the user reviewed: if the branch moved since, it is refused, so a
merge never publishes code the user has not seen in this interface.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

CACHE_SECONDS = 60
METHODS = ("squash", "merge", "rebase")


class GitHubApi:
    """Thin default adapter over swarmforge.prs; tests and the demo substitute a fake."""
    def __init__(self, config):
        from ..github import GitHubClient
        self.gh = GitHubClient(config.github)

    @property
    def has_token(self) -> bool:
        return bool(self.gh.token)

    def list(self, repo, store):
        from .. import prs
        return prs.list_prs(self.gh, repo, store)

    def merge(self, pr, method, delete_branch):
        from .. import prs
        return prs.merge(self.gh, pr, method, delete_branch)

    def update(self, pr):
        from .. import prs
        prs.update_branch(self.gh, pr)


def public(pr, store) -> dict:
    data = {k: v for k, v in asdict(pr).items() if k not in ("extra", "node_id")}
    icon, text = pr.label
    data.update(ready=pr.ready, status=text, work_item=None)
    if pr.run_id:
        row = store._one("SELECT id FROM queue_items WHERE run_id=? ORDER BY id DESC LIMIT 1", (pr.run_id,))
        data["work_item"] = f"task-{row['id']}" if row else f"run-{pr.run_id}"
    return data


class PullRequests:
    def __init__(self, store, config, api=None):
        self.store, self.config = store, config
        self._api = api
        self._cache: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @property
    def api(self):
        if self._api is None:
            self._api = GitHubApi(self.config)
        return self._api

    def repos(self) -> list[str]:
        from ..prs import known_repos
        return known_repos(self.config, self.store)

    def listing(self, refresh: bool = False) -> dict:
        if self.config is None:
            return {"configured": False, "token": False, "repos": [], "items": [], "errors": {}, "fetched_at": None}
        with self._lock:
            if not refresh and self._cache and time.time() - self._cache["fetched_at"] < CACHE_SECONDS:
                return self._cache["view"]
            repos, items, errors, raw = self.repos(), [], {}, {}
            for repo in repos[:20]:
                try:
                    found = self.api.list(repo, self.store)
                except Exception as exc:  # noqa: BLE001 - one repository must not hide the others
                    errors[repo] = str(exc)[:300]
                    continue
                for pr in found[:50]:
                    raw[(pr.repo, pr.number)] = pr
                    items.append(public(pr, self.store))
            view = {"configured": True, "token": self.api.has_token, "repos": repos[:20], "items": items, "errors": errors,
                    "fetched_at": time.time()}
            self._cache = {"fetched_at": view["fetched_at"], "view": view, "raw": raw}
            return view

    def _reviewed(self, target: str, expected_sha: Any):
        repo, _, number = target.rpartition("#")
        if not repo or not number.isdigit():
            raise ValueError("Choose a pull request")
        if not isinstance(expected_sha, str) or len(expected_sha) < 7:
            raise ValueError("Reload pull requests and review the current changes first")
        fresh = {p.number: p for p in self.api.list(repo, self.store)}
        pr = fresh.get(int(number))
        if pr is None:
            raise ValueError("This pull request is no longer open")
        if pr.head_sha != expected_sha:
            raise ValueError("New commits were pushed after you reviewed this pull request. Reload and review them first.")
        return pr

    def merge(self, target, args) -> dict:
        method = args.get("method", "squash")
        if method not in METHODS:
            raise ValueError("Choose squash, merge or rebase")
        if args.get("reviewed") is not True:
            raise ValueError("Confirm that you reviewed these changes before merging")
        if not self.api.has_token:
            raise ValueError("Add a GitHub token (GITHUB_TOKEN in .env, or `gh auth login`) with pull request write access")
        pr = self._reviewed(target, args.get("expected_sha"))
        if not pr.ready:
            raise ValueError(f"This pull request is not ready to merge ({pr.label[1]})")
        self.api.merge(pr, method, args.get("delete_branch", True) is True)
        self._cache = None
        return {"message": f"Merged {pr.repo} #{pr.number}"}

    def update(self, target, args) -> dict:
        if not self.api.has_token:
            raise ValueError("Add a GitHub token with pull request write access")
        pr = self._reviewed(target, args.get("expected_sha"))
        if pr.state != "behind":
            raise ValueError("Only branches that are behind and free of conflicts can be updated here")
        self.api.update(pr)
        self._cache = None
        return {"message": f"Updated the branch of {pr.repo} #{pr.number}"}
