"""CLI wiring for the visual-regression gate: `swarmforge visual regress|update-golden`.

Offline: the browser capture is monkeypatched, so no Chromium is needed here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarmforge import cli, visual
from swarmforge.visual import DIFF_THRESHOLD, RegressError
from tests.test_visual_regress import (
    change_colour,
    fake_capture,
    make_repo,
    reformat_only,
)


# ------------------------------------------------------------------ parser
def test_parser_visual_regress_defaults():
    args = cli.build_parser().parse_args(
        [
            "visual",
            "regress",
            "-b",
            "feat",
            "--base",
            "main",
            "--threshold",
            "0.05",
            "--json",
        ]
    )
    assert args.visual_cmd == "regress"
    assert args.branch == "feat"
    assert args.base == "main"
    assert args.threshold == 0.05
    assert args.repo == "."
    assert args.json is True
    assert args.tabs is None and args.port is None
    assert args.fn is cli.cmd_visual


def test_parser_visual_regress_defaults_threshold():
    args = cli.build_parser().parse_args(["visual", "regress", "-b", "feat"])
    assert args.base == "main"
    assert args.threshold == DIFF_THRESHOLD


def test_parser_visual_update_golden_defaults():
    args = cli.build_parser().parse_args(
        ["visual", "update-golden", "--branch", "main"]
    )
    assert args.visual_cmd == "update-golden"
    assert args.branch == "main"
    assert args.repo == "."
    assert args.fn is cli.cmd_visual


# ------------------------------------------------------------------ cmd_visual
def _ok_result(ok=True):
    return {
        "ok": ok,
        "threshold": DIFF_THRESHOLD,
        "max_score": 0.0,
        "tabs": [{"tab": "work", "theme": "light", "score": 0.0, "status": "clean"}],
        "findings": []
        if ok
        else [
            {
                "severity": "major",
                "page": "work",
                "theme": "light",
                "blocking": True,
                "title": "visual regression on tab 'work' (light): 5.0% of pixels differ (> 2%)",
                "description": "d",
                "recommendation": "r",
            }
        ],
        "shots": {},
    }


def test_cmd_visual_regress_exit_codes(monkeypatch, capsys):
    calls = {}

    def fake_regress(pr_branch, base_branch, **kw):
        calls.update(branch=pr_branch, base=base_branch, **kw)
        return _ok_result(ok=False)

    monkeypatch.setattr(visual, "regress", fake_regress)
    args = cli.build_parser().parse_args(
        ["visual", "regress", "-b", "feat", "--base", "main"]
    )
    assert cli.cmd_visual(args, None) == 1
    assert calls == {
        "branch": "feat",
        "base": "main",
        "repo": ".",
        "threshold": DIFF_THRESHOLD,
        "tabs": None,
        "port": None,
    }
    out = capsys.readouterr().out
    assert "work" in out and "major" in out

    monkeypatch.setattr(visual, "regress", lambda *a, **kw: _ok_result(ok=True))
    assert cli.cmd_visual(args, None) == 0


def test_cmd_visual_regress_tabs_whitespace(monkeypatch):
    calls = {}

    def fake_regress(pr_branch, base_branch, **kw):
        calls.update(branch=pr_branch, base=base_branch, **kw)
        return _ok_result(ok=True)

    monkeypatch.setattr(visual, "regress", fake_regress)
    args = cli.build_parser().parse_args(
        ["visual", "regress", "-b", "feat", "--tabs", "work, chat"]
    )
    assert cli.cmd_visual(args, None) == 0
    assert calls["tabs"] == ["work", "chat"]

    calls.clear()
    args = cli.build_parser().parse_args(
        ["visual", "regress", "-b", "feat", "--tabs", " , , "]
    )
    assert cli.cmd_visual(args, None) == 0
    assert calls["tabs"] is None


def test_cmd_visual_regress_json(monkeypatch, capsys):
    monkeypatch.setattr(visual, "regress", lambda *a, **kw: _ok_result(ok=False))
    args = cli.build_parser().parse_args(["visual", "regress", "-b", "feat", "--json"])
    assert cli.cmd_visual(args, None) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["findings"] and data["findings"][0]["severity"] == "major"


def test_cmd_visual_regress_error(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RegressError("branch 'nope' does not exist")

    monkeypatch.setattr(visual, "regress", boom)
    args = cli.build_parser().parse_args(["visual", "regress", "-b", "nope"])
    assert cli.cmd_visual(args, None) == 2
    assert "nope" in capsys.readouterr().err


def test_cmd_visual_update_golden(monkeypatch, capsys):
    calls = {}

    def fake_update(*, repo=".", branch=None, tabs=None, port=None):
        calls.update(repo=repo, branch=branch, tabs=tabs, port=port)
        return [Path("tests/golden/work-light.png"), Path("tests/golden/work-dark.png")]

    monkeypatch.setattr(visual, "update_golden", fake_update)
    args = cli.build_parser().parse_args(
        [
            "visual",
            "update-golden",
            "--repo",
            "/tmp/r",
            "--branch",
            "main",
            "--tabs",
            "work,chat",
        ]
    )
    assert cli.cmd_visual(args, None) == 0
    assert calls == {
        "repo": "/tmp/r",
        "branch": "main",
        "tabs": ["work", "chat"],
        "port": None,
    }
    out = capsys.readouterr().out
    assert "work-light.png" in out and "2 golden image(s) refreshed" in out


def test_cmd_visual_update_golden_error(monkeypatch, capsys):
    def boom(**kw):
        raise RegressError("no browser")

    monkeypatch.setattr(visual, "update_golden", boom)
    args = cli.build_parser().parse_args(["visual", "update-golden"])
    assert cli.cmd_visual(args, None) == 2
    assert "no browser" in capsys.readouterr().err


# ------------------------------------------- AC1/AC2 end-to-end via cmd_visual
def _run_cli(repo: Path, branch: str) -> int:
    args = cli.build_parser().parse_args(
        [
            "visual",
            "regress",
            "-b",
            branch,
            "--base",
            "main",
            "--repo",
            str(repo),
            "--tabs",
            "work",
            "--port",
            "8712",
        ]
    )
    return cli.cmd_visual(args, None)


def test_cmd_visual_blocks_colour_change(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    change_colour(repo, "#111111")
    monkeypatch.setattr(visual, "capture_tabs", fake_capture)
    assert _run_cli(repo, "feature") == 1
    assert "major" in capsys.readouterr().out


def test_cmd_visual_allows_whitespace_reformat(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    reformat_only(repo)
    monkeypatch.setattr(visual, "capture_tabs", fake_capture)
    assert _run_cli(repo, "feature") == 0


def test_cmd_visual_unusable_branch(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(visual, "capture_tabs", fake_capture)
    args = cli.build_parser().parse_args(
        [
            "visual",
            "regress",
            "-b",
            "does-not-exist",
            "--base",
            "main",
            "--repo",
            str(repo),
        ]
    )
    assert cli.cmd_visual(args, None) == 2
    assert capsys.readouterr().err.strip() != ""


# ------------------------------------------------------------------ cmd_update hook
def _updated_result():
    return {
        "old": "aaaaaaa1",
        "new": "bbbbbbb2",
        "behind": 2,
        "ahead": 0,
        "commits": ["bbbbbbb fix"],
        "deps": None,
        "images_stale": False,
        "merged": True,
        "message": "Updated aaaaaaa1 → bbbbbbb2 (2 new commit(s)).",
    }


def test_cmd_update_refreshes_goldens_after_update(cfg, monkeypatch, capsys):
    from swarmforge import selfupdate as su

    monkeypatch.setattr(su, "update", lambda *a, **kw: _updated_result())
    monkeypatch.setattr(
        su, "request_worker_restart", lambda store: "worker restart requested"
    )
    seen = {}

    def fake_refresh(repo=".", **kw):
        seen["repo"] = repo
        return {"refreshed": True, "files": ["tests/golden/work-light.png"]}

    monkeypatch.setattr(visual, "maybe_refresh_golden_after_update", fake_refresh)
    assert cli.cmd_update(None, cfg) == 0
    assert seen["repo"] == su.ROOT
    assert "golden screenshots refreshed (1 file(s))" in capsys.readouterr().out


def test_cmd_update_refresh_failure_never_breaks_update(cfg, monkeypatch, capsys):
    from swarmforge import selfupdate as su

    monkeypatch.setattr(su, "update", lambda *a, **kw: _updated_result())
    monkeypatch.setattr(
        su, "request_worker_restart", lambda store: "worker restart requested"
    )

    def boom(*a, **kw):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(visual, "maybe_refresh_golden_after_update", boom)
    assert cli.cmd_update(None, cfg) == 0
    assert "browser exploded" in capsys.readouterr().out


def test_cmd_update_no_refresh_when_already_up_to_date(cfg, monkeypatch, capsys):
    from swarmforge import selfupdate as su

    res = _updated_result()
    res.update(behind=0, new=res["old"], message="Already up to date.", commits=[])
    monkeypatch.setattr(su, "update", lambda *a, **kw: res)
    called = []
    monkeypatch.setattr(
        visual,
        "maybe_refresh_golden_after_update",
        lambda *a, **kw: called.append(1) or None,
    )
    assert cli.cmd_update(None, cfg) == 0
    assert called == []
    assert "golden" not in capsys.readouterr().out


# ------------------------------------------------------------- check-layout
def _layout_report(ok=True):
    return {
        "ok": ok,
        "tab": "chat",
        "widths": [375, 768, 1280],
        "results": [
            {
                "width": w,
                "shot": f"/tmp/layout/chat-{w}px-light.png",
                "status": "clean",
                "doc_scroll_width": 360,
                "overflowing": [],
                "clipped": [],
            }
            for w in (375, 768, 1280)
        ],
        "findings": []
        if ok
        else [
            {
                "severity": "major",
                "file": "chat (375px)",
                "page": "chat",
                "theme": "light",
                "width": 375,
                "layout_overflow": True,
                "blocking": True,
                "title": "overflow at 375px: element wider than viewport",
                "description": "d",
                "recommendation": "r",
            }
        ],
        "error": None,
    }


def test_parser_visual_check_layout():
    args = cli.build_parser().parse_args(
        ["visual", "check-layout", "--tab", "chat", "--widths", "375,768,1280"]
    )
    assert args.fn is cli.cmd_visual
    assert args.visual_cmd == "check-layout"
    assert args.tab == "chat"
    assert args.widths == "375,768,1280"
    assert args.repo == "."
    assert args.url is None
    assert args.out is None
    assert args.port is None

    args = cli.build_parser().parse_args(["visual", "check-layout", "--tab", "chat"])
    assert args.widths == "375,768,1280"


def test_cmd_visual_check_layout_dispatch(monkeypatch, capsys):
    calls = {}

    def fake_render(tab, widths=None, **kw):
        calls.update(tab=tab, widths=widths, **kw)
        return _layout_report(ok=True)

    monkeypatch.setattr(visual, "render_breakpoints", fake_render)
    args = cli.build_parser().parse_args(
        ["visual", "check-layout", "--tab", "chat", "--widths", "375,768,1280"]
    )
    assert cli.cmd_visual(args, None) == 0
    assert calls == {
        "tab": "chat",
        "widths": [375, 768, 1280],
        "repo": ".",
        "url": None,
        "out_dir": None,
        "port": None,
    }
    out = capsys.readouterr().out
    assert json.loads(out) == _layout_report(ok=True)

    # failing report -> exit 1
    monkeypatch.setattr(
        visual, "render_breakpoints", lambda *a, **kw: _layout_report(ok=False)
    )
    assert cli.cmd_visual(args, None) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is False
    assert json.loads(out)["findings"][0]["severity"] == "major"


def test_cmd_visual_check_layout_issue_invocation(monkeypatch, capsys):
    """The exact shape from the issue: swarmforge visual check-layout --tab chat
    --widths 375,768,1280 prints a JSON report with the documented keys."""
    monkeypatch.setattr(
        visual, "render_breakpoints", lambda *a, **kw: _layout_report(ok=True)
    )
    args = cli.build_parser().parse_args(
        ["visual", "check-layout", "--tab", "chat", "--widths", "375,768,1280"]
    )
    assert cli.cmd_visual(args, None) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) >= {"ok", "tab", "widths", "results", "findings"}


@pytest.mark.parametrize("bad", ["375,abc", "", "100", "375,,768,x"])
def test_cmd_visual_check_layout_bad_widths(bad, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        visual,
        "render_breakpoints",
        lambda *a, **kw: called.append((a, kw)) or _layout_report(),
    )
    args = cli.build_parser().parse_args(
        ["visual", "check-layout", "--tab", "chat", "--widths", bad]
    )
    assert cli.cmd_visual(args, None) == 2
    assert called == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()


def test_cmd_visual_check_layout_regress_error(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RegressError("no browser found")

    monkeypatch.setattr(visual, "render_breakpoints", boom)
    args = cli.build_parser().parse_args(
        ["visual", "check-layout", "--tab", "chat", "--widths", "375,768,1280"]
    )
    assert cli.cmd_visual(args, None) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "visual check-layout: no browser found" in captured.err
