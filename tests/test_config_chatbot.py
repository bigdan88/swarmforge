"""Tests for the top-level `chatbot` config section."""
import pytest

from swarmforge.config import ConfigError, parse_config
from tests.conftest import base_raw


def test_default_tools(tmp_path):
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chatbot.tools is None


def test_empty_tools(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": []}
    cfg = parse_config(raw)
    assert cfg.chatbot.tools == []


def test_explicit_tools_preserved(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": ["fetch_url", "search"]}
    cfg = parse_config(raw)
    assert cfg.chatbot.tools == ["fetch_url", "search"]


def test_unknown_key_raises(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"typo": 1}
    with pytest.raises(ConfigError):
        parse_config(raw)
