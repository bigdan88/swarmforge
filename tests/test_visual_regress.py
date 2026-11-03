"""Offline tests for the visual-regression snapshot gate (no browser, no network)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from swarmforge import visual
from swarmforge.visual import (
    DIFF_THRESHOLD,
    RegressError,
    capture_tabs,
    golden_dir,
    golden_gate,
    maybe_refresh_golden_after_update,
    pixel_diff_score,
    regress,
    slugify,
    update_golden,
)

TABS = ["work", "settings"]
THEMES = ("light", "dark")


def _git(repo: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "web").mkdir()
    (repo / "web" / "style.css").write_text("body { color: #336699; margin: 0; }\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def _colour(repo_dir: Path) -> tuple[int, int, int]:
    """The RGB colour the repo's CSS declares (whitespace-only reformat -> same colour)."""
    import hashlib

    text = Path(repo_dir / "web" / "style.css").read_text()
    m = re.search(r"#([0-9a-fA-F]{6})", text)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    norm = "".join(text.split())
    h = hashlib.sha256(norm.encode()).digest()
    return h[0], h[1], h[2]


def fake_capture(repo_dir, out_dir, *, tabs, themes, port=None):
    """Write one solid 40x40 PNG per tab/theme, coloured by the repo's CSS content."""
    from PIL import Image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rgb = _colour(repo_dir)
    shots = {}
    for theme in themes:
        for tab in tabs:
            p = out_dir / f"{tab}-{theme}.png"
            Image.new("RGB", (40, 40), rgb).save(p)
            shots[f"{tab}-{theme}"] = p
    return shots


