"""Acceptance coverage for the responsive-layout breakpoint check (issue: check-layout).

Complements tests/test_visual_breakpoints.py and the check-layout CLI tests in
tests/test_cli_visual.py with the gaps they leave per criterion:

- AC1: the empty-tab setup error goes through the real (unmonkeypatched)
  `render_breakpoints` validation path and exits 2 with no JSON on stdout.
- AC2: a real headless Chromium renders a 400 px div at 375/768 AND 1280 px
  (the developer slow test only covers 375/768) and skips gracefully when
  Chromium is genuinely unavailable.
- AC3: layout findings recorded via `record_breakpoint_findings` are visible
  in the dashboard Review-tab findings table (AppTest, offline).
- AC4: the auto-created temporary screenshot directory is removed both when
  `render_breakpoints` succeeds and when it raises, while a caller-supplied
  `out_dir` is never deleted (not even on error).
"""

from __future__ import annotations

import functools
import socket
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from swarmforge import cli, state, visual
from swarmforge.db import Store
from swarmforge.visual import (
    RegressError,
    breakpoint_findings,
    record_breakpoint_findings,
    render_breakpoints,
)

ROOT = Path(__file__).resolve().parents[1]
DASH = str(ROOT / "src" / "swarmforge" / "dashboard.py")
REVIEW_COLS = [
    "cycle",
    "source",
    "severity",
    "file",
    "line",
    "title",
    "description",
    "recommendation",
]

# DOM payload as OVERFLOW_JS would report it for a 400 px div at a 375 px viewport.
WIDE_DOM_375 = {
    "viewport": 375,
    "doc_scroll_width": 420,
    "overflowing": [
        {"selector": "div.wide-panel", "width": 400.0, "left": 10.0, "right": 410.0}
    ],
    "clipped": [],
}


def layout_report() -> dict:
    """A render_breakpoints-shaped report with one major overflow finding at 375 px."""
    findings = breakpoint_findings("Chat", 375, WIDE_DOM_375)
    return {
        "ok": False,
        "tab": "chat",
        "widths": [375, 768, 1280],
        "results": [
            {
                "width": w,
                "shot": "",
                "status": "clean",
                "doc_scroll_width": 360,
                "overflowing": [],
                "clipped": [],
            }
            for w in (768, 1280)
        ],
        "findings": findings,
        "error": None,
    }


# ------------------------------------------------------------------- AC1: CLI


def test_cli_check_layout_empty_tab_exits_2_without_browser(capsys):
    """`--tab ''` is a setup error: real render_breakpoints validation, exit 2,
    stderr message, no JSON on stdout, and no browser/dashboard is launched."""
    args = cli.build_parser().parse_args(["visual", "check-layout", "--tab", ""])
    assert cli.cmd_visual(args, None) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tab must not be empty" in captured.err


def test_cli_check_layout_width_out_of_range_exits_2(capsys):
    """Widths are validated before anything launches (exit 2 = setup error)."""
    args = cli.build_parser().parse_args(
        ["visual", "check-layout", "--tab", "chat", "--widths", "100"]
    )
    assert cli.cmd_visual(args, None) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "visual check-layout:" in captured.err


# ------------------------------------------------------------------- AC3: UI


