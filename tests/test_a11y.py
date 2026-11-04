"""Offline tests for the accessibility audit (no browser, no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarmforge import state, visual
from swarmforge.a11y import (
    CONTRAST_JS,
    INTERACTIVE_ROLES,
    A11yError,
    contrast_findings,
    contrast_ratio,
    flatten_a11y_tree,
    label_findings,
    parse_color,
)

# ------------------------------------------------------------- parse_color


def test_parse_color_hex_forms():
    assert parse_color("#000000") == (0, 0, 0)
    assert parse_color("#fff") == (255, 255, 255)
    assert parse_color("#FFFFFF") == (255, 255, 255)
    # alpha composited over white: fully opaque, fully transparent
    assert parse_color("#000000ff") == (0, 0, 0)
    assert parse_color("#00000000") == (255, 255, 255)
    assert parse_color("#00000080") == (127, 127, 127)


def test_parse_color_rgb_and_rgba():
    assert parse_color("rgb(0, 0, 0)") == (0, 0, 0)
    assert parse_color("rgb(255,255,255)") == (255, 255, 255)
    assert parse_color("rgba(0, 0, 0, 1)") == (0, 0, 0)
    assert parse_color("rgba(0, 0, 0, 0)") == (255, 255, 255)
    assert parse_color("rgba(0, 0, 0, 0.5)") == (128, 128, 128)


def test_parse_color_invalid():
    assert parse_color("transparent") is None
    assert parse_color("") is None
    assert parse_color("not-a-color") is None
    assert parse_color("#12345") is None


# ------------------------------------------------------------- contrast_ratio


def test_contrast_ratio_known_pairs():
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    ratio = contrast_ratio("#777777", "#ffffff")
    assert 4.0 < ratio < 4.6
    # symmetric
    assert contrast_ratio("#ffffff", "#777777") == pytest.approx(ratio)
    # unparseable inputs never raise
    assert contrast_ratio("bogus", "#ffffff") == 1.0
    assert contrast_ratio(None, None) == 1.0


# ------------------------------------------------------------- flatten/labels


def test_flatten_a11y_tree_depth_first_skips_none():
    snap = {
        "role": "WebArea",
        "name": "Chat",
        "children": [
            {"role": "button", "name": "Send"},
            None,
            {
                "role": "textbox",
                "name": "",
                "children": [{"role": "button", "name": ""}],
            },
        ],
    }
    roles = [n["role"] for n in flatten_a11y_tree(snap)]
    assert roles == ["WebArea", "button", "textbox", "button"]
    assert flatten_a11y_tree(None) == []
    assert flatten_a11y_tree([{"role": "link", "name": "x"}]) == [
        {"role": "link", "name": "x"}
    ]


def test_label_findings_flags_nameless_button_major():
    snap = {
        "role": "WebArea",
        "name": "Chat",
        "children": [
            {"role": "button", "name": "Send"},
            {"role": "button", "name": "   "},
            {"role": "link", "name": ""},
        ],
    }
    findings = label_findings("Chat", "light", snap)
    assert len(findings) == 2
    f = findings[0]
    assert f["severity"] == "major"
    assert f["blocking"] is True
    assert f["page"] == "chat"
    assert f["theme"] == "light"
    assert f["file"] == "src/swarmforge/dashboard_pages/chat.py"
    assert f["line"] is None
    assert f["title"] == "button without an accessible name"
    assert "recommendation" in f and "description" in f
    # named interactive elements produce no findings
    assert label_findings("Chat", "dark", snap["children"][0:1]) == []
    # non-interactive nameless nodes are ignored
    assert label_findings("Chat", "light", {"role": "generic", "name": ""}) == []


def test_label_findings_flags_slider_and_tab_roles():
    snap = {
        "role": "WebArea",
        "name": "Chat",
        "children": [
            {"role": "slider", "name": ""},
            {"role": "tab", "name": ""},
            {"role": "slider", "name": "Volume"},
        ],
    }
    findings = label_findings("Chat", "dark", snap)
    assert [f["title"] for f in findings] == [
        "slider without an accessible name",
        "tab without an accessible name",
    ]
    assert all(f["severity"] == "major" and f["blocking"] is True for f in findings)
    assert all(f["theme"] == "dark" and f["page"] == "chat" for f in findings)


def test_label_findings_description_truncated():
    children = [{"role": "checkbox", "name": ""} for _ in range(20)]
    findings = label_findings("Chat", "dark", {"role": "WebArea", "children": children})
    assert len(findings) == 20
    assert len(findings[0]["description"]) <= 1500
    assert "+12 more" in findings[0]["description"]


# ------------------------------------------------------------- contrast_findings


def test_contrast_findings_flags_low_ratio_minor():
    pairs = [
        {
            "selector": "p.muted",
            "text": "hello",
            "color": "#777777",
            "background": "#ffffff",
        },
        {
            "selector": "p.ok",
            "text": "fine",
            "color": "#000000",
            "background": "#ffffff",
        },
    ]
    findings = contrast_findings("Chat", "light", pairs)
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "minor"
    assert f["blocking"] is False
    assert f["contrast"] is True
    assert f["a11y"] is True
    assert f["page"] == "chat"
    assert f["theme"] == "light"
    assert f["file"] == "src/swarmforge/dashboard_pages/chat.py"
    assert "below WCAG AA" in f["title"]
    assert "p.muted" in f["description"]
    assert "raise the light-theme contrast of p.muted" in f["recommendation"]


def test_contrast_findings_dedupe_sort_and_cap():
    pairs = [
        {"selector": "p.a", "text": "x", "color": "#777777", "background": "#ffffff"},
        {"selector": "p.a", "text": "x", "color": "#777777", "background": "#ffffff"},
        {"selector": "p.b", "text": "y", "color": "#999999", "background": "#ffffff"},
    ] + [
        {
            "selector": f"p.n{i}",
            "text": "z",
            "color": "#888888",
            "background": "#ffffff",
        }
        for i in range(15)
    ]
    findings = contrast_findings("Chat", "dark", pairs)
    assert len(findings) == 10  # capped
    # worst-first: #999999 on white is the lowest ratio
    assert "p.b" in findings[0]["description"]
    # non-list input is tolerated
    assert contrast_findings("Chat", "dark", None) == []
    # custom min_ratio
    assert contrast_findings("Chat", "dark", pairs[:1], min_ratio=5.0)


# ------------------------------------------------------------- audit end-to-end


class FakePage:
    def __init__(self, snapshot, pairs):
        self._snapshot = snapshot
        self._pairs = pairs
        self.goto_calls: list[str] = []
        self.closed = False
        self.accessibility = SimpleNamespace(snapshot=lambda: self._snapshot)

    def goto(self, url, **kw):
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        assert js == CONTRAST_JS
        return self._pairs

    def close(self):
        self.closed = True


def test_audit_end_to_end_both_themes():
    pages: dict[str, FakePage] = {}

    def open_page(theme):
        snap = {
            "role": "WebArea",
            "name": "Chat",
            "children": [
                {"role": "button", "name": ""},  # nameless -> major
                {"role": "textbox", "name": "Prompt"},
            ],
        }
        pairs = [
            {
                "selector": "p.muted",
                "text": "hi",
                "color": "#777777",
                "background": "#ffffff",
            },
            {
                "selector": "p.dark",
                "text": "ok",
                "color": "#ffffff",
                "background": "#000000",
            },
        ]
        pages[theme] = FakePage(snap, pairs)
        return pages[theme]

    result = visual.audit(
        "http://localhost:8501/?page=Chat", open_page=open_page, wait_ms=0
    )
    assert result["ok"] is True
    assert result["url"] == "http://localhost:8501/?page=Chat"
    assert result["tab"] == "chat"
    assert result["themes"] == ["light", "dark"]
    assert result["error"] is None
    assert len(result["results"]) == 2
    assert {r["theme"] for r in result["results"]} == {"light", "dark"}
    assert all(r["error"] is None for r in result["results"])
    assert all(
        r["interactive"] == 2 and r["text_pairs"] == 2 for r in result["results"]
    )
    assert set(pages) == {"light", "dark"}
    assert all(p.closed for p in pages.values())
    # one major label finding + one minor contrast finding per theme
    majors = [f for f in result["findings"] if f["severity"] == "major"]
    minors = [f for f in result["findings"] if f["severity"] == "minor"]
    assert len(majors) == 2 and len(minors) == 2
    assert {f["theme"] for f in majors} == {"light", "dark"}
    assert majors[0]["title"] == "button without an accessible name"
    assert "below WCAG AA" in minors[0]["title"]
    # JSON-safe report shape
    assert set(result) == {"ok", "url", "tab", "themes", "results", "findings", "error"}


def test_audit_findings_feed_repair_tasks():
    snap = {
        "role": "WebArea",
        "name": "Chat",
        "children": [{"role": "button", "name": ""}],
    }
    pairs = [
        {
            "selector": "p.muted",
            "text": "hi",
            "color": "#777777",
            "background": "#ffffff",
        }
    ]

    def open_page(theme):
        return FakePage(snap, pairs)

    result = visual.audit(
        "http://localhost:8501/?page=Chat", open_page=open_page, wait_ms=0
    )
    tasks = state.repair_tasks_from_findings(
        result["findings"],
        ["developer_frontend"],
        cycle=1,
        scope_files={"src/swarmforge/dashboard_pages/chat.py"},
    )
    # the major label finding becomes a repair task (minors are not blocking)
    assert len(tasks) == 1
    assert tasks[0].id == "R1F1"
    assert "button without an accessible name" in tasks[0].title
    assert tasks[0].expected_files == ["src/swarmforge/dashboard_pages/chat.py"]


def test_audit_per_theme_error_recorded_not_raised():
    def open_page(theme):
        if theme == "dark":
            raise RuntimeError("boom")
        return FakePage({"role": "WebArea", "name": "Chat", "children": []}, [])

    result = visual.audit("http://localhost/x/chat", open_page=open_page, wait_ms=0)
    assert result["ok"] is True  # light theme succeeded
    assert result["tab"] == "chat"  # last path segment fallback
    dark = next(r for r in result["results"] if r["theme"] == "dark")
    assert dark["error"] == "boom"
    assert result["error"] is not None and "boom" in result["error"]


def test_audit_all_themes_error_ok_false():
    def open_page(theme):
        raise RuntimeError("no browser")

    result = visual.audit("http://localhost/", open_page=open_page)
    assert result["ok"] is False
    assert result["findings"] == []
    assert "no browser" in result["error"]


def test_audit_snapshot_missing_records_error():
    class NoSnapPage(FakePage):
        def __init__(self):
            super().__init__(None, [])
            self.accessibility = SimpleNamespace(snapshot=lambda: None)

    result = visual.audit(
        "http://localhost/?page=Chat",
        open_page=lambda theme: NoSnapPage(),
        wait_ms=0,
    )
    assert result["ok"] is False  # every theme errored
    assert all("no data" in (r["error"] or "") for r in result["results"])
    assert result["findings"] == []


def test_audit_validates_arguments():
    with pytest.raises(A11yError):
        visual.audit("ftp://localhost/")
    with pytest.raises(A11yError):
        visual.audit("http://localhost/", themes=("light", "sepia"))
    with pytest.raises(A11yError):
        visual.audit("http://localhost/", themes=())


def test_audit_playwright_import_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pw(name, *a, **kw):
        if "playwright" in name:
            raise ImportError("no playwright")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pw)
    with pytest.raises(A11yError, match="playwright is not installed"):
        visual.audit("http://localhost/")


def test_visual_audit_delegates_to_a11y_audit(monkeypatch):
    from swarmforge import a11y

    seen = {}

    def fake_audit(tab_url, **kwargs):
        seen["url"], seen["kwargs"] = tab_url, kwargs
        return {"ok": True}

    monkeypatch.setattr(a11y, "audit", fake_audit)
    result = visual.audit("http://localhost/?page=Chat", wait_ms=5)
    assert result == {"ok": True}
    assert seen["url"] == "http://localhost/?page=Chat"
    assert seen["kwargs"] == {"wait_ms": 5}


def test_interactive_roles_contents():
    assert "button" in INTERACTIVE_ROLES
    assert "checkbox" in INTERACTIVE_ROLES
    assert "generic" not in INTERACTIVE_ROLES
