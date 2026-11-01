import subprocess

import pytest

from swarmforge.repos import RepoError, normalize_repo, preflight


@pytest.mark.parametrize("text,source,branch", [
    ("https://github.com/bigdan88/commander_forge/tree/main", "https://github.com/bigdan88/commander_forge.git", "main"),
    ("https://github.com/bigdan88/commander_forge", "https://github.com/bigdan88/commander_forge.git", None),
    ("https://github.com/bigdan88/commander_forge/", "https://github.com/bigdan88/commander_forge.git", None),
    ("https://github.com/bigdan88/commander_forge.git", "https://github.com/bigdan88/commander_forge.git", None),
    ("github.com/bigdan88/commander_forge", "https://github.com/bigdan88/commander_forge.git", None),
    ("bigdan88/commander_forge", "https://github.com/bigdan88/commander_forge.git", None),
    ("  'https://github.com/o/r/tree/feature/x'  ", "https://github.com/o/r.git", "feature/x"),
    ("https://github.com/o/r/blob/dev/src/a.py", "https://github.com/o/r.git", "dev"),
    ("https://github.com/o/r/issues/12", "https://github.com/o/r.git", None),
    ("https://github.com/o/r/pulls", "https://github.com/o/r.git", None),
    ("git@github.com:o/r.git", "https://github.com/o/r.git", None),
    ("https://gitlab.com/o/r.git", "https://gitlab.com/o/r.git", None),
])
def test_normalize_repo(text, source, branch):
    ref = normalize_repo(text)
    assert (ref.source, ref.branch) == (source, branch)


def test_normalize_local_and_unknown(tmp_path):
    ref = normalize_repo(f'"{tmp_path}"')
    assert ref.local and ref.source == str(tmp_path.resolve())
    with pytest.raises(RepoError, match="neither an existing folder"):
        normalize_repo(r"C:\nope\does-not-exist-123")
    with pytest.raises(RepoError):
        normalize_repo("   ")


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_preflight_local_messages(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepoError, match="not a git repository"):
        preflight(normalize_repo(str(plain)))
    _git(plain, "init", "-q", "-b", "main")
    with pytest.raises(RepoError, match="no commits yet"):
        preflight(normalize_repo(str(plain)))
    (plain / "a").write_text("x")
    _git(plain, "add", "-A")
    _git(plain, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
    assert preflight(normalize_repo(str(plain))).local


def test_preflight_remote_empty_and_branch_resolution(tmp_path, monkeypatch):
    # Run from a neutral cwd: `preflight` shells out to `git ls-remote` without an explicit
    # cwd, so a broken .git pointer in the pytest invocation directory (as in CI worktree
    # checkouts) would otherwise fail every remote check before git even reaches the URL.
    monkeypatch.chdir(tmp_path)
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    url = bare.resolve().as_uri()
    with pytest.raises(RepoError, match="is empty"):
        preflight(normalize_repo(url))
    work = tmp_path / "w"
    _git(tmp_path, "clone", "-q", url, str(work))
    (work / "a").write_text("x")
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
    ref = normalize_repo(url)
    ref.branch = "feature/x/src/app.py"     # what a /tree/feature/x/src/app.py URL produces
    assert preflight(ref).branch == "feature/x"
    ref.branch = "nope"
    with pytest.raises(RepoError, match="does not exist"):
        preflight(ref)
    with pytest.raises(RepoError, match="cannot access"):
        preflight(normalize_repo((tmp_path / "missing.git").resolve().as_uri()))


def test_public_repo_works_with_a_bad_token(tmp_path, monkeypatch):
    # A stale token must not break anonymous access (file:// stands in for a public remote).
    monkeypatch.chdir(tmp_path)  # neutral cwd, see test_preflight_remote_empty_and_branch_resolution
    bare = tmp_path / "pub.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    work = tmp_path / "w"
    _git(tmp_path, "clone", "-q", bare.resolve().as_uri(), str(work))
    (work / "a").write_text("x")
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    ref = preflight(normalize_repo(bare.resolve().as_uri()), token="expired-token")
    assert ref.use_auth is False
