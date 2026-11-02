"""Acceptance tests for the issue "Pi harness: loadable skills/playbooks from the knowledge base".

Maps to the acceptance criteria AC1..AC4. Fully offline: no network, no database,
no docker, no Pi binary (socket/sqlite are even monkeypatched off in the AC4 guard test).
"""

from __future__ import annotations

import dataclasses
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from swarmforge import pi_skills, prompts
from swarmforge.agent import tools as agent_tools
from swarmforge.agent.navtools import NAV_TOOLS
from swarmforge.agent.pi_runtime import SF_TOOLS, ModelRelay
from swarmforge.agent.tools import ToolContext
from swarmforge.config import SandboxConfig
from swarmforge.knowledge import Vault
from swarmforge.resources import sf as SF
from swarmforge.sandbox import LocalSandbox

# The four known pitfalls the issue requires to be seeded (AC3), each with markers that
# must appear in its playbook text.
PITFALL_SKILLS = {
    "apptest-button-key": ("b.key", "proto.key"),
    "apptest-file-uploader": ("file_uploader", "at.get("),
    "material-icon-format": (":material/", "snake_case"),
    "cap-truncation": ("_cap", "truncat"),
}
SEED_NAMES = tuple(PITFALL_SKILLS)


# --------------------------------------------------------------------------- AC1
def test_ac1_skill_is_a_dataclass_with_registry_fields(tmp_path):
    """AC1: pi_skills defines a Skill dataclass with the registry fields."""
    assert dataclasses.is_dataclass(pi_skills.Skill)
    field_names = {f.name for f in dataclasses.fields(pi_skills.Skill)}
    assert {
        "name",
        "title",
        "category",
        "summary",
        "playbook",
        "sources",
    } <= field_names
    reg = pi_skills.get_registry(tmp_path / "no-vault")
    skill = reg["cap-truncation"]
    assert isinstance(skill, pi_skills.Skill)
    with pytest.raises(dataclasses.FrozenInstanceError):
        skill.name = "mutated"  # type: ignore[misc]


def test_ac1_registry_built_in_seeds_without_vault(tmp_path):
    """AC1: registry loads built-in seeds even when the knowledge base is absent."""
    reg = pi_skills.build_registry(tmp_path / "missing-vault")
    assert set(SEED_NAMES) <= set(reg)
    for name, skill in reg.items():
        assert isinstance(skill, pi_skills.Skill), name
        assert skill.title and skill.category and skill.summary
        assert skill.playbook.strip(), name


def test_ac1_registry_enriched_from_knowledge_base_notes(tmp_path):
    """AC1: Vault notes matched by category are merged into the registry entries."""
    vault = Vault(tmp_path / "kb")
    vault.write(
        "Button key lives on proto",
        "AppTest buttons expose .key; the element object is proto.key, not b.key.",
        type="pitfall",
    )
    vault.write(
        "Unrelated ops note",
        "Nothing to do with streamlit accessors or icons.",
        type="pitfall",
    )
    reg = pi_skills.build_registry(vault.root)
    text = reg["apptest-button-key"].playbook
    assert "## Related knowledge-base notes" in text
    assert "Button key lives on proto" in text
    assert "Unrelated ops note" not in text
    assert (
        vault.get(reg["apptest-button-key"].sources[0]).title
        == "Button key lives on proto"
    )
    # skills whose category has no matching notes stay pure seeds
    assert reg["cap-truncation"].sources == ()
    assert "Related knowledge-base notes" not in reg["cap-truncation"].playbook


def test_ac1_list_skills_and_load_skill_functions(tmp_path):
    """AC1: list_skills() and load_skill(name) are exposed and behave."""
    vault = tmp_path / "no-vault"
    listing = pi_skills.list_skills(vault)
    assert isinstance(listing, str)
    for name in SEED_NAMES:
        assert name in listing
    text = pi_skills.load_skill("cap-truncation", vault)
    assert isinstance(text, str) and text.startswith("# ")


# --------------------------------------------------------------------------- AC2
def test_ac2_load_skill_registered_alongside_navtools():
    """AC2: `load_skill` is a tool in TOOL_SCHEMAS/SYNC_IMPLS, next to the navtools."""
    schema = agent_tools.TOOL_SCHEMAS["load_skill"]
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "load_skill"
    assert "name" in fn["parameters"]["properties"]
    assert "name" in fn["parameters"]["required"]
    assert callable(agent_tools.SYNC_IMPLS["load_skill"])
    assert callable(agent_tools.SYNC_IMPLS["list_skills"])
    # the existing navtools are still registered alongside
    for tool in NAV_TOOLS:
        assert tool in agent_tools.TOOL_SCHEMAS, tool
        assert tool in agent_tools.ASYNC_IMPLS, tool


def test_ac2_relay_allowlist_and_sf_cli_mapping():
    """AC2: the model relay allowlists the skill tools and the `sf` CLI maps them."""
    assert {"load_skill", "list_skills"} <= set(SF_TOOLS)
    assert SF.parse(["skill", "cap-truncation"]) == (
        "load_skill",
        {"name": "cap-truncation"},
    )
    assert SF.parse(["skills"]) == ("list_skills", {})
    with pytest.raises(SF.Usage):
        SF.parse(["skill"])
    assert "sf skills" in SF.__doc__ and "sf skill NAME" in SF.__doc__
    # the Pi developer prompt tells the agent about the playbooks
    assert "sf skills" in prompts.PI_DEVELOPER_SYSTEM
    assert "sf skill" in prompts.PI_DEVELOPER_SYSTEM


