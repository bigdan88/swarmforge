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
    assert p.command == "python .swarmforge/sf_deps.py"
    assert p.sources == ["requirements.txt", "pyproject.toml"]
    from swarmforge.deps import _write_helper
    import json
    _write_helper(tmp_path)
    spec = json.loads((tmp_path / ".swarmforge" / "sf_deps.json").read_text())
    assert spec["req_files"] == ["requirements.txt"] and spec["file_reqs_parsed"] is True
    assert set(spec["requirements"]) == {"requests>=2", "httpx>=0.27", "pytest>=8"}
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


async def test_npm_setup_detects_manifest_and_lockfile(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "scaffold", "dependencies": {"react": "^18"}}')
    sandbox = RecordingSandbox()
    status, _, plan = await ensure_deps(sandbox, tmp_path, network="bridge")
    assert status == "installed"
    assert plan.command.startswith("npm install --no-package-lock")
    assert sandbox.calls[0]["network"] == "bridge"
    assert (await ensure_deps(sandbox, tmp_path))[0] == "current"
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
    status, _, locked_plan = await ensure_deps(sandbox, tmp_path)
    assert status == "installed" and locked_plan.command.startswith("npm ci ")
    assert locked_plan.fingerprint != plan.fingerprint
    assert plan_install(tmp_path, "custom install").command == "custom install"


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


async def test_helper_skips_when_already_satisfied_and_installs_otherwise(tmp_path):
    """Runs the real helper script with the local sandbox: pytest is installed in this env."""
    from swarmforge.config import SandboxConfig
    from swarmforge.sandbox import LocalSandbox
    import sys
    sb = LocalSandbox(SandboxConfig(mode="local"))
    (tmp_path / "requirements.txt").write_text("pytest>=1\n")
    from swarmforge import deps as d
    real = d.plan_install

    def plan_with_this_python(ws, install_command=None):  # use this interpreter, not PATH's python
        p = real(ws, install_command)
        if p.command:
            p.command = f'"{sys.executable}" .swarmforge/sf_deps.py'
        return p
    d.plan_install = plan_with_this_python
    try:
        status, res, _ = await ensure_deps(sb, tmp_path)
    finally:
        d.plan_install = real
    assert status == "satisfied", res.output
    # unsatisfiable without network -> real pip attempt that fails (no index reachable offline is fine)
    (tmp_path / "requirements.txt").write_text("./does-not-exist.whl\n")
    d.plan_install = plan_with_this_python
    try:
        status, res, _ = await ensure_deps(sb, tmp_path)
    finally:
        d.plan_install = real
    assert status == "failed"
