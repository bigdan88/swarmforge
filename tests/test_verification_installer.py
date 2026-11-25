"""Exercise installer state detection with temporary local Git fixtures, no network."""
import difflib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(SHELL is None, reason="PowerShell is unavailable")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / "repository"
    bundle = tmp_path / "bundle"
    repo.mkdir()
    bundle.mkdir()
    for arguments in (("init", "-q"), ("config", "core.autocrlf", "false"),
                      ("remote", "add", "origin", "https://github.com/bigdan88/swarmforge.git")):
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)
    for filename, before in (("swarmforge-product-verification.patch", "base\n"),
                             ("swarmforge-product-verification-fix.patch", "installed with bug\n")):
        patch = "".join(difflib.unified_diff(before.splitlines(True), ["fixed\n"],
                                            fromfile="a/app.txt", tofile="b/app.txt"))
        (bundle / filename).write_text(patch, encoding="utf-8", newline="\n")
    shutil.copyfile(ROOT / "scripts/apply_product_verification.ps1", bundle / "apply.ps1")
    return repo, bundle


def invoke(installation, run_tests=False):
    repo, bundle = installation
    command = [SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(bundle / "apply.ps1"),
               "-Repository", str(repo), "-PythonExecutable", sys.executable]
    if run_tests:
        command.append("-RunTests")
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)


@pytest.mark.parametrize("initial,message", [("base\n", "Applied product verification changes"),
                                             ("installed with bug\n", "Applied the product verification repair"),
                                             ("fixed\n", "already installed")])
def test_installer_handles_fresh_prior_and_current_installations(installation, initial, message):
    repo, _ = installation
    (repo / "app.txt").write_text(initial, encoding="utf-8", newline="\n")
    unrelated = repo / "my-uncommitted-work.txt"
    unrelated.write_text("retain my edits", encoding="utf-8")
    first = invoke(installation)
    assert first.returncode == 0, first.stdout + first.stderr
    assert message in first.stdout
    assert (repo / "app.txt").read_text() == "fixed\n"
    assert unrelated.read_text() == "retain my edits"
    second = invoke(installation)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already installed" in second.stdout
    assert "patch does not apply" not in second.stdout + second.stderr
    assert (repo / "app.txt").read_text() == "fixed\n"


def test_installer_stops_on_conflicting_edits_without_changing_them(installation):
    repo, _ = installation
    (repo / "app.txt").write_text("my conflicting edits\n", encoding="utf-8", newline="\n")
    result = invoke(installation)
    assert result.returncode != 0
    assert "No patch was applied" in result.stdout + result.stderr
    assert (repo / "app.txt").read_text() == "my conflicting edits\n"


def test_failed_validation_can_be_retried_without_reapplying_patch(installation):
    repo, _ = installation
    (repo / "app.txt").write_text("installed with bug\n", encoding="utf-8", newline="\n")
    # This intentionally minimal repository has no SwarmForge test files, so
    # validation fails after patching. A second invocation must only retest.
    first = invoke(installation, run_tests=True)
    assert first.returncode != 0
    assert "validation failed" in first.stdout + first.stderr
    assert (repo / "app.txt").read_text() == "fixed\n"
    second = invoke(installation, run_tests=True)
    assert second.returncode != 0
    assert "already installed" in second.stdout
    assert "validation failed" in second.stdout + second.stderr
    assert "patch does not apply" not in second.stdout + second.stderr
    assert (repo / "app.txt").read_text() == "fixed\n"
