"""Tests for swarmforge.pi_skills (offline; no network/db/docker)."""

from pathlib import Path

import pytest

from swarmforge import pi_skills
from swarmforge.knowledge import Vault
from swarmforge.pi_skills import (
    SEED_SKILLS,
    SkillError,
    build_registry,
    list_skills,
    load_skill,
)

SEED_NAMES = (
    "apptest-button-key",
    "apptest-file-uploader",
    "material-icon-format",
    "cap-truncation",
)


def test_registry_contains_seed_names():
    reg = build_registry(vault_path=Path("/nonexistent-vault"))
    for name in SEED_NAMES:
        assert name in reg
        s = reg[name]
        assert s.name == name
        assert s.title
        assert s.category
        assert s.summary
        assert s.playbook.startswith("#")


def test_seed_skills_have_at_least_four():
    assert len(SEED_SKILLS) >= 4
    assert set(SEED_NAMES) <= set(SEED_SKILLS)


def test_load_skill_playbooks_mention_key_advice():
    assert "b.key" in load_skill("apptest-button-key")
    assert "proto.key" in load_skill("apptest-button-key")
    assert "at.file_uploader" in load_skill("apptest-file-uploader")
    assert ":material/" in load_skill("material-icon-format")
    assert "_cap" in load_skill("cap-truncation")


def test_load_skill_lookup_is_forgiving():
    a = load_skill("apptest-button-key")
    assert load_skill("AppTest_Button_Key") == a
    assert load_skill("  APPTEST-BUTTON-KEY ") == a


def test_load_skill_unknown_raises_with_available_names():
    with pytest.raises(SkillError) as exc:
        load_skill("no-such-skill")
    msg = str(exc.value)
    for name in SEED_NAMES:
        assert name in msg
    assert "available skills" in msg


def test_load_skill_empty_name_raises():
    with pytest.raises(SkillError):
        load_skill("   ")


def test_list_skills_mentions_each_seed():
    listing = list_skills(vault_path=Path("/nonexistent-vault"))
    for name in SEED_NAMES:
        assert name in listing
    assert listing.startswith("Available skills")


def test_vault_enrichment_appends_related_note(tmp_path):
    vault = Vault(tmp_path)
    vault.write(
        "AppTest Button.key not Button.proto.key",
        "Accessing b.proto.key raises AttributeError; use b.key instead.",
        type="pitfall",
    )
    vault.write(
        "Unrelated note about deployment",
        "Nothing to do with the topic of this skill.",
        type="pitfall",
    )
    reg = build_registry(vault_path=tmp_path)
    skill = reg["apptest-button-key"]
    assert "Related knowledge-base notes" in skill.playbook
    assert "Button.key not Button.proto.key" in skill.playbook
    assert "b.proto.key raises AttributeError" in skill.playbook
    assert any("app-test-button-key-not-button-proto-key" in s or "Button.key" in s for s in skill.sources)
    # unrelated note must not leak into this skill
    assert "deployment" not in skill.playbook


def test_vault_enrichment_ignores_wrong_note_types(tmp_path):
    Vault(tmp_path).write(
        "AppTest file_uploader pitfall",
        "Use at.file_uploader, not at.get.",
        type="user-note",
    )
    reg = build_registry(vault_path=tmp_path)
    assert "Related knowledge-base notes" not in reg["apptest-file-uploader"].playbook


def test_vault_enrichment_truncates_long_note_bodies(tmp_path):
    Vault(tmp_path).write(
        "Icon pitfall: material icon name",
        "x" * 2000,
        type="pitfall",
    )
    reg = build_registry(vault_path=tmp_path)
    skill = reg["material-icon-format"]
    assert "Related knowledge-base notes" in skill.playbook
    assert len(skill.playbook) < len(skill.playbook.split("## Related")[0]) + 1200


def test_build_registry_missing_vault_returns_seeds(tmp_path):
    reg = build_registry(vault_path=tmp_path / "does-not-exist")
    assert set(reg) >= set(SEED_NAMES)
    for s in reg.values():
        assert s.sources == ()


def test_get_registry_caches_by_path(tmp_path):
    a = pi_skills.get_registry(tmp_path)
    b = pi_skills.get_registry(tmp_path)
    assert a is b


def test_resolve_vault_path_never_raises():
    assert pi_skills.resolve_vault_path() is None or Path(
        pi_skills.resolve_vault_path()
    )
