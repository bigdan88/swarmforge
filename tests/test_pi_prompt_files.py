"""Large Pi inputs must never become Windows/Linux process arguments; no real models or Docker."""
import json
from pathlib import Path

import pytest

from swarmforge.agent import pi_runtime as pr
from swarmforge.agent.devtools import DockerSession, LocalSession
from swarmforge.agent.tools import TerminalTool, ToolContext
from swarmforge.config import SandboxConfig
from swarmforge.sandbox import CommandResult, DockerSandbox, LocalSandbox
from .test_pi import _llm


@pytest.mark.parametrize("docker", [False, True])
async def test_large_prompts_are_loaded_from_files_by_pi(cfg, tmp_path, monkeypatch, docker):
    system = "system context\n" * 20000
    prompt = "task context with quotes ' \" and unicode \u2603\n" * 20000
    final = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]}})
    calls = []
    def verify(argv, system_path, prompt_path):
        assert max(map(len, argv)) < 2000
        assert system not in " ".join(argv) and prompt not in " ".join(argv)
        assert system_path.read_text(encoding="utf-8") == system
        assert prompt_path.read_text(encoding="utf-8") == prompt
        calls.append(argv)
    class FakeDocker(DockerSession):
        def __init__(self): pass
        async def exec(self, *args, **kwargs):
            return CommandResult("setup", 0, "", 0)
        async def exec_argv(self, argv, **kwargs):
            env = kwargs["env"]
            local = lambda name: tmp_path / env[name].removeprefix("/workspace/")
            verify(argv, local("SF_SYS"), local("SF_PROMPT"))
            assert '"$SF_SYS" -- "@$SF_PROMPT"' in argv[2]
            assert "$(cat" not in argv[2]
            local("SF_OUT").write_text(final, encoding="utf-8")
            return CommandResult("pi", 0, "", 0)
    class FakeLocal(LocalSession):
        def __init__(self): pass
    async def fake_local(argv, cwd, env, out_file, err_file, timeout):
        verify(argv, Path(argv[argv.index("--append-system-prompt") + 1]), Path(argv[-1].removeprefix("@")))
        out_file.write_text(final, encoding="utf-8")
        return False
    monkeypatch.setattr("swarmforge.agent.devtools.open_session", lambda _: FakeDocker() if docker else FakeLocal())
    monkeypatch.setattr(pr, "_run_local", fake_local)
    llm, _ = _llm(cfg, lambda _: {"content": "not called"})
    relay = pr.ModelRelay(llm)
    sandbox = DockerSandbox(SandboxConfig()) if docker else LocalSandbox(SandboxConfig(mode="local"))
    ctx = ToolContext(workspace=tmp_path, sandbox=sandbox)
    terminal = TerminalTool("finish", "", {}, lambda a: (a, None))
    result = await pr.run_pi_agent(relay=relay, ctx=ctx, role="developer_backend", task_key="T1", system=system,
                                  prompt=prompt, terminal=terminal, max_steps=10, timeout_s=60, max_tokens=4096,
                                  thinking=None, sf_tools=False)
    assert result.status == "completed", result.error
    assert calls and result.steps == 0  # no actual model or container was used
