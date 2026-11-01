"""Unit tests for the `swarmforge ask` CLI command."""
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from swarmforge import chatbot
from swarmforge.cli import main

from .conftest import base_raw


def _write_cfg(tmp_path, monkeypatch) -> Path:
    """Write a minimal config file and return its path."""
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return p


def _capture(argv: list[str]) -> tuple[str, str, int]:
    """Run main(argv) and return captured stdout, stderr and exit code."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return out.getvalue(), err.getvalue(), code


def test_ask_prints_answer_and_forwards_kwargs(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path, monkeypatch)
    recorded = {}

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12):
        recorded["question"] = question
        recorded["max_runs"] = max_runs
        recorded["conversation"] = conversation
        return {"answer": "All good.", "role": "chat", "model": "m1",
                "question": question}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    out, err, code = _capture(["--config", str(p), "ask", "how are runs doing?"])
    assert code == 0
    assert out.strip() == "All good."
    assert err == ""
    assert recorded["question"] == "how are runs doing?"
    assert recorded["max_runs"] == 12
    assert recorded["conversation"] == "cli"  # default conversation id


def test_ask_forwards_conversation_flag(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path, monkeypatch)
    recorded = {}

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12):
        recorded["conversation"] = conversation
        return {"answer": "ok", "role": "chat", "model": "m1", "question": question}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    _out, _err, code = _capture(["--config", str(p), "ask", "q", "--conversation", "thread-42"])
    assert code == 0
    assert recorded["conversation"] == "thread-42"


def test_ask_json_output(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path, monkeypatch)

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12):
        return {"answer": "2 failed.", "role": "chat", "model": "m1",
                "question": question}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    out, _err, code = _capture(
        ["--config", str(p), "ask", "which failed?", "--json", "--max-runs", "3"])
    assert code == 0
    payload = json.loads(out)
    assert payload == {"question": "which failed?", "answer": "2 failed.",
                       "role": "chat", "model": "m1"}


def test_ask_max_runs_forwarded(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path, monkeypatch)
    recorded = {}

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12):
        recorded["max_runs"] = max_runs
        return {"answer": "ok", "role": "chat", "model": "m1", "question": question}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    _, _, code = _capture(["--config", str(p), "ask", "q", "--max-runs", "5"])
    assert code == 0
    assert recorded["max_runs"] == 5


def test_ask_chat_error_returns_2(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path, monkeypatch)

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12):
        raise chatbot.ChatError("DEEPINFRA_API_KEY is not set")

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    out, err, code = _capture(["--config", str(p), "ask", "hi"])
    assert code == 2
    assert "chatbot: DEEPINFRA_API_KEY is not set" in err
    assert out == ""


def test_ask_help_mentions_max_runs(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ask", "--help"])
    assert exc.value.code == 0
    assert "--max-runs" in capsys.readouterr().out


def test_ask_help_mentions_conversation(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ask", "--help"])
    assert exc.value.code == 0
    assert "--conversation" in capsys.readouterr().out
