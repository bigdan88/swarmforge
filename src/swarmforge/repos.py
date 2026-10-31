"""Turn whatever the user typed into a cloneable repository source, and check it before a run.

Accepted forms (all normalized):
  https://github.com/owner/repo            https://github.com/owner/repo.git
  https://github.com/owner/repo/tree/dev   (-> branch dev)    .../blob/main/x.py, .../issues/3, .../pulls
  github.com/owner/repo                    owner/repo          git@github.com:owner/repo.git
  any other https/ssh git URL              a local folder (quotes and ~ allowed)
"""
from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

GH_WEB_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/(tree|blob|commits?|issues|pulls?|actions|releases|wiki)(?:/(.*))?)?/?(?:[?#].*)?$")
GH_SSH_RE = re.compile(r"^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
SHORTHAND_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


class RepoError(ValueError):
    """A repository reference that cannot be used, with a message meant for the user."""


@dataclass
class RepoRef:
    source: str                 # what to pass to `git clone` (URL or absolute local path)
    branch: str | None = None   # branch requested via a /tree/<branch> URL
    owner: str | None = None    # GitHub owner/repo when known
    name: str | None = None
    local: bool = False
    use_auth: bool = False      # True only if the repository could not be read anonymously

    @property
    def full_name(self) -> str | None:
        return f"{self.owner}/{self.name}" if self.owner else None


def normalize_repo(text: str) -> RepoRef:
    s = (text or "").strip().strip('"').strip("'").strip()
    if not s:
        raise RepoError("no repository given")
    m = GH_WEB_RE.match(s)
    if m:
        owner, name, kind, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        branch = rest.strip("/") if kind == "tree" and rest else None
        if kind == "blob" and rest:
            branch = rest.split("/", 1)[0]
        return RepoRef(f"https://github.com/{owner}/{name}.git", branch, owner, name)
    m = GH_SSH_RE.match(s)
    if m:
        return RepoRef(f"https://github.com/{m.group(1)}/{m.group(2)}.git", None, m.group(1), m.group(2))
    if re.match(r"^(https?|ssh|git|file)://", s) or s.startswith("git@"):
        return RepoRef(s)
    p = Path(s).expanduser()
    if p.exists():
        return RepoRef(str(p.resolve()), local=True)
    m = SHORTHAND_RE.match(s)
    if m and not re.match(r"^[A-Za-z]:", s):
        return RepoRef(f"https://github.com/{m.group(1)}/{m.group(2)}.git", None, m.group(1), m.group(2))
    raise RepoError(f"repository not found: '{s}' is neither an existing folder nor a git/GitHub URL")


def auth_args(url: str, token: str | None) -> list[str]:
    """One-off git -c args that authenticate HTTPS GitHub access without storing the token."""
    if token and url.startswith("https://github.com/"):
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}"]
    return []


def _git(args: list[str], cwd: Path | None = None, timeout: int = 45) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"})
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except FileNotFoundError:
        raise RepoError("git is not installed or not on PATH")


def _redact(s: str, token: str | None) -> str:
    if token:
        s = s.replace(token, "***")
        s = s.replace(base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***")
    return s


def preflight(ref: RepoRef, token: str | None = None) -> RepoRef:
    """Check that the repository can be cloned and has a commit to branch from.
    Returns the ref with `branch` resolved/validated. Raises RepoError with guidance."""
    if ref.local:
        p = Path(ref.source)
        if not p.is_dir():
            raise RepoError(f"'{p}' is not a folder")
        rc, _ = _git(["rev-parse", "--git-dir"], cwd=p)
        if rc != 0:
            raise RepoError(f"'{p}' is not a git repository. In that folder run: git init -b main; "
                            "git add -A; git commit -m \"initial commit\" — then start the run again.")
        rc, _ = _git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=p)
        if rc != 0:
            raise RepoError(f"'{p}' has no commits yet. Commit your files first "
                            "(git add -A; git commit -m \"initial commit\").")
        if ref.branch:
            rc, _ = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{ref.branch}"], cwd=p)
            if rc != 0:
                raise RepoError(f"branch '{ref.branch}' does not exist in {p}")
        return ref

    # Anonymous first: public repositories must work even with a stale/invalid token around.
    rc, out = _git(["-c", "credential.helper=", "ls-remote", "--symref", ref.source])
    ref.use_auth = False
    if rc != 0 and token and ref.source.startswith("https://github.com/"):
        rc2, out2 = _git(["-c", "credential.helper=", *auth_args(ref.source, token),
                          "ls-remote", "--symref", ref.source])
        if rc2 == 0:
            rc, out, ref.use_auth = rc2, out2, True
        else:
            out = out2 + "\n(also failed with your GitHub token: it may be expired or lack access)"
    out = _redact(out, token)
    if rc != 0:
        hint = ""
        if ref.owner:
            hint = (f" Check the spelling of {ref.full_name}. If it is a private repository, put a GitHub "
                    "token with read access in .env as GITHUB_TOKEN (or run `gh auth login`).")
        lines = [l for l in out.splitlines() if l.strip()]
        key = [l for l in lines if l.lower().startswith(("fatal", "error", "remote:"))] or lines[-1:]
        why = "; ".join(dict.fromkeys(key[:2])) or "unknown error"
        if any("also failed with your GitHub token" in l for l in lines):
            why += " (your GitHub token was also tried and rejected: it may be expired or lack access)"
        raise RepoError(f"cannot access repository {ref.source}: {why}.{hint}")
    heads = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1][len("refs/heads/"):]] = parts[0]
    if not heads:
        where = f"https://github.com/{ref.full_name}" if ref.owner else ref.source
        raise RepoError(f"the repository {where} is empty (no commits yet). SwarmForge needs a base commit "
                        "to branch from: on GitHub click 'Add a README' (or push any commit), then start again.")
    if ref.branch and ref.branch not in heads:
        # /tree/<branch>/<path>: find the longest prefix that is a real branch
        parts = ref.branch.split("/")
        for i in range(len(parts), 0, -1):
            cand = "/".join(parts[:i])
            if cand in heads:
                ref.branch = cand
                break
        else:
            raise RepoError(f"branch '{ref.branch}' does not exist in {ref.source}; "
                            f"available: {', '.join(sorted(heads)[:10])}")
    return ref
