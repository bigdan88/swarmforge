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
