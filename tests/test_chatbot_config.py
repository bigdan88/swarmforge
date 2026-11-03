"""Tests for the chatbot.enable_shell config flag."""

import pytest

from swarmforge.config import ConfigError, parse_config
from tests.conftest import base_raw


def test_default_false(tmp_path):
    raw = base_raw(tmp_path / "work")
    assert "chatbot" not in raw
    cfg = parse_config(raw)
    assert cfg.chatbot.enable_shell is False


def test_enable_shell_true(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": True}
    cfg = parse_config(raw)
    assert cfg.chatbot.enable_shell is True


def test_bad_type_raises(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": "yes"}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_unknown_key_raises(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": True, "other": 1}
    with pytest.raises(ConfigError):
        parse_config(raw)
