"""Offline tests for the responsive-layout breakpoint checks (no browser, no network).

The slow real-browser test at the bottom skips gracefully when Chromium is missing.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from swarmforge import state
from swarmforge.db import Store
from swarmforge.visual import (
    BREAKPOINT_WIDTHS,
    OVERFLOW_JS,
    RegressError,
    breakpoint_findings,
    record_breakpoint_findings,
    render_breakpoints,
    slugify,
)

CLEAN_DOM = {"viewport": 0, "doc_scroll_width": 360, "overflowing": [], "clipped": []}
WIDE_DOM = {
    "viewport": 375,
    "doc_scroll_width": 420,
    "overflowing": [{"selector": "div.wide-panel", "width": 400.0,
                     "left": 10.0, "right": 410.0}],
    "clipped": [],
}


class FakePage:
    """Duck-typed Playwright page returning canned DOM payloads per viewport width."""

    def __init__(self, dom_by_width: dict[int, dict]):
        self.dom_by_width = dom_by_width
        self.goto_calls: list[str] = []
        self.shots: list[Path] = []
        self.eval_args: list[int] = []
        self.closed = False

    def goto(self, url, **kw):
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, **kw):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.shots.append(Path(path))

    def evaluate(self, js, arg=None):
        assert js == OVERFLOW_JS
        self.eval_args.append(arg)
        return self.dom_by_width.get(arg, CLEAN_DOM)

    def close(self):
        self.closed = True


def fake_open_page(dom_by_width: dict[int, dict], seen: list[tuple[int, str]]):
    def open_page(width: int, theme: str):
        page = FakePage(dom_by_width)
        seen.append((width, theme))
        return page
    return open_page


# ------------------------------------------------------------- breakpoint_findings

def test_breakpoint_findings_document_overflow_is_major():
    fs = breakpoint_findings("Chat", 375, {"doc_scroll_width": 420, "overflowing": [], "clipped": []})
    assert len(fs) == 1
    f = fs[0]
    assert f["severity"] == "major" and f["blocking"] is True
    assert f["page"] == "chat" and f["theme"] == "light" and f["width"] == 375
    assert f["title"] == "layout overflow at 375px viewport"
    assert "420" in f["description"]


def test_breakpoint_findings_element_overflow_lists_selectors():
    fs = breakpoint_findings("Chat", 375, WIDE_DOM, theme="dark")
    assert len(fs) == 1
    f = fs[0]
    assert f["severity"] == "major" and f["blocking"] is True and f["theme"] == "dark"
    assert "div.wide-panel" in f["description"] and "400" in f["description"]
    assert f["recommendation"].startswith("make the Chat tab responsive at 375px")


def test_breakpoint_findings_only_clipped_is_minor_non_blocking():
    dom = {"doc_scroll_width": 375, "overflowing": [],
           "clipped": [{"selector": "p.caption", "scroll_width": 500, "client_width": 360}]}
    fs = breakpoint_findings("Review", 375, dom)
    assert len(fs) == 1
    f = fs[0]
    assert f["severity"] == "minor" and f["blocking"] is False
    assert f["title"] == "content clipped at 375px viewport"
    assert "p.caption" in f["description"]


def test_breakpoint_findings_clean_is_empty():
    assert breakpoint_findings("Chat", 768, {"doc_scroll_width": 760, "overflowing": [], "clipped": []}) == []


def test_breakpoint_findings_keys_match_gate_schema():
    f = breakpoint_findings("Chat", 375, WIDE_DOM)[0]
    assert set(f) == {"severity", "page", "theme", "title", "description",
                      "recommendation", "blocking", "layout_overflow", "width"}
    json.dumps(f)  # all JSON-safe


# ------------------------------------------------------------- render_breakpoints

def test_render_breakpoints_flags_400px_div_at_375(tmp_path):
    seen: list[tuple[int, str]] = []
    doms = {375: WIDE_DOM, 768: CLEAN_DOM, 1280: CLEAN_DOM}
    report = render_breakpoints("Chat", url="http://example.invalid/",
                                open_page=fake_open_page(doms, seen),
                                out_dir=tmp_path, theme="light")
    # the fake page got each viewport width in order
    assert [w for w, _ in seen] == [375, 768, 1280]
    assert all(t == "light" for _, t in seen)
    assert report["tab"] == "chat" and report["widths"] == [375, 768, 1280]
    assert report["ok"] is False and report["error"] is None
    assert len(report["results"]) == 3
    by_width = {r["width"]: r for r in report["results"]}
    assert by_width[375]["status"] == "overflow"
    assert by_width[375]["doc_scroll_width"] == 420
    assert by_width[768]["status"] == "clean" and by_width[1280]["status"] == "clean"
    # exactly one major finding, for width 375, mentioning the selector and its width
    majors = [f for f in report["findings"] if f["severity"] == "major"]
    assert len(majors) == 1 and majors[0]["width"] == 375
    assert "div.wide-panel" in majors[0]["description"]
    assert "400" in majors[0]["description"]
    assert not [f for f in report["findings"] if f.get("width") in (768, 1280)]
    # screenshots recorded per width with the expected filename pattern
    for w in (375, 768, 1280):
        shot = tmp_path / f"chat-{w}px-light.png"
        assert shot.exists() and by_width[w]["shot"] == str(shot)
    # JSON round-trips
    assert json.loads(json.dumps(report)) == report


def test_render_breakpoints_clipped_only_is_minor(tmp_path):
    dom = {"doc_scroll_width": 375, "overflowing": [],
           "clipped": [{"selector": "p.caption", "scroll_width": 500, "client_width": 360}]}
    report = render_breakpoints("Review", widths=[375], url="http://example.invalid/",
                                open_page=fake_open_page({375: dom}, []), out_dir=tmp_path)
    assert report["ok"] is False
    assert [f["severity"] for f in report["findings"]] == ["minor"]
    assert report["results"][0]["status"] == "clean"


def test_render_breakpoints_clean_report_ok(tmp_path):
    report = render_breakpoints("Chat", widths=[768], url="http://example.invalid/",
                                open_page=fake_open_page({}, []), out_dir=tmp_path)
    assert report["ok"] is True and report["findings"] == []
    assert report["results"][0]["status"] == "clean"


def test_render_breakpoints_page_error_becomes_critical_finding(tmp_path):
    class BoomPage:
        def goto(self, url, **kw):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        def close(self):
            pass

    report = render_breakpoints("Chat", widths=[375], url="http://example.invalid/",
                                open_page=lambda w, t: BoomPage(), out_dir=tmp_path)
    assert report["ok"] is False
    assert report["results"][0]["status"] == "error"
    f = report["findings"][0]
    assert f["severity"] == "critical" and f["blocking"] is True
    assert "ERR_CONNECTION_REFUSED" in f["description"]


def test_render_breakpoints_default_widths_constant():
    assert BREAKPOINT_WIDTHS == (375, 768, 1280)


# ------------------------------------------------------------- validation

@pytest.mark.parametrize("kwargs", [
    {"tab": "  "},
    {"tab": "Chat", "widths": []},
    {"tab": "Chat", "widths": [100]},
    {"tab": "Chat", "widths": [4000]},
    {"tab": "Chat", "widths": ["abc"]},
    {"tab": "Chat", "widths": [375.5]},
])
def test_render_breakpoints_rejects_bad_input(kwargs):
    with pytest.raises(RegressError):
        render_breakpoints(kwargs.get("tab", "Chat"), kwargs.get("widths", [375]),
                           url="http://example.invalid/", open_page=fake_open_page({}, []))


def test_render_breakpoints_rejects_unknown_theme():
    with pytest.raises(RegressError):
        render_breakpoints("Chat", [375], url="http://example.invalid/",
                           theme="solarized", open_page=fake_open_page({}, []))


def test_render_breakpoints_missing_playwright_without_open_page(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pw(name, *a, **kw):
        if "playwright" in name:
            raise ImportError("no playwright")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pw)
    with pytest.raises(RegressError, match="playwright is not installed"):
        render_breakpoints("Chat", [375], url="http://example.invalid/")


# ------------------------------------------------------------- integration (AC3)

def test_findings_feed_repair_tasks_and_store(tmp_path):
    seen: list[tuple[int, str]] = []
    doms = {375: WIDE_DOM, 768: CLEAN_DOM, 1280: CLEAN_DOM}
    report = render_breakpoints("Chat", url="http://example.invalid/",
                                open_page=fake_open_page(doms, seen), out_dir=tmp_path)
    # blocking findings become repair tasks; the minor/clipped ones do not
    tasks = state.repair_tasks_from_findings(
        report["findings"], ["developer_backend", "developer_frontend"], cycle=1)
    assert len(tasks) == 1
    assert tasks[0].id == "R1F1"
    assert "layout overflow at 375px viewport" in tasks[0].title
    assert "div.wide-panel" in tasks[0].description

    # minor-only report produces no repair tasks
    minor_report = {"findings": [{"severity": "minor", "page": "review", "theme": "light",
                                  "blocking": False, "width": 375,
                                  "title": "content clipped at 375px viewport",
                                  "description": "d", "recommendation": "r",
                                  "file": "review (375px)"}]}
    assert state.repair_tasks_from_findings(
        minor_report["findings"], ["developer_frontend"], cycle=1) == []

    # record_breakpoint_findings writes rows the Review-tab table renders
    store = Store(tmp_path / "db.sqlite")
    store.create_run("run-1", state="COMPLETED", issue_title="layout", repo="x/y")
    n = record_breakpoint_findings(store, "run-1", report, cycle=2)
    assert n == 1
    rows = store.findings("run-1")
    assert len(rows) == 1
    row = rows[0]
    # the columns the Review tab's findings table selects
    for col in ("cycle", "source", "severity", "file", "line", "title",
                "description", "recommendation"):
        assert col in row
    assert row["cycle"] == 2 and row["source"] == "layout"
    assert row["severity"] == "major"
    assert row["file"] == "chat (375px)"
    assert row["title"] == "layout overflow at 375px viewport"
    assert "div.wide-panel" in row["description"]
    assert row["recommendation"].startswith("make the Chat tab responsive")


# ------------------------------------------------------------- real browser (slow)

@pytest.mark.slow
def test_render_breakpoints_real_browser_flags_wide_div(tmp_path, browser_slot):
    pytest.importorskip("playwright")
    html = tmp_path / "page.html"
    html.write_text("<html><body><div style='width:400px;height:50px'>wide</div></body></html>")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    import functools

    server = HTTPServer(("127.0.0.1", port),
                       functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with browser_slot():
            try:
                report = render_breakpoints("Test", widths=[375, 768],
                                            url=f"http://127.0.0.1:{port}/{html.name}",
                                            out_dir=tmp_path)
            except (OSError, RuntimeError) as e:
                if "not installed" in str(e) or "chromium" in str(e).lower():
                    pytest.skip(f"no headless Chromium here: {e}")
                raise
    finally:
        server.shutdown()
    by_width = {r["width"]: r for r in report["results"]}
    assert by_width[375]["status"] == "overflow"
    assert by_width[768]["status"] == "clean"
    assert report["ok"] is False
    majors = [f for f in report["findings"] if f["severity"] == "major"]
    assert len(majors) == 1 and majors[0]["width"] == 375
    assert slugify("Test") == "test"
