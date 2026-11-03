"""Team profiles contain model/harness choices, never operational permissions."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .config import ConfigError

ROLE_FIELDS = {"model", "runtime", "fallback_models", "max_steps", "timeout_s",
               "temperature", "max_output_tokens", "reasoning_effort"}


def apply_team_profile(raw: dict, source_path: Path | None) -> dict:
    path = Path(raw["team_profile"])
    if not path.is_absolute():
        path = (source_path.parent if source_path else Path.cwd()) / path
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read team profile {path}: {exc}") from exc
    if not isinstance(profile, dict) or set(profile) - {"roles", "description"}:
        raise ConfigError("team profile supports only description and roles")
    roles = profile.get("roles")
    if not isinstance(roles, dict):
        raise ConfigError("team profile roles must be a mapping")
    result = copy.deepcopy(raw)
    for name, values in roles.items():
        if name not in result.get("roles", {}):
            raise ConfigError(f"team profile references unknown role {name}")
        if not isinstance(values, dict) or set(values) - ROLE_FIELDS:
            raise ConfigError(f"team profile role {name} contains unsupported fields")
        for field, value in values.items():
            expected = (list if field == "fallback_models" else
                        (int, float) if field == "temperature" else
                        int if field in {"max_steps", "timeout_s", "max_output_tokens"} else str)
            if not isinstance(value, expected) or isinstance(value, bool):
                raise ConfigError(f"team profile role {name}.{field} has the wrong type")
            if field == "fallback_models" and not all(isinstance(m, str) for m in value):
                raise ConfigError(f"team profile role {name}.fallback_models must contain model names")
        result["roles"][name] = {**result["roles"][name], **values}
    return result
