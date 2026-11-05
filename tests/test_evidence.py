"""Tests for visual evidence collection and GitHub asset upload (offline only)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from swarmforge import evidence
from swarmforge.config import GitHubConfig
from swarmforge.github import GitHubClient, GitHubError, pr_number_from_url

# --- evidence.touches_ui / plan_file_set / tabs_for -------------------------


def test_touches_ui_detects_dashboard_and_css():
    assert evidence.touches_ui(["src/swarmforge/dashboard.py"])
    assert evidence.touches_ui(["src/swarmforge/static/style.css"])
    assert evidence.touches_ui(["src/swarmforge/dashboard_pages/runs.py"])
    assert not evidence.touches_ui(["src/swarmforge/state.py", "src/swarmforge/llm.py"])
    assert not evidence.touches_ui([])


def test_plan_file_set_flattens_tasks():
    tasks = [
        {"expected_files": ["a.py", "b.css"]},
        type("T", (), {"expected_files": ["c.py"]})(),
        {"other": 1},  # missing expected_files
    ]
    assert evidence.plan_file_set(tasks) == ["a.py", "b.css", "c.py"]
    assert evidence.plan_file_set(None) == []


def test_tabs_for_css_and_failed_runs():
    assert evidence.tabs_for(["x.css"]) == ["work", "settings"]
    assert evidence.tabs_for(["src/swarmforge/dashboard.py"]) == [
        "work",
        "new-task",
        "failures",
        "speed",
        "chat",
        "knowledge",
        "settings",
    ]
    # failed runs always include the Review/findings tab
    assert "work" in evidence.tabs_for(["state.py"], failed=True)
    assert evidence.tabs_for(["state.py"]) == []


# --- collect / manifest round-trip (stubbed capture, no browser) ------------


def test_collect_and_manifest_roundtrip(tmp_path, monkeypatch):
    shots_dir = tmp_path / "shots"
    pngs = {
        "work-light": shots_dir / "work-light.png",
        "new-task-dark": shots_dir / "new-task-dark.png",
    }
    for p in pngs.values():
        shots_dir.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")

    calls = {}

    def fake_capture(repo_dir, out_dir, *, tabs, themes):
        calls["tabs"] = tabs
        calls["themes"] = themes
        return pngs

    monkeypatch.setattr(evidence.visual, "capture_tabs", fake_capture)
    shots = evidence.collect(tmp_path, shots_dir, tabs=["work", "new-task"])

    assert calls["tabs"] == ["work", "new-task"]
    assert {s["tab"] for s in shots} == {"work", "new-task"}
    assert {s["theme"] for s in shots} == {"light", "dark"}
    assert all(Path(s["file"]).exists() for s in shots)

    manifest = shots_dir / "manifest.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text(encoding="utf-8")) == shots
    assert evidence.read_manifest(shots_dir) == shots


def test_read_manifest_missing_or_corrupt(tmp_path):
    assert evidence.read_manifest(tmp_path) == []
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert evidence.read_manifest(tmp_path) == []
    (tmp_path / "manifest.json").write_text('{"a": 1}', encoding="utf-8")
    assert evidence.read_manifest(tmp_path) == []


def test_collect_propagates_exceptions(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no browser")

    monkeypatch.setattr(evidence.visual, "capture_tabs", boom)
    with pytest.raises(RuntimeError):
        evidence.collect(tmp_path, tmp_path / "out", tabs=["work"])


# --- GitHub asset upload / PR body update (MockTransport) -------------------


def _client(handler) -> GitHubClient:
    return GitHubClient(
        GitHubConfig(token_env="SF_TEST_TOKEN"), transport=httpx.MockTransport(handler)
    )


def test_upload_pr_asset_posts_multipart_and_returns_url(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(
            201, json={"url": "https://api.github.com/repos/o/r/assets/1"}
        )

    png = tmp_path / "work-light.png"
    png.write_bytes(b"\x89PNG fake")
    client = _client(handler)
    client.token = "tok"  # simulate an available token
    url = client.upload_pr_asset("o", "r", 41, png)
    assert url == "https://api.github.com/repos/o/r/assets/1"
    assert seen["path"] == "/repos/o/r/issues/41/assets"
    assert "multipart/form-data" in seen["content_type"]
    assert b"\x89PNG fake" in seen["body"]


def test_upload_pr_asset_without_token_raises(tmp_path):
    client = _client(lambda request: httpx.Response(200, json={}))
    client.token = None
    with pytest.raises(GitHubError, match="no GitHub token"):
        client.upload_pr_asset("o", "r", 41, tmp_path / "x.png")


def test_upload_pr_asset_http_error(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(b"png")
    client = _client(lambda request: httpx.Response(422, json={}, text="boom"))
    client.token = "tok"
    with pytest.raises(GitHubError, match="HTTP 422"):
        client.upload_pr_asset("o", "r", 41, png)


def test_update_pr_body_patches():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(handler)
    client.token = "tok"
    client.update_pr_body("o", "r", 41, "new body")
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/repos/o/r/pulls/41"
    assert seen["json"] == {"body": "new body"}


def test_update_pr_body_without_token_raises():
    client = _client(lambda request: httpx.Response(200, json={}))
    client.token = None
    with pytest.raises(GitHubError, match="no GitHub token"):
        client.update_pr_body("o", "r", 41, "body")


def test_pr_number_from_url():
    assert pr_number_from_url("https://github.com/bigdan88/swarmforge/pull/41") == 41
    assert pr_number_from_url("https://github.com/o/r/pull/7/files") == 7
    assert pr_number_from_url("https://github.com/o/r/issues/5") is None
