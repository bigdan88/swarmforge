"""Loadable skills/playbooks for the Pi harness.

A *skill* is a short markdown playbook encoding a recurring pitfall
(e.g. AppTest accessor bugs, Material-icon format, output truncation) so
agents can load proven advice on demand instead of rediscovering it.

The registry starts from :data:`SEED_SKILLS` (always available, no I/O
required) and is optionally enriched with matching notes from the
knowledge base (Zettelkasten vault).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# NOTE: this module must not import from swarmforge.agent.* (circular imports).

__all__ = [
    "CATEGORY_KEYWORDS",
    "SEED_SKILLS",
    "Skill",
    "SkillError",
    "build_registry",
    "default_registry",
    "get_registry",
    "list_skills",
    "load_skill",
    "resolve_vault_path",
]


class SkillError(Exception):
    """Raised when a skill name cannot be resolved."""


@dataclass(frozen=True)
class Skill:
    """A reusable playbook that can be injected into an agent prompt."""

    name: str
    title: str
    category: str
    summary: str
    playbook: str
    sources: tuple[str, ...] = ()  # vault note stems that fed this skill


# --------------------------------------------------------------------- seeds

SEED_SKILLS: dict[str, dict] = {
    "apptest-button-key": {
        "title": "AppTest Button.key, not Button.proto.key",
        "category": "apptest-accessors",
        "summary": "Streamlit AppTest Button elements expose `.key`; `b.proto.key` raises AttributeError.",
        "playbook": """\
# AppTest Button.key, not Button.proto.key

Streamlit's `AppTest` `Button` element has **no `proto.key` field**; accessing
`b.proto.key` raises `AttributeError`. The correct accessor is `b.key`.

## WRONG

```python
at = AppTest.from_file("app.py")
at.run()
b = at.button[0]
assert b.proto.key == "run"   # AttributeError!
```

## CORRECT

```python
at = AppTest.from_file("app.py")
at.run()
b = at.button[0]
assert b.key == "run"          # works
assert b.proto.icon == ":material/play_arrow:"  # .proto.icon exists
assert b.proto.type == "primary"                # .proto.type exists
```

Notes:
- `.key` is the accessor for the button's key.
- `.proto.icon` and `.proto.type` *do* exist and work correctly.
- Only `.proto.key` is the trap.
""",
    },
    "apptest-file-uploader": {
        "title": "AppTest file_uploader accessor: at.file_uploader, never at.get(...)",
        "category": "apptest-accessors",
        "summary": "Use `at.file_uploader` / `getattr(at, 'file_uploader', None)`; `at.get('file_uploader')` is unsupported.",
        "playbook": """\
# AppTest file_uploader accessor

Streamlit's `AppTest` does **not** support `at.get('file_uploader')` for
accessing file-uploader widgets. Use the attribute accessor `at.file_uploader` (or
`getattr(at, "file_uploader", None)`) and handle
`None` / empty-list results.

## WRONG

```python
fu = at.get("file_uploader")   # not supported -> failure
```

## CORRECT

```python
fu = getattr(at, "file_uploader", None)
if fu is None or len(fu) == 0:
    # uploader not rendered (e.g. hidden behind a guard) — handle it
    ...
else:
    assert fu[0].value is not None
```

Notes:
- Prefer `getattr(at, "file_uploader", None)` when the widget may be absent.
- An empty result (no uploader rendered) is a normal state, not an error.
""",
    },
    "material-icon-format": {
        "title": "Material icons: :material/snake_case: with underscores",
        "category": "material-icons",
        "summary": "Streamlit material icons must be `:material/snake_case:` (underscores); wrong names render as literal text.",
        "playbook": """\
# Material icon format

Streamlit material icons must be written as `:material/<snake_case_name>:`
with **underscores inside the name** — never slashes or camelCase.
A wrong name silently renders as literal text instead of an icon.

## WRONG

```python
st.button("Add", icon=":material/add-circle:")   # hyphen: renders literally
NAV_ICON = {"New task": ":material/AddCircle:"}  # camelCase: renders literally
```

## CORRECT

```python
st.button("Add", icon=":material/add_circle:")
NAV_ICON = {
    "Work": ":material/view_list:",
    "New task": ":material/add_circle:",
    "Failures": ":material/healing:",
    "Settings": ":material/settings:",
    "Pull requests": ":material/merge:",
}
```

(See `NAV_ICON` in `src/swarmforge/dashboard.py` for the canonical usage.)

Notes:
- Icon names come from the Material Symbols set, in snake_case.
- There is no error/warning for a bad name — check rendered output.
""",
    },
    "cap-truncation": {
        "title": "Tool outputs are truncated by _cap()",
        "category": "truncation",
        "summary": "Tool outputs pass through `_cap(text, limit)`; never assert on the tail of long outputs.",
        "playbook": """\
# Tool outputs are truncated by _cap()

Tool outputs pass through `_cap(text, limit)` (see
`src/swarmforge/agent/navtools.py` and `chatbot.py`), which truncates long
text with a marker. Never assert on the tail of long outputs; account for
truncated JUnit/diff text.

## WRONG

```python
out = run_tool("sf tests ...")
assert "passed" in out.splitlines()[-1]   # tail may be the truncation marker
```