async def test_ac2_relay_returns_playbook_text(cfg, tmp_path, monkeypatch):
    """AC2: POST /sf/tool load_skill through the model relay returns the playbook text."""
    from .test_pi import _llm

    monkeypatch.setenv("FAKE_KEY", "k")
    llm, _ = _llm(cfg, lambda b: {"content": "x"})
    relay = ModelRelay(llm)
    await relay.start()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")))
    tok = relay.register("developer_backend", "T1", max_steps=5, ctx=ctx)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{relay.port}") as c:
            headers = {"Authorization": f"Bearer {tok}"}

            async def call(tool, args):
                r = await c.post(
                    "/sf/tool", json={"tool": tool, "args": args}, headers=headers
                )
                return r.json()

            res = await call("load_skill", {"name": "apptest-button-key"})
            assert res["ok"] is True
            assert "b.key" in res["output"] and "proto.key" in res["output"]

            res = await call("load_skill", {"name": "no-such-skill"})
            assert res["ok"] is False
            assert res["output"].startswith("ERROR:")
            assert "available skills" in res["output"]

            res = await call("list_skills", {})
            assert res["ok"] is True
            assert "material-icon-format" in res["output"]

            res = await call("load_skill", {})  # missing required arg
            assert res["ok"] is False and "load_skill" in res["output"]
    finally:
        await relay.stop()


async def test_ac2_sf_cli_subprocess_through_relay(cfg, tmp_path, monkeypatch):
    """AC2: the installed `sf skill NAME` CLI run as a subprocess prints the playbook via the relay."""
    import asyncio
    import subprocess
    import sys

    from .test_pi import _llm

    monkeypatch.setenv("FAKE_KEY", "k")
    llm, _ = _llm(cfg, lambda b: {"content": "x"})
    relay = ModelRelay(llm)
    await relay.start()
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")))
    tok = relay.register("developer_backend", "T1", max_steps=5, ctx=ctx)
    env = {
        "PATH": "/usr/bin:/bin",
        "FAKE_KEY": "k",
        "SF_TOOLS_URL": f"http://127.0.0.1:{relay.port}",
        "SF_RELAY_TOKEN": tok,
    }
    sf_py = Path(SF.__file__)
    try:

        def run(*argv):
            return subprocess.run(
                [sys.executable, str(sf_py), *argv],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )

        r = await asyncio.to_thread(run, "skill", "apptest-button-key")
        assert r.returncode == 0, r.stderr
        assert "b.key" in r.stdout and "proto.key" in r.stdout
        r = await asyncio.to_thread(run, "skills")
        assert r.returncode == 0 and all(n in r.stdout for n in SEED_NAMES)
        r = await asyncio.to_thread(run, "skill", "no-such-skill")
        assert r.returncode == 1 and "available skills" in r.stdout
    finally:
        await relay.stop()


# --------------------------------------------------------------------------- AC3
@pytest.mark.parametrize("name,markers", sorted(PITFALL_SKILLS.items()))
def test_ac3_four_known_pitfalls_seeded_and_loadable(tmp_path, name, markers):
    """AC3: the four known pitfalls are seeded as skills and loadable by name."""
    text = pi_skills.load_skill(name, tmp_path / "missing-vault")
    assert text.startswith("# "), name
    for marker in markers:
        assert marker in text, f"{name}: missing {marker!r}"


def test_ac3_load_skill_name_normalization(tmp_path):
    """AC3: names are loadable case-insensitively with _ and - interchangeable."""
    vault = tmp_path / "missing-vault"
    expected = pi_skills.load_skill("apptest-button-key", vault)
    assert pi_skills.load_skill("APPTEST_BUTTON_KEY", vault) == expected
    assert pi_skills.load_skill(
        " material-icon-format ", vault
    ) == pi_skills.load_skill("material_icon_format", vault)


# --------------------------------------------------------------------------- AC4
def test_ac4_unknown_skill_raises_clear_error_listing_available(tmp_path):
    """AC4: load_skill with an unknown name raises an error listing available skills."""
    with pytest.raises(pi_skills.SkillError) as excinfo:
        pi_skills.load_skill("no-such-skill", tmp_path / "missing-vault")
    msg = str(excinfo.value)
    assert "unknown skill 'no-such-skill'" in msg
    assert "available skills" in msg
    for name in SEED_NAMES:
        assert name in msg, f"error should list {name}"


def test_ac4_tool_wrapper_surfaces_error_as_toolerror():
    """AC4: the agent-facing wrapper converts SkillError into a ToolError (relay-safe)."""
    with pytest.raises(agent_tools.ToolError) as excinfo:
        agent_tools.load_skill(SimpleNamespace(), "definitely-not-a-skill")
    assert "available skills" in str(excinfo.value)
    # list_skills tool works with a dummy ctx
    out = agent_tools.list_skills(SimpleNamespace())
    assert all(name in out for name in SEED_NAMES)


def test_ac4_registry_and_load_skill_work_without_network_or_db(tmp_path, monkeypatch):
    """AC4: no network or database is required to load the registry or a skill."""

    def _boom(*args, **kwargs):
        raise AssertionError("network or db access attempted")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(sqlite3, "connect", _boom)
    vault = tmp_path / "missing-vault"
    reg = pi_skills.get_registry(vault)
    assert set(SEED_NAMES) <= set(reg)
    assert pi_skills.load_skill("cap-truncation", vault)
    assert pi_skills.list_skills(vault)
