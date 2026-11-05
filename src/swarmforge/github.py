"""GitHub integration: issue retrieval, safe branch publishing, draft pull requests."""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import GitHubConfig
from .workspace import git, git_rc

ISSUE_URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?(?:[#?].*)?$")
PROTECTED_NAMES = {"main", "master", "develop", "trunk", "release", "production"}


class GitHubError(RuntimeError):
    pass


@dataclass
class Issue:
    title: str
    body: str
    number: int | None = None
    owner: str | None = None
    repo: str | None = None
    url: str | None = None
    labels: list[str] | None = None

    @property
    def full_repo(self) -> str | None:
        return f"{self.owner}/{self.repo}" if self.owner else None

    @property
    def clone_url(self) -> str | None:
        return f"https://github.com/{self.owner}/{self.repo}.git" if self.owner else None


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = ISSUE_URL_RE.match(url.strip())
    if not m:
        raise GitHubError(f"not a GitHub issue URL: {url!r} (expected https://github.com/OWNER/REPO/issues/N)")
    return m.group(1), m.group(2).removesuffix(".git"), int(m.group(3))


def get_token(cfg: GitHubConfig) -> str | None:
    tok = os.environ.get(cfg.token_env)
    if tok:
        return tok
    if shutil.which("gh"):
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return None


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "swarmforge"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GitHubClient:
    def __init__(self, cfg: GitHubConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.token = get_token(cfg)
        self._client = httpx.Client(base_url=cfg.api_url, headers=_headers(self.token), timeout=30,
                                    transport=transport)

    def fetch_issue(self, url: str) -> Issue:
        owner, repo, num = parse_issue_url(url)
        r = self._client.get(f"/repos/{owner}/{repo}/issues/{num}")
        if r.status_code in (401, 403) and self.token:
            # stale/insufficient token: public issues are still readable anonymously
            req = self._client.build_request("GET", f"/repos/{owner}/{repo}/issues/{num}")
            req.headers.pop("Authorization", None)
            r2 = self._client.send(req)
            if r2.status_code == 200 or r.status_code == 401:
                r = r2
        if r.status_code == 404:
            raise GitHubError(f"issue not found or not accessible: {url}"
                              + ("" if self.token else " (no GitHub token set; private repos need one)"))
        r.raise_for_status()
        d = r.json()
        if "pull_request" in d:
            raise GitHubError(f"{url} is a pull request, not an issue")
        return Issue(title=d["title"], body=d.get("body") or "", number=num, owner=owner, repo=repo,
                     url=d.get("html_url", url), labels=[l["name"] for l in d.get("labels", [])])

    def repo_info(self, owner: str, repo: str) -> dict:
        r = self._client.get(f"/repos/{owner}/{repo}")
        r.raise_for_status()
        return r.json()

    def can_push(self, owner: str, repo: str) -> bool:
        if not self.token:
            return False
        try:
            return bool(self.repo_info(owner, repo).get("permissions", {}).get("push"))
        except httpx.HTTPError:
            return False

    # ------------------------------------------------ issue labels and comments (`swarmforge watch`)
    def labeled_issues(self, owner: str, repo: str, label: str) -> list[Issue]:
        """Open issues (not pull requests) carrying `label`."""
        r = self._client.get(f"/repos/{owner}/{repo}/issues",
                             params={"labels": label, "state": "open", "per_page": 50})
        if r.status_code >= 400:
            raise GitHubError(f"listing issues of {owner}/{repo} failed: HTTP {r.status_code} {r.text[:300]}")
        out = []
        for d in r.json():
            if "pull_request" in d:
                continue
            out.append(Issue(title=d["title"], body=d.get("body") or "", number=d["number"], owner=owner,
                             repo=repo, url=d.get("html_url"), labels=[l["name"] for l in d.get("labels", [])]))
        return out

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        r = self._client.post(f"/repos/{owner}/{repo}/issues/{number}/labels", json={"labels": labels})
        if r.status_code >= 400:
            raise GitHubError(f"adding labels failed: HTTP {r.status_code} {r.text[:300]}")

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        from urllib.parse import quote
        r = self._client.delete(f"/repos/{owner}/{repo}/issues/{number}/labels/{quote(label, safe='')}")
        if r.status_code >= 400 and r.status_code != 404:
            raise GitHubError(f"removing label failed: HTTP {r.status_code} {r.text[:300]}")

    def comment(self, owner: str, repo: str, number: int, body: str) -> str:
        r = self._client.post(f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body[:60_000]})
        if r.status_code >= 400:
            raise GitHubError(f"commenting failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json().get("html_url", "")

    def create_draft_pr(self, owner: str, repo: str, head: str, base: str, title: str, body: str) -> str:
        if not self.token:
            raise GitHubError(f"no GitHub token ({self.cfg.token_env}) available")
        r = self._client.post(f"/repos/{owner}/{repo}/pulls", json={
            "title": title, "head": head, "base": base, "body": body, "draft": True})
        if r.status_code >= 400:
            raise GitHubError(f"creating draft PR failed: HTTP {r.status_code} {r.text[:500]}")
        return r.json()["html_url"]

    def upload_pr_asset(self, owner: str, repo: str, branch: str, path: str | Path,
                        dest_dir: str = ".swarmforge/evidence") -> str:
        """Commit a file (e.g. a screenshot PNG) to a branch via the Contents API.

        Returns a raw.githubusercontent.com URL for the committed file.
        """
        if not self.token:
            raise GitHubError(f"no GitHub token ({self.cfg.token_env}) available")
        path = Path(path)
        api_path = quote(f"{dest_dir}/{path.name}", safe="/")
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        sha: str | None = None
        r = self._client.get(
            f"/repos/{owner}/{repo}/contents/{api_path}", params={"ref": branch})
        if r.status_code == 200:
            sha = r.json().get("sha")
        payload = {
            "message": f"Add visual evidence: {path.name}",
            "content": content,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        r = self._client.put(f"/repos/{owner}/{repo}/contents/{api_path}", json=payload)
        if r.status_code >= 400:
            raise GitHubError(f"uploading PR asset failed: HTTP {r.status_code} {r.text[:300]}")
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{api_path}"

    def get_pr_body(self, owner: str, repo: str, pr_number: int) -> str:
        """Return the body of an existing pull request."""
        r = self._client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        if r.status_code >= 400:
            raise GitHubError(f"fetching PR body failed: HTTP {r.status_code} {r.text[:300]}")
        return str(r.json().get("body") or "")

    def update_pr_body(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Replace the body of an existing pull request."""
        if not self.token:
            raise GitHubError(f"no GitHub token ({self.cfg.token_env}) available")
        r = self._client.patch(f"/repos/{owner}/{repo}/pulls/{pr_number}", json={"body": body})
        if r.status_code >= 400:
            raise GitHubError(f"updating PR body failed: HTTP {r.status_code} {r.text[:300]}")


def pr_number_from_url(url: str) -> int | None:
    """Extract the PR number from a .../pull/N URL; None when it is not one."""
    m = re.search(r"/pull/(\d+)", str(url))
    return int(m.group(1)) if m else None


def check_branch_safe(branch: str, default_branch: str, prefix: str) -> None:
    if not branch.startswith(prefix):
        raise GitHubError(f"refusing to push '{branch}': branch must start with '{prefix}'")
    if branch == default_branch or branch.split("/")[-1] in PROTECTED_NAMES or branch in PROTECTED_NAMES:
        raise GitHubError(f"refusing to push to protected branch name '{branch}'")


def push_branch(repo_dir: Path, owner: str, repo: str, branch: str, token: str,
                default_branch: str, prefix: str) -> None:
    """Push a new branch. Never force-pushes; refuses if the remote branch already exists."""
    check_branch_safe(branch, default_branch, prefix)
    url = f"https://github.com/{owner}/{repo}.git"
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    hdr = f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}"
    rc, out = git_rc(repo_dir, "-c", hdr, "ls-remote", "--heads", url, branch)
    if rc != 0:
        raise GitHubError(f"cannot reach remote: {_redact(out, token, auth)}")
    if out.strip():
        raise GitHubError(f"remote branch '{branch}' already exists; refusing to overwrite it")
    rc, out = git_rc(repo_dir, "-c", hdr, "push", url, f"refs/heads/{branch}:refs/heads/{branch}")
    if rc != 0:
        raise GitHubError(f"git push failed: {_redact(out, token, auth)}")


def _redact(s: str, *secrets: str) -> str:
    for x in secrets:
        if x:
            s = s.replace(x, "***")
    return s


def load_issue_file(path: Path) -> Issue:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^\s*#\s+(.+?)\s*\n", text)
    title = m.group(1) if m else path.stem
    body = text[m.end():] if m else text
    return Issue(title=title, body=body.strip(), url=str(path.resolve()))