def change_colour(repo: Path, colour: str) -> None:
    (repo / "web" / "style.css").write_text(f"body {{ color: {colour}; margin: 0; }}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "colour change")


def reformat_only(repo: Path) -> None:
    (repo / "web" / "style.css").write_text("body{color:#336699;margin:0;}\n\n\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "reformat")


def test_slugify_matches_dashboard_convention():
    assert slugify("New task") == "new-task"
    assert slugify("Pull requests") == "pull-requests"
    assert slugify("work") == "work"


def test_pixel_diff_score(tmp_path):
    from PIL import Image

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    c = tmp_path / "c.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(a)
    Image.new("RGB", (10, 10), (0, 0, 0)).save(b)
    Image.new("RGB", (10, 10), (255, 255, 255)).save(c)
    assert pixel_diff_score(a, b) == 0.0
    assert pixel_diff_score(a, c) == 1.0
    assert pixel_diff_score(a, a.read_bytes()) == 0.0
    # size mismatch
    d = tmp_path / "d.png"
    Image.new("RGB", (11, 10), (0, 0, 0)).save(d)
    assert pixel_diff_score(a, d) == 1.0
    # small difference below tol is ignored, above tol counts
    e = tmp_path / "e.png"
    f = tmp_path / "f.png"
    Image.new("RGB", (10, 10), (100, 100, 100)).save(e)
    Image.new("RGB", (10, 10), (200, 100, 100)).save(f)  # grayscale diff ~30
    assert pixel_diff_score(e, f, tol=16) == 1.0
    assert pixel_diff_score(e, f, tol=40) == 0.0


def test_regress_catches_colour_change(tmp_path):
    repo = make_repo(tmp_path)
    change_colour(repo, "#111111")
    res = regress(
        "feature", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
    )
    assert res["ok"] is False
    assert res["findings"], "a colour change must produce findings"
    f = res["findings"][0]
    assert f["severity"] == "major"
    assert f["blocking"] is True
    assert f["golden_regression"] is True
    assert f["page"] in TABS
    assert res["max_score"] > DIFF_THRESHOLD
    assert all(row["score"] > DIFF_THRESHOLD for row in res["tabs"])
    assert all(row["status"] == "diff" for row in res["tabs"])
    assert set(res["shots"]) == {f"{t}-{th}" for t in TABS for th in THEMES}


def test_regress_ignores_whitespace_reformat(tmp_path):
    repo = make_repo(tmp_path)
    reformat_only(repo)
    res = regress(
        "feature", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
    )
    assert res["ok"] is True
    assert res["findings"] == []
    assert res["max_score"] == 0.0
    assert all(row["score"] == 0.0 and row["status"] == "clean" for row in res["tabs"])


def test_regress_missing_on_base_counts_as_full_diff(tmp_path):
    repo = make_repo(tmp_path)

    def capture_missing(repo_dir, out_dir, *, tabs, themes, port=None):
        shots = fake_capture(repo_dir, out_dir, tabs=tabs, themes=themes, port=port)
        if Path(repo_dir).name.startswith("sf-visual-pr"):
            pass  # keep all
        else:
            shots.pop("work-light")
        return shots

    res = regress(
        "feature", "main", repo=repo, tabs=TABS, themes=THEMES, capture=capture_missing
    )
    row = next(r for r in res["tabs"] if r["tab"] == "work" and r["theme"] == "light")
    assert row["score"] == 1.0 and row["status"] == "missing"
    assert res["ok"] is False


def test_regress_nonexistent_branch_raises(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(RegressError):
        regress(
            "nope", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
        )


def test_regress_cleans_up_shot_temp_dirs(tmp_path, monkeypatch):
    import tempfile as tf_mod

    repo = make_repo(tmp_path)
    created: list[Path] = []
    real_mkdtemp = tf_mod.mkdtemp

    def recording_mkdtemp(prefix=""):
        p = Path(real_mkdtemp(prefix=prefix))
        if prefix.startswith("sf-visual-") and "shots" in prefix:
            created.append(p)
        return str(p)

    monkeypatch.setattr(tf_mod, "mkdtemp", recording_mkdtemp)
    res = regress(
        "feature", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
    )
    assert res["ok"] is True
    assert created, "expected regress() to create shot temp dirs"
    assert not any(p.exists() for p in created)

    # the RegressError path must clean up too
    created.clear()
    with pytest.raises(RegressError):
        regress(
            "nope", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
        )


def test_regress_leaves_no_demo_db_in_repo(tmp_path):
    repo = make_repo(tmp_path)
    regress(
        "feature", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
    )
    assert not (repo / ".swarmforge" / "visual-demo.sqlite").exists()


def test_capture_tabs_leaves_no_demo_db_in_repo(tmp_path, monkeypatch):
    """capture_tabs must keep the demo DB in a temp dir and remove it, even when it fails."""
    import subprocess

    repo = make_repo(tmp_path)
    db_paths: list[str] = []

    class FakeProc:
        pid = 1234

        def kill(self):
            pass

        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        db_paths.append(kwargs["env"]["SWARMFORGE_DB"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(visual, "_wait_http", lambda *a, **k: None)
    monkeypatch.setattr(visual, "_run", lambda *a, **k: None)

    import sys as _sys
    import types

    class FakePw:
        def __enter__(self):
            raise RegressError("no browser")

        def __exit__(self, *a):
            return False

    fake_pw_mod = types.ModuleType("playwright")
    fake_api_mod = types.ModuleType("playwright.sync_api")
    fake_api_mod.sync_playwright = lambda: FakePw()
    fake_pw_mod.sync_api = fake_api_mod
    monkeypatch.setitem(_sys.modules, "playwright", fake_pw_mod)
    monkeypatch.setitem(_sys.modules, "playwright.sync_api", fake_api_mod)

    out = tmp_path / "shots"
    with pytest.raises(RegressError):
        capture_tabs(repo, out, tabs=TABS, themes=THEMES)

    assert db_paths, "expected the dashboard process to be launched"
    db = Path(db_paths[0])
    assert ".swarmforge" not in db.parts
    assert not db.exists(), "the demo DB temp dir must be removed"
    assert not (repo / ".swarmforge" / "visual-demo.sqlite").exists()
    assert not list(out.glob("*.png"))


def test_capture_tabs_seed_failure_cleans_temp_db(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    seen: list[str] = []

    def fake_streamlit_env(repo_dir, db):
        seen.append(str(db))
        return {}

    def boom(*a, **k):
        raise RegressError("seed failed")

    monkeypatch.setattr(visual, "_streamlit_env", fake_streamlit_env)
    monkeypatch.setattr(visual, "_run", boom)
    with pytest.raises(RegressError):
        capture_tabs(repo, tmp_path / "shots", tabs=TABS, themes=THEMES)
    assert seen and not Path(seen[0]).exists()
    assert not (repo / ".swarmforge").exists()


def test_update_golden_and_golden_gate(tmp_path):
    repo = make_repo(tmp_path)
    written = update_golden(
        repo=repo, branch="main", tabs=TABS, themes=THEMES, capture=fake_capture
    )
    assert written
    for key in (f"{t}-{th}" for t in TABS for th in THEMES):
        assert (golden_dir(repo) / f"{key}.png").exists()
    assert all(str(p).startswith(str(golden_dir(repo))) for p in written)

    # unchanged branch passes the gate
    shots = [
        {"page": t, "theme": th, "file": str(golden_dir(repo) / f"{t}-{th}.png")}
        for t in TABS
        for th in THEMES
    ]
    assert golden_gate(shots, repo) == []

    # a changed branch is flagged
    change_colour(repo, "#22cc44")
    res = regress(
        "feature", "main", repo=repo, tabs=TABS, themes=THEMES, capture=fake_capture
    )
    assert res["ok"] is False
    assert all(f["blocking"] for f in res["findings"])
    # and the orchestrator hook flags the same thing
    pr_shots = fake_capture(repo, tmp_path / "pr", tabs=TABS, themes=THEMES)
    gate_shots = [
        {"page": t, "theme": th, "file": str(pr_shots[f"{t}-{th}"])}
        for t in TABS
        for th in THEMES
    ]
    findings = golden_gate(gate_shots, repo)
    assert findings and findings[0]["severity"] == "major"
    assert findings[0]["blocking"] is True
    assert findings[0]["golden_regression"] is True


def test_golden_gate_no_golden_dir(tmp_path):
    repo = make_repo(tmp_path)
    assert golden_dir(repo).exists() is False
    assert (
        golden_gate([{"page": "work", "theme": "light", "file": "/nope.png"}], repo)
        == []
    )


def test_maybe_refresh_golden_after_update(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    # no golden dir -> None, update_golden never called
    called = []
    monkeypatch.setattr(visual, "update_golden", lambda **kw: called.append(kw) or [])
    assert maybe_refresh_golden_after_update(repo) is None
    assert called == []

    # with goldens present -> refresh runs
    gdir = golden_dir(repo)
    gdir.mkdir(parents=True)
    (gdir / "work-light.png").write_bytes(b"\x89PNG")
    out = maybe_refresh_golden_after_update(repo)
    assert out is not None and out["refreshed"] is True
    assert called

    # a failing refresh is swallowed into a reason
    def boom(**kw):
        raise RegressError("no browser")

    monkeypatch.setattr(visual, "update_golden", boom)
    out = maybe_refresh_golden_after_update(repo)
    assert out == {"refreshed": False, "reason": "no browser"}


def test_golden_path_override(tmp_path):
    repo = make_repo(tmp_path)
    gdir = tmp_path / "elsewhere" / "golden"
    gdir.mkdir(parents=True)
    update_golden(
        repo=repo, branch="main", tabs=TABS, themes=THEMES, capture=fake_capture
    )
    # move the goldens to the override location
    import shutil

    for p in golden_dir(repo).glob("*.png"):
        shutil.copy(p, gdir / p.name)
    change_colour(repo, "#0000ff")
    res = regress(
        "feature",
        "main",
        repo=repo,
        golden_path=gdir,
        tabs=TABS,
        themes=THEMES,
        capture=fake_capture,
    )
    assert res["ok"] is False
