"""CLI `swarmforge dashboard --theme` flag tests (no streamlit launched)."""
import argparse

import yaml

from swarmforge.cli import build_parser, cmd_dashboard
from swarmforge.config import load_config

from .conftest import base_raw


def _cfg(tmp_path, monkeypatch):
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return load_config(str(p))


def _ns(**kw):
    d = {"port": 8501, "theme": "auto", "lan": False, "no_browser": True}
    d.update(kw)
    return argparse.Namespace(**d)


def _capture(monkeypatch):
    calls = []

    def fake_call(cmd, env=None, **kwargs):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr("swarmforge.cli.subprocess.call", fake_call)
    monkeypatch.setattr("swarmforge.cli.shutil.which", lambda name: "streamlit")
    return calls


def test_dashboard_theme_default_auto(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    rc = cmd_dashboard(_ns(), _cfg(tmp_path, monkeypatch))
    assert rc == 0
    cmd, env = calls[0]
    assert cmd[cmd.index("--theme.base") + 1] == "auto"
    assert env["SWARMFORGE_DASH_THEME"] == "auto"
    # existing flags preserved
    assert "--server.port" in cmd and cmd[cmd.index("--server.port") + 1] == "8501"
    assert "--server.headless" in cmd and "--client.toolbarMode" in cmd


def test_dashboard_theme_dark(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    rc = cmd_dashboard(_ns(theme="dark", port=8600), _cfg(tmp_path, monkeypatch))
    assert rc == 0
    cmd, env = calls[0]
    assert cmd[cmd.index("--theme.base") + 1] == "dark"
    assert env["SWARMFORGE_DASH_THEME"] == "dark"
    assert cmd[cmd.index("--server.port") + 1] == "8600"


def test_dashboard_theme_missing_attr_defaults_auto(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    ns = argparse.Namespace(port=8501, lan=False, no_browser=True)
    rc = cmd_dashboard(ns, _cfg(tmp_path, monkeypatch))
    assert rc == 0
    cmd, env = calls[0]
    assert cmd[cmd.index("--theme.base") + 1] == "auto"
    assert env["SWARMFORGE_DASH_THEME"] == "auto"


def test_parser_accepts_and_rejects_theme():
    parser = build_parser()
    args = parser.parse_args(["dashboard", "--theme", "dark"])
    assert args.theme == "dark"
    args = parser.parse_args(["dashboard"])
    assert args.theme == "auto"
    try:
        parser.parse_args(["dashboard", "--theme", "sepia"])
    except SystemExit:
        pass
    else:
        raise AssertionError("invalid theme should be rejected")
