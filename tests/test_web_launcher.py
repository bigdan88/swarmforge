"""Startup diagnostics run even when optional interface packages are absent."""
import argparse
import builtins
import sys
from types import SimpleNamespace

import pytest

from swarmforge.webapp import __main__ as launcher


def test_missing_interface_dependency_identifies_the_active_python(monkeypatch, capsys):
    def missing(name):
        raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
    monkeypatch.setattr(launcher.importlib, "import_module", missing)
    with pytest.raises(SystemExit) as error:
        launcher._interface_dependencies(argparse.ArgumentParser())
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "fastapi" in message and sys.executable in message
    assert 'python -m pip install -e ".[interface]"' in message


def test_pydantic_one_gets_a_version_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(launcher.importlib, "import_module", lambda name: SimpleNamespace(BaseModel=object))
    with pytest.raises(SystemExit):
        launcher._interface_dependencies(argparse.ArgumentParser())
    assert "Pydantic 2" in capsys.readouterr().err


def test_internal_import_failure_is_not_mislabeled_as_missing_dependencies(monkeypatch):
    monkeypatch.setattr(launcher, "_interface_dependencies", lambda parser: SimpleNamespace())
    monkeypatch.setattr(sys, "argv", ["react_dashboard.py", "--demo"])
    original = builtins.__import__
    def broken_app(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app" and level == 1:
            raise ImportError("Internal application import failure")
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", broken_app)
    with pytest.raises(ImportError, match="Internal application import failure"):
        launcher.main()
