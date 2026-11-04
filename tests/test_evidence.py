"""Tests for evidence.py and the new GitHubClient PR-asset methods."""

from __future__ import annotations

import json

import httpx
import pytest

from swarmforge import evidence
from swarmforge.config import GitHubConfig
from swarmforge.evidence import (
    attach_to_pr,
    evidence_markdown,
    plan_files,
    screenshot_tabs,
    shots_from_events,
    tabs_for_files,
    touches_ui,
)
from swarmforge.github import GitHubClient, GitHubError
from swarmforge.plan import Plan, Task


def test_touches_ui_classification():
    assert touches_ui(["src/swarmforge/dashboard.py"])
    assert touches_ui(["src/swarmforge/dashboard_pages/chat.py"])
    assert touches_ui(["assets/style.css"])
    assert not touches_ui(["src/swarmforge/state.py", "src/swarmforge/llm.py"])
    assert not touches_ui([])


def test_tabs_for_files():
    assert tabs_for_files(["src/swarmforge/dashboard_pages/chat.py"]) == ["chat"]
    assert tabs_for_files(["src/swarmforge/dashboard.py"]) == list(
        evidence.visual.DEFAULT_TABS
    )
    assert tabs_for_files(["a.css"]) == list(evidence.visual.DEFAULT_TABS)
    # dedupe + order preserved
    tabs = tabs_for_files(
        [
            "dashboard_pages/chat.py",
            "dashboard_pages/work.py",
            "dashboard_pages/chat.py",
            "state.py",
        ]
    )
    assert tabs == ["chat", "work"]


def test_plan_files_dataclass_and_dict():
    plan = Plan(
        summary="s",
        tasks=[
            Task(
                id="T1",
                title="a",
                description="",
                role="r",
                expected_files=["a.py", "b.py"],
            ),
            Task(
                id="T2",
                title="b",
                description="",
                role="r",
                expected_files=["b.py", "c.py"],
            ),
        ],
    )
    assert plan_files(plan) == ["a.py", "b.py", "c.py"]
    d = {"tasks": [{"expected_files": ["x.py"]}, {"expected_files": ["x.py", "y.py"]}]}
    assert plan_files(d) == ["x.py", "y.py"]


def test_screenshot_tabs_skips_non_ui():
    calls = []

    def fake_capture(*a, **k):
        calls.append((a, k))
        return {}

    assert (
        screenshot_tabs("repo", "out", ["state.py", "llm.py"], capture=fake_capture)
        == []
    )
    assert calls == []


def test_screenshot_tabs_with_fake_capture(tmp_path):
    calls = []

    def fake_capture(repo, out, *, tabs, themes, port):
        calls.append((tabs, themes, port))
        return {f"{t}-{th}": tmp_path / f"{t}-{th}.png" for t in tabs for th in themes}

    shots = screenshot_tabs(
        "repo",
        tmp_path,
        ["dashboard_pages/chat.py"],
        themes=("light",),
        capture=fake_capture,
        port=1234,
    )
    assert calls == [(["chat"], ("light",), 1234)]
    assert shots == [
        {"tab": "chat", "theme": "light", "file": str(tmp_path / "chat-light.png")}
    ]

    # failed run appends the failures tab
    calls.clear()
    shots = screenshot_tabs(
        "repo", tmp_path, ["dashboard_pages/chat.py"], failed=True, capture=fake_capture
    )
    assert calls[0][0] == ["chat", "failures"]
    assert {s["tab"] for s in shots} == {"chat", "failures"}


def test_shots_from_events():
    events = [
        {"kind": "evidence", "data": json.dumps({"shots": [{"tab": "old"}]})},
        {"kind": "other", "data": "{}"},
        {
            "kind": "evidence",
            "data": {"shots": [{"tab": "chat", "theme": "light", "file": "a.png"}]},
        },
    ]
    assert shots_from_events(events) == [
        {"tab": "chat", "theme": "light", "file": "a.png"}
    ]
    assert shots_from_events([{"kind": "evidence", "data": {"shots": []}}]) == []
    assert shots_from_events([]) == []
    assert shots_from_events([{"kind": "evidence"}]) == []


def test_evidence_markdown():
    md = evidence_markdown(
        [{"tab": "chat", "theme": "light", "file": "out/chat-light.png"}]
    )
    assert "## Visual evidence" in md
    assert "**chat** (light): `out/chat-light.png`" in md
    md2 = evidence_markdown([], failed=True)
    assert "## Visual evidence" in md2
    assert "unresolved" in md2


def _client(transport) -> GitHubClient:
    return GitHubClient(GitHubConfig(), transport=transport)


def test_upload_pr_asset_and_update_body(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.host == "uploads.github.com":
            assert "image/png" in request.headers["content-type"]
            assert request.url.params["name"] == "chat-light.png"
            assert request.url.path == "/repos/o/r/issues/7/assets"
            return httpx.Response(
                201, json={"browser_download_url": "https://example.com/a.png"}
            )
        if request.method == "GET" and request.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json={"body": "original"})
        if request.method == "PATCH" and request.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    gh = _client(httpx.MockTransport(handler))
    png = tmp_path / "chat-light.png"
    png.write_bytes(b"\x89PNG")
    url = gh.upload_pr_asset("o", "r", 7, png)
    assert url == "https://example.com/a.png"
    gh.update_pr_body("o", "r", 7, "new body")
    assert [r.method for r in seen] == ["POST", "PATCH"]


def test_upload_pr_asset_error():
    gh = _client(httpx.MockTransport(lambda r: httpx.Response(403, json={})))
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".png")
    with os.fdopen(fd, "wb") as f:
        f.write(b"\x89PNG")
    with pytest.raises(GitHubError):
        gh.upload_pr_asset("o", "r", 1, path)
    os.unlink(path)


def test_attach_to_pr_no_token():
    calls = []

    class FakeGH:
        token = None

        def upload_pr_asset(self, *a, **k):
            calls.append("upload")

        def update_pr_body(self, *a, **k):
            calls.append("patch")

    assert (
        attach_to_pr(
            FakeGH(),
            "o",
            "r",
            "https://github.com/o/r/pull/7",
            [{"tab": "chat", "theme": "light", "file": "a.png"}],
        )
        is None
    )
    assert calls == []

    # no shots -> also skip
    class FakeGH2(FakeGH):
        token = "tok"

    assert (
        attach_to_pr(FakeGH2(), "o", "r", "https://github.com/o/r/pull/7", []) is None
    )
    assert calls == []


def test_attach_to_pr_uploads_and_appends(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                201, json={"browser_download_url": "https://example.com/x.png"}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"body": "original body"})
        return httpx.Response(200, json={})

    gh = _client(httpx.MockTransport(handler))
    gh.token = "tok"
    png = tmp_path / "chat-light.png"
    png.write_bytes(b"\x89PNG")
    urls = attach_to_pr(
        gh,
        "o",
        "r",
        "https://github.com/o/r/pull/7",
        [{"tab": "chat", "theme": "light", "file": str(png)}],
    )
    assert urls == ["https://example.com/x.png"]
    patch = next(r for r in requests if r.method == "PATCH")
    body = json.loads(patch.content)
    assert body["body"].startswith("original body")
    assert "## 📸 Visual evidence" in body["body"]
    assert "![chat (light)](https://example.com/x.png)" in body["body"]
