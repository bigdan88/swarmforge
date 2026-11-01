"""Tests for the default sandbox Dockerfile.

The preflight sandbox self-check (swarmforge/preflight.py: SANDBOX_REQUIRED_TOOLS)
runs `which ruff/git/curl` inside the built image, so the Dockerfile must install
all of them. These tests parse the resource file as text — no docker needed.
"""

from pathlib import Path

import swarmforge

REQUIRED_TOOLS = ("ruff", "git", "curl")


def _dockerfile_path() -> Path:
    try:
        from importlib.resources import files

        return Path(str(files("swarmforge") / "resources" / "Dockerfile.sandbox"))
    except (ImportError, FileNotFoundError, ModuleNotFoundError):
        return Path(swarmforge.__file__).parent / "resources" / "Dockerfile.sandbox"


def _read_dockerfile() -> str:
    path = _dockerfile_path()
    assert path.exists(), f"sandbox Dockerfile not found at {path}"
    return path.read_text()


def test_dockerfile_installs_all_required_tools() -> None:
    text = _read_dockerfile()
    for tool in REQUIRED_TOOLS:
        assert tool in text, (
            f"sandbox Dockerfile must install '{tool}': preflight self-checks it "
            f"inside the image (SANDBOX_REQUIRED_TOOLS)"
        )


def test_dockerfile_pip_installs_ruff() -> None:
    text = _read_dockerfile()
    pip_lines = [line for line in text.splitlines() if "pip install" in line]
    assert any("ruff" in line for line in pip_lines), (
        "sandbox Dockerfile must install ruff on a pip install line "
        "(preflight self-checks it inside the image)"
    )


def test_dockerfile_apt_installs_git_and_curl() -> None:
    text = _read_dockerfile()
    apt_lines = [line for line in text.splitlines() if "apt-get install" in line]
    assert apt_lines, "sandbox Dockerfile must have an apt-get install line"
    for tool in ("git", "curl"):
        assert any(tool in line for line in apt_lines), (
            f"sandbox Dockerfile must install '{tool}' on the apt-get install line "
            "(preflight self-checks it inside the image)"
        )


def test_dockerfile_mentions_preflight_self_check() -> None:
    text = _read_dockerfile()
    assert "preflight" in text.lower(), (
        "sandbox Dockerfile should note that preflight self-checks these tools "
        "(swarmforge/preflight.py: SANDBOX_REQUIRED_TOOLS)"
    )