## CORRECT

```python
out = run_tool("sf tests ...")
assert "passed" in out or "failed" in out          # search anywhere
assert out.startswith("...") is False              # marker only appears when truncated
# if you need the tail, re-run with a smaller scope or read the report file
```

Notes:
- `_cap` truncates to a character limit and appends a truncation marker.
- JUnit summaries and long diffs are commonly cut off mid-text.
- When a test failure message looks incomplete, suspect truncation first.
""",
    },
}


CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "apptest-accessors": ("apptest", "proto.key", "file_uploader", "button.key"),
    "material-icons": ("material", "icon"),
    "truncation": ("_cap", "truncat"),
}

# note types that may enrich a skill's playbook
_SKILL_NOTE_TYPES = {"pitfall", "failure", "review-lesson"}
_MAX_NOTES_PER_SKILL = 4
_NOTE_BODY_CAP = 600


# ------------------------------------------------------------------ registry


def resolve_vault_path() -> Path | None:
    """Return the configured vault path, or None if unresolvable."""
    try:
        from .config import load_config

        return Path(load_config().vault_path)
    except Exception:  # noqa: BLE001
        return None


def _match_category(text: str) -> str | None:
    low = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(k in low for k in keywords):
            return category
    return None


def _enriched_playbook(base: str, notes: list) -> tuple[str, tuple[str, ...]]:
    if not notes:
        return base, ()
    lines = [base.rstrip("\n"), "", "## Related knowledge-base notes"]
    stems: list[str] = []
    for n in notes[:_MAX_NOTES_PER_SKILL]:
        body = n.body.strip()
        if len(body) > _NOTE_BODY_CAP:
            body = body[:_NOTE_BODY_CAP].rstrip() + " …[truncated]"
        lines += ["", f"### {n.title}", "", body]
        stems.append(n.stem)
    return "\n".join(lines) + "\n", tuple(stems)


def build_registry(vault_path: Path | str | None = None) -> dict[str, Skill]:
    """Build the skill registry from seeds, optionally enriched by vault notes.

    Never raises if the vault is missing or empty — seeds alone always load.
    """
    registry: dict[str, Skill] = {}
    by_category: dict[str, list[str]] = {}
    for name, spec in SEED_SKILLS.items():
        registry[name] = Skill(
            name=name,
            title=spec["title"],
            category=spec["category"],
            summary=spec["summary"],
            playbook=spec["playbook"],
        )
        by_category.setdefault(spec["category"], []).append(name)

    path = vault_path
    if path is None:
        path = resolve_vault_path()
    if path is None:
        return registry
    try:
        from .knowledge import Vault

        notes = [
            n
            for n in Vault(Path(path)).notes()
            if n.type in _SKILL_NOTE_TYPES and _match_category(f"{n.title}\n{n.body}")
        ]
    except Exception:  # noqa: BLE001
        return registry

    matched: dict[str, list] = {}
    for n in notes:
        category = _match_category(f"{n.title}\n{n.body}")
        if category is None:
            continue
        matched.setdefault(category, []).append(n)

    for category, cat_notes in matched.items():
        for name in by_category.get(category, ()):
            skill = registry[name]
            playbook, stems = _enriched_playbook(skill.playbook, cat_notes)
            registry[name] = Skill(
                name=skill.name,
                title=skill.title,
                category=skill.category,
                summary=skill.summary,
                playbook=playbook,
                sources=tuple(sorted(set(skill.sources) | set(stems))),
            )
    return registry


_REGISTRY_CACHE: dict[str, dict[str, Skill]] = {}


def get_registry(vault_path: Path | str | None = None) -> dict[str, Skill]:
    """Cached registry keyed by the resolved vault path."""
    if vault_path is None:
        key = str(resolve_vault_path())
    else:
        key = str(vault_path)
    if key not in _REGISTRY_CACHE:
        _REGISTRY_CACHE[key] = build_registry(vault_path)
    return _REGISTRY_CACHE[key]


def default_registry() -> dict[str, Skill]:
    """Registry using the configured vault path."""
    return get_registry(resolve_vault_path())


# -------------------------------------------------------------------- public


def list_skills(vault_path: Path | str | None = None) -> str:
    """One-line-per-skill listing of the available skills."""
    registry = get_registry(vault_path)
    lines = [f"Available skills ({len(registry)}):"]
    for name in sorted(registry):
        s = registry[name]
        lines.append(f"- {name}: {s.title} — {s.summary}")
    lines.append("Load one with load_skill(name) (or `sf skill NAME`).")
    return "\n".join(lines)


def load_skill(name: str, vault_path: Path | str | None = None) -> str:
    """Return the markdown playbook for a skill.

    Lookup is case-insensitive and treats `_` and `-` as equivalent.
    Raises :class:`SkillError` for unknown names.
    """
    registry = get_registry(vault_path)
    norm = name.strip().lower().replace("_", "-")
    skill = registry.get(norm)
    if skill is None:
        available = ", ".join(sorted(registry))
        raise SkillError(
            f"unknown skill {name!r}; available skills: {available} "
            "(call list_skills / `sf skills`)"
        )
    return skill.playbook