def test_review_tab_table_lists_recorded_layout_findings(tmp_path, monkeypatch):
    """record_breakpoint_findings -> Store -> the Review tab's findings table
    shows the layout row with the documented cycle/source/severity/... columns."""
    import streamlit as st_
    from streamlit.testing.v1 import AppTest

    from swarmforge.demo import seed

    st_.cache_resource.clear()
    db = tmp_path / "swarmforge.db"
    seed(db)
    store = Store(db)
    rid = "20261101-110000-d4e5f6"

    report = layout_report()
    assert record_breakpoint_findings(store, rid, report, cycle=3) == 1

    for k, v in {
        "SWARMFORGE_CONFIG": str(tmp_path / "swarmforge.yaml"),
        "SWARMFORGE_DB": str(db),
        "SWARMFORGE_DASH_AUTOREFRESH": "0",
    }.items():
        monkeypatch.setenv(k, v)
    at = AppTest.from_file(DASH, default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    rows = [
        b
        for b in at.button
        if (b.key or "").startswith("op_") and rid[-6:] in (b.key or "")
    ]
    assert rows, "seeded run row not found on the Work page"
    rows[0].click().run()
    assert not at.exception, [e.value for e in at.exception]

    tables = []
    for el in at.dataframe:
        df = el.value
        if list(df.columns) == REVIEW_COLS:
            tables.append(df)
    assert tables, "Review-tab findings table (cycle/source/... columns) not rendered"
    # one of the findings tables carries the layout row recorded via the report
    layout_rows = [
        r for df in tables for _, r in df.iterrows() if r["source"] == "layout"
    ]
    assert len(layout_rows) == 1, [df.to_dict("records") for df in tables]
    r = layout_rows[0]
    assert int(r["cycle"]) == 3
    assert r["severity"] == "major"
    assert r["file"] == "chat (375px)"
    assert r["title"] == "layout overflow at 375px viewport"
    assert "div.wide-panel" in r["description"]
    assert r["recommendation"].startswith("make the Chat tab responsive at 375px")


# ------------------------------------------------------- AC4: temp screenshot dir


def test_auto_out_dir_removed_when_page_render_errors():
    """AC4 (per-viewport error): with no out_dir given, the auto-created temp
    dir is gone after the call even though every viewport errored."""

    class BoomPage:
        def goto(self, url, **kw):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        def close(self):
            pass

    report = render_breakpoints(
        "Chat",
        widths=[375],
        url="http://example.invalid/",
        open_page=lambda w, t: BoomPage(),
    )
    assert report["results"][0]["status"] == "error"
    shot_parent = Path(report["results"][0]["shot"]).parent
    assert not shot_parent.exists(), f"auto out_dir leaked: {shot_parent}"


def test_auto_out_dir_removed_when_dashboard_launch_fails(monkeypatch):
    """AC4 (raised error): when the local-dashboard launch raises inside
    render_breakpoints, the auto-created screenshot dir (and temp db dir) are
    removed before the exception propagates."""
    import tempfile as tempfile_mod

    created: list[Path] = []
    real_mkdtemp = tempfile_mod.mkdtemp

    def spy_mkdtemp(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        created.append(Path(p))
        return p

    monkeypatch.setattr(tempfile_mod, "mkdtemp", spy_mkdtemp)

    def boom(*a, **kw):
        raise RegressError("streamlit is not installed")

    monkeypatch.setattr(visual, "_streamlit_env", boom)
    with pytest.raises(RegressError, match="streamlit"):
        render_breakpoints("Chat", widths=[375])
    assert created, "expected temp dirs to have been created during the attempt"
    for p in created:
        assert not p.exists(), f"temp dir leaked: {p}"


def test_explicit_out_dir_survives_launch_failure(tmp_path, monkeypatch):
    """AC4: a caller-supplied out_dir is never deleted, even when the render
    raises before any screenshot is written."""

    def boom(*a, **kw):
        raise RegressError("streamlit is not installed")

    monkeypatch.setattr(visual, "_streamlit_env", boom)
    with pytest.raises(RegressError, match="streamlit"):
        render_breakpoints("Chat", widths=[375], out_dir=tmp_path)
    assert tmp_path.exists()
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------- AC2: real browser


@pytest.mark.slow
def test_real_browser_flags_400px_div_at_375_only(tmp_path, browser_slot):
    """Render a genuinely 400 px-wide div in headless Chromium at the three
    issue breakpoints: overflow finding at 375 px, clean at 768 and 1280 px.

    Skips gracefully (never fails) when Chromium is unavailable: missing
    package, missing binary, or a browser that cannot launch.
    """
    pytest.importorskip("playwright")

    html = tmp_path / "wide.html"
    html.write_text(
        "<html><head><meta name='viewport' content='width=device-width'></head>"
        "<body style='margin:0'>"
        "<div style='width:400px;height:60px;background:#345'>wide</div>"
        "</body></html>",
        encoding="utf-8",
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = HTTPServer(
        ("127.0.0.1", port),
        functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with browser_slot():
            try:
                report = render_breakpoints(
                    "Test",
                    widths=[375, 768, 1280],
                    url=f"http://127.0.0.1:{port}/{html.name}",
                    out_dir=tmp_path,
                )
            except (OSError, RuntimeError) as e:
                if "chromium" in str(e).lower() or "playwright" in str(e).lower():
                    pytest.skip(f"no headless Chromium here: {e}")
                raise
    finally:
        server.shutdown()

    if all(r["status"] == "error" for r in report["results"]):
        detail = report["findings"][0]["description"] if report["findings"] else "?"
        if any(
            k in detail.lower()
            for k in ("chromium", "executable", "playwright", "browser")
        ):
            pytest.skip(f"chromium could not launch: {detail[:200]}")
        pytest.fail(f"every viewport errored: {detail[:200]}")
    by_width = {r["width"]: r for r in report["results"]}
    assert set(by_width) == {375, 768, 1280}
    assert by_width[375]["status"] == "overflow"
    assert by_width[768]["status"] == "clean"
    assert by_width[1280]["status"] == "clean"
    assert report["ok"] is False

    majors = [f for f in report["findings"] if f["severity"] == "major"]
    assert len(majors) == 1 and majors[0]["width"] == 375
    assert "400" in majors[0]["description"]
    assert not [f for f in report["findings"] if f.get("width") in (768, 1280)]

    # one screenshot per viewport width, as the issue asks
    for w, r in by_width.items():
        shot = Path(r["shot"])
        assert shot.exists() and shot.stat().st_size > 0, (
            f"missing screenshot for {w}px"
        )

    # and the real-browser finding still feeds the repair pipeline (AC3 schema)
    tasks = state.repair_tasks_from_findings(
        report["findings"], ["developer_frontend"], cycle=1
    )
    assert len(tasks) == 1 and tasks[0].id == "R1F1"
