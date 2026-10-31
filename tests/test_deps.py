from pathlib import Path

from swarmforge.deps import ensure_deps, plan_install
from swarmforge.sandbox import DEPS_CONTAINER_PATH, CommandResult, Sandbox


class RecordingSandbox(Sandbox):
    def __init__(self, exit_code=0):
        self.calls = []
        self.exit_code = exit_code

    async def run(self, workspace, command, *, timeout_s=None, artifacts_dir=None, network=None, cache_dir=None):
        self.calls.append({"command": command, "network": network, "cache_dir": cache_dir})
        return CommandResult(command, self.exit_code, "ok", 0.1)

    def describe(self):
        return "recording"


def test_plan_from_requirements_and_pyproject(tmp_path):
    assert plan_install(tmp_path).command is None
    (tmp_path / "requirements.txt").write_text("requests>=2\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["httpx>=0.27"]\n[project.optional-dependencies]\ndev=["pytest>=8"]\n')
    p = plan_install(tmp_path)
    assert f"--target {DEPS_CONTAINER_PATH}" in p.command
    assert "-r requirements.txt" in p.command and "'httpx>=0.27'" in p.command and "'pytest>=8'" in p.command
    assert p.sources == ["requirements.txt", "pyproject.toml"]
    p2 = plan_install(tmp_path, install_command="pip install -e . --target x")
    assert p2.command == "pip install -e . --target x" and p2.fingerprint != p.fingerprint


async def test_ensure_deps_uses_network_once_and_caches(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    sb = RecordingSandbox()
    status, _, _ = await ensure_deps(sb, tmp_path, network="bridge", cache_dir=tmp_path / "cache")
    assert status == "installed" and sb.calls[0]["network"] == "bridge"
    status, _, _ = await ensure_deps(sb, tmp_path)
    assert status == "current" and len(sb.calls) == 1          # unchanged -> no reinstall
    (tmp_path / "requirements.txt").write_text("requests\nrich\n")
    status, _, _ = await ensure_deps(sb, tmp_path)
    assert status == "installed" and len(sb.calls) == 2         # changed -> reinstall
    bad = RecordingSandbox(exit_code=1)
    (tmp_path / "requirements.txt").write_text("nope\n")
    status, res, _ = await ensure_deps(bad, tmp_path)
    assert status == "failed" and res.exit_code == 1


def test_docker_args_for_install_and_pythonpath(tmp_path):
    from swarmforge.config import SandboxConfig
    from swarmforge.sandbox import DockerSandbox
    sb = DockerSandbox(SandboxConfig())
    normal = sb.build_args(tmp_path, "pytest", "n")
    assert normal[normal.index("--network") + 1] == "none"
    assert f"PYTHONPATH={DEPS_CONTAINER_PATH}" in normal
    inst = sb.build_args(tmp_path, "pip install x", "n", network="bridge", cache_dir=tmp_path / "c")
    assert inst[inst.index("--network") + 1] == "bridge"
    assert "--cap-drop" in inst and "--read-only" in inst and "PIP_CACHE_DIR=/pipcache" in inst
