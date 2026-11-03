"""Git workspace management: disposable clone, integration branch, per-task worktrees.

Safety properties:
  * SwarmForge never operates on the user's own checkout. It always clones the source
    (remote URL or local path) into <workdir>/runs/<run_id>/repo, so uncommitted user
    work can't be overwritten.
  * Each task gets its own worktree + branch; a worktree is leased to exactly one task.
  * Merges into the integration branch are serialized and never forced; conflicts abort the
    merge and are handed to a conflict-resolution agent in a separate worktree.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDES = ["__pycache__/", "*.pyc", ".pytest_cache/", ".swarmforge/", ".mypy_cache/",
            ".ruff_cache/"]
CONFLICT_MARKER_RE = re.compile(r"^(<{7}|>{7})( |$)", re.M)


class GitError(RuntimeError):
    pass


class EmptyRepository(GitError):
    """The source repository has no commits yet."""


class StaleSeed(GitError):
    """A previous run's result can't be brought up to date with the current base branch
    (the base moved on and merging it in conflicts)."""

    def __init__(self, message: str, conflicts: list[str] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


# Transient resource errors worth retrying (e.g. under heavy parallel load).
_TRANSIENT_GIT_ERRORS = ("unable to create thread", "Resource temporarily unavailable",
                         "fork failed", "cannot fork", "Cannot allocate memory",
                         "git-pack-objects died", "bad pack header")
_GIT_MAX_ATTEMPTS = 5


def _run_git_subprocess(cwd: Path, args: tuple[str, ...], env: dict | None,
                        check: bool) -> tuple[int, str, str]:
    """Run git, retrying transient resource errors with 0.1s -> 1.6s backoff."""
    for attempt in range(1, _GIT_MAX_ATTEMPTS + 1):
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        if p.returncode == 0:
            return p.returncode, p.stdout, p.stderr
        combined = p.stdout + p.stderr
        if attempt < _GIT_MAX_ATTEMPTS and any(t in combined for t in _TRANSIENT_GIT_ERRORS):
            time.sleep(min(0.1 * 2 ** (attempt - 1), 1.6))
            continue
        break
    if check:
        raise GitError(f"git {' '.join(args)} failed ({p.returncode}): "
                       f"{p.stderr.strip() or p.stdout.strip()}")
    return p.returncode, p.stdout, p.stderr


def git(cwd: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    _, out, _ = _run_git_subprocess(cwd, args, env, check)
    return out


def git_rc(cwd: Path, *args: str) -> tuple[int, str]:
    rc, out, err = _run_git_subprocess(cwd, args, None, False)
    return rc, out + err


@dataclass
class MergeResult:
    ok: bool
    sha: str | None = None
    conflicts: list[str] = field(default_factory=list)
    message: str = ""


def safe_slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:40] or "x"


class GitWorkspace:
    def __init__(self, run_dir: Path, run_id: str, branch_prefix: str = "swarmforge/"):
        self.run_dir = run_dir
        self.run_id = run_id
        self.short_id = run_id.rsplit("-", 1)[-1][:8]
        self.repo = run_dir / "repo"
        self.worktrees_dir = run_dir / "worktrees"
        self.prefix = branch_prefix
        self.base_branch: str | None = None
        self.base_sha: str | None = None
        self.integration_branch: str | None = None
        self._lock = threading.RLock()  # serializes operations on the main repo / git metadata
        self._leases: dict[Path, str] = {}
        self.seed_updated = False  # set by seed_from when the base had to be merged in

    # ----------------------------------------------------------------- setup
    def prepare(self, source: str, issue_number: int | None, base_branch: str | None = None,
                git_config: list[str] | None = None) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.repo.exists():
            raise GitError(f"{self.repo} already exists; refusing to overwrite")
        src_path = Path(source)
        if src_path.exists():
            rc, out = git_rc(src_path, "status", "--porcelain")
            self.source_dirty = rc == 0 and bool(out.strip())
            source = str(src_path.resolve())
        else:
            self.source_dirty = False
        # keep the repository's own line endings (Windows autocrlf would rewrite every file the
        # agents touch and confuse exact-match edits and diffs)
        args = ["-c", "core.autocrlf=false", "clone", "--quiet"]
        if base_branch:
            args += ["--branch", base_branch]
        git(self.run_dir, *(git_config or []), *args, source, "repo")
        rc, _ = git_rc(self.repo, "rev-parse", "--verify", "--quiet", "HEAD")
        if rc != 0:
            raise EmptyRepository(f"{source} has no commits yet")
        git(self.repo, "config", "user.name", "SwarmForge Bot")
        git(self.repo, "config", "user.email", "swarmforge@localhost")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "core.autocrlf", "false")
        excl = self.repo / ".git" / "info" / "exclude"
        excl.parent.mkdir(parents=True, exist_ok=True)
        with excl.open("a", encoding="utf-8") as f:
            f.write("\n# swarmforge\n" + "\n".join(EXCLUDES) + "\n")
        self.base_branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        self.base_sha = self.head(self.repo)
        tag = f"issue-{issue_number}" if issue_number else "task"
        self.integration_branch = f"{self.prefix}{tag}-{self.short_id}"
        git(self.repo, "checkout", "-q", "-b", self.integration_branch)
        self.worktrees_dir.mkdir(exist_ok=True)

    def seed_from(self, other_repo: Path, branch: str) -> str:
        """Start the integration branch from another run's result (same repository).
        The base branch/sha stay the original ones, so diffs, reviews and PRs cover all the work."""
        with self._lock:
            git(self.repo, "fetch", "--quiet", str(other_repo), f"+refs/heads/{branch}:refs/remotes/seed/{branch}")
            seed_sha = git(self.repo, "rev-parse", f"refs/remotes/seed/{branch}").strip()
            self.seed_updated = False
            git(self.repo, "reset", "--quiet", "--hard", seed_sha)
            rc, _ = git_rc(self.repo, "merge-base", "--is-ancestor", self.base_sha, seed_sha)
            if rc == 0:
                return seed_sha
            # The base branch moved on since the previous attempt (e.g. new commits were pushed to
            # main). Bring the previous result up to date by merging the current base into it.
            rc, out = git_rc(self.repo, "merge", "--no-ff", "--no-edit", "-m",
                             f"SwarmForge: update previous result with {self.base_branch} "
                             f"({self.base_sha[:8]})", self.base_sha)
            if rc != 0:
                conflicts = [c for c in git(self.repo, "diff", "--name-only", "--diff-filter=U",
                                             check=False).splitlines() if c]
                git_rc(self.repo, "merge", "--abort")
                git(self.repo, "reset", "--quiet", "--hard", self.base_sha)
                raise StaleSeed(
                    f"{self.base_branch} moved on since the previous result {branch} was made, and "
                    f"merging it in conflicts ({', '.join(conflicts[:10]) or out.strip()[:200]})",
                    conflicts)
            self.seed_updated = True
            return self.head(self.repo)

    def fetch_base(self, git_config: list[str] | None = None) -> str:
        """Fetch the latest base branch from the source repository; returns its sha."""
        with self._lock:
            ref = f"refs/remotes/origin/{self.base_branch}"
            git(self.repo, *(git_config or []), "fetch", "--quiet", "origin", f"+refs/heads/{self.base_branch}:{ref}")
            return git(self.repo, "rev-parse", ref).strip()

    def is_ancestor(self, a: str, b: str) -> bool:
        return git_rc(self.repo, "merge-base", "--is-ancestor", a, b)[0] == 0

    def attach(self, base_branch: str, base_sha: str, integration_branch: str) -> None:
        """Re-attach to an existing run directory (used by benchmark/report tooling)."""
        self.base_branch, self.base_sha, self.integration_branch = base_branch, base_sha, integration_branch

    @staticmethod
    def head(path: Path) -> str:
        return git(path, "rev-parse", "HEAD").strip()

    # ------------------------------------------------------------- worktrees
    def create_worktree(self, key: str, start_point: str | None = None) -> tuple[Path, str]:
        with self._lock:
            slug = safe_slug(key)
            branch = f"{self.prefix}{self.short_id}/{slug}"
            path = self.worktrees_dir / slug
            n = 1
            while path.exists() or self._branch_exists(branch):
                n += 1
                path = self.worktrees_dir / f"{slug}-{n}"
                branch = f"{self.prefix}{self.short_id}/{slug}-{n}"
            git(self.repo, "worktree", "add", "-q", "-b", branch, str(path),
                start_point or self.integration_branch)
            self._leases[path.resolve()] = key
            return path, branch

    def _branch_exists(self, branch: str) -> bool:
        rc, _ = git_rc(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return rc == 0

    def lease_owner(self, path: Path) -> str | None:
        return self._leases.get(path.resolve())

    def assert_lease(self, path: Path, key: str) -> None:
        owner = self._leases.get(path.resolve())
        if owner != key:
            raise GitError(f"worktree {path} is leased to {owner!r}, not {key!r}")

    def remove_worktree(self, path: Path) -> None:
        with self._lock:
            git(self.repo, "worktree", "remove", "--force", str(path), check=False)
            self._leases.pop(path.resolve(), None)

    # --------------------------------------------------------------- commits
    def commit_all(self, path: Path, message: str) -> str | None:
        """Stage and commit everything in a worktree. Returns sha, or None if nothing changed."""
        with self._lock:
            git(path, "add", "-A")
            rc, _ = git_rc(path, "diff", "--cached", "--quiet")
            if rc == 0:
                return None
            git(path, "commit", "-q", "--no-verify", "-m", message)
            return self.head(path)

    def has_changes_vs(self, branch: str, base: str) -> bool:
        rc, _ = git_rc(self.repo, "diff", "--quiet", base, branch)
        return rc != 0

    # ----------------------------------------------------------- integration
    def merge_into_integration(self, branch: str, message: str) -> MergeResult:
        with self._lock:
            cur = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
            if cur != self.integration_branch:
                raise GitError(f"integration checkout is on {cur}, expected {self.integration_branch}")
            rc, out = git_rc(self.repo, "merge", "--no-ff", "--no-edit", "-m", message, branch)
            if rc == 0:
                return MergeResult(ok=True, sha=self.head(self.repo), message=out.strip())
            conflicts = [l for l in git(self.repo, "diff", "--name-only", "--diff-filter=U",
                                        check=False).splitlines() if l.strip()]
            git(self.repo, "merge", "--abort", check=False)
            return MergeResult(ok=False, conflicts=conflicts, message=out.strip())

    def start_conflict_resolution(self, key: str, branch: str) -> tuple[Path, str, list[str]]:
        """Worktree at integration HEAD with `branch` merged in, conflict markers left in place."""
        path, res_branch = self.create_worktree(key)
        with self._lock:
            rc, out = git_rc(path, "merge", "--no-ff", "--no-commit", branch)
            conflicts = [l for l in git(path, "diff", "--name-only", "--diff-filter=U",
                                        check=False).splitlines() if l.strip()]
        return path, res_branch, conflicts

    def finish_conflict_resolution(self, path: Path, message: str) -> tuple[bool, list[str]]:
        """Verify markers are gone, then conclude the merge commit. Returns (ok, files_with_markers)."""
        with self._lock:
            files = [l for l in git(path, "diff", "--name-only", "HEAD", check=False).splitlines() if l]
            files += [l for l in git(path, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() if l]
            bad = []
            for f in sorted(set(files)):
                fp = path / f
                if fp.is_file():
                    try:
                        if CONFLICT_MARKER_RE.search(fp.read_text(encoding="utf-8", errors="replace")):
                            bad.append(f)
                    except OSError:
                        pass
            if bad:
                return False, bad
            git(path, "add", "-A")
            git(path, "commit", "-q", "--no-verify", "-m", message)
            return True, []

    # ------------------------------------------------------------------ diffs
    def diff(self, base: str | None = None, head: str | None = None, max_chars: int = 120_000) -> str:
        base = base or self.base_sha
        head = head or self.integration_branch
        d = git(self.repo, "diff", f"{base}...{head}")
        if len(d) > max_chars:
            d = d[:max_chars] + f"\n...[diff truncated, {len(d) - max_chars} more chars]"
        return d

    def diff_stat(self, base: str | None = None, head: str | None = None) -> str:
        return git(self.repo, "diff", "--stat", f"{base or self.base_sha}...{head or self.integration_branch}")

    def changed_files(self, base: str | None = None, head: str | None = None) -> list[str]:
        out = git(self.repo, "diff", "--name-only", f"{base or self.base_sha}...{head or self.integration_branch}")
        return [l for l in out.splitlines() if l.strip()]

    def deliver_to_local(self, source: Path) -> str:
        """Push the integration branch into the user's local source repo as a NEW branch.
        Never overwrites an existing branch and never touches the user's checkout/working tree."""
        branch = self.integration_branch
        source = Path(source).resolve()
        rc, _ = git_rc(source, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if rc == 0:
            raise GitError(f"branch {branch} already exists in {source}; refusing to overwrite it")
        with self._lock:
            last_out = ""
            for attempt in range(3):
                if attempt:
                    time.sleep(0.5 * attempt)
                rc, last_out = git_rc(self.repo, "push", "--quiet", str(source),
                                      f"refs/heads/{branch}:refs/heads/{branch}")
                if rc == 0:
                    return branch
        raise GitError(f"push to {source} failed after 3 attempts: {last_out}")

    def file_tree(self, path: Path | None = None, limit: int = 400) -> list[str]:
        files = git(path or self.repo, "ls-files").splitlines()
        return files[:limit]


async def run_git(fn, *args, **kwargs):
    """Run a blocking git helper off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)
