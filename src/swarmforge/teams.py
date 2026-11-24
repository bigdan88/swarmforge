"""Named model teams: a whole lineup (every role's model and fallbacks) chosen by name.

A team is a YAML file with `description`, `roles` and optionally `vision_models`. Built-in teams ship
in `resources/teams/`; a file with the same name in `teams/` next to swarmforge.yaml overrides one,
and new files there add teams. Select one with `team: NAME` in swarmforge.yaml, `--team NAME` on the
command line, or per queue task / project / benchmark.

Rules (so a team's results are really that team's):
* the team must name a model for every role the configuration uses;
* fallback models come only from the team: a role without `fallback_models` in the team gets none,
  the base configuration's fallbacks are never kept;
* teams change models and model settings only, never permissions, budgets or sandbox policy.
"""
from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path

import yaml

from .config import Config, ConfigError, RoleConfig, validate_config

ROLE_FIELDS = {"model", "fallback_models", "runtime", "max_steps", "timeout_s", "temperature",
               "max_output_tokens", "reasoning_effort"}
NO_TEAM = ""          # the lineup written in swarmforge.yaml itself


def _builtin_dir() -> Path:
    return Path(str(resources.files("swarmforge") / "resources" / "teams"))


def team_dirs(cfg: Config | None) -> list[Path]:
    """Lowest priority first: built-in teams, then `teams/` next to swarmforge.yaml."""
    dirs = [_builtin_dir()]
    if cfg is not None and cfg.source_path is not None:
        dirs.append(cfg.source_path.parent / "teams")
    return dirs


def team_files(cfg: Config | None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in team_dirs(cfg):
        if d.is_dir():
            for f in sorted([*d.glob("*.yaml"), *d.glob("*.yml")]):
                found[f.stem] = f
    return found


def _read(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read team {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"description", "roles", "vision_models"}:
        raise ConfigError(f"team {path.stem}: only description, roles and vision_models are allowed")
    if not isinstance(data.get("roles"), dict) or not data["roles"]:
        raise ConfigError(f"team {path.stem}: roles must be a non-empty mapping")
    for name, values in data["roles"].items():
        if not isinstance(values, dict) or set(values) - ROLE_FIELDS:
            raise ConfigError(f"team {path.stem}: role {name} may only set {sorted(ROLE_FIELDS)}")
        if not isinstance(values.get("model"), str) or not values["model"].strip():
            raise ConfigError(f"team {path.stem}: role {name} needs a model")
        fb = values.get("fallback_models", [])
        if not isinstance(fb, list) or not all(isinstance(m, str) for m in fb):
            raise ConfigError(f"team {path.stem}: role {name}.fallback_models must be a list of model names")
    vm = data.get("vision_models", [])
    if not isinstance(vm, list) or not all(isinstance(m, str) for m in vm):
        raise ConfigError(f"team {path.stem}: vision_models must be a list of model names")
    return data


def list_teams(cfg: Config | None) -> dict[str, dict]:
    """name -> {description, models (role -> model), path}. Unreadable files are reported, not raised."""
    out = {}
    for name, path in team_files(cfg).items():
        try:
            data = _read(path)
            out[name] = {"description": str(data.get("description") or ""), "path": str(path),
                         "models": {r: v["model"] for r, v in data["roles"].items()}}
        except ConfigError as e:
            out[name] = {"description": f"invalid: {e}", "path": str(path), "models": {}, "error": str(e)}
    return out


def load_team(cfg: Config | None, name: str) -> dict:
    name = (name or "").strip()
    p = Path(name)
    if p.suffix in (".yaml", ".yml"):
        if not p.is_absolute() and cfg is not None and cfg.source_path is not None:
            p = cfg.source_path.parent / p
        if not p.is_file():
            raise ConfigError(f"team file {p} not found")
        return _read(p)
    files = team_files(cfg)
    if name not in files:
        raise ConfigError(f"unknown team {name!r}; available: {', '.join(sorted(files)) or 'none'}")
    return _read(files[name])


def apply_team(cfg: Config, name: str | None) -> Config:
    """A copy of `cfg` with team `name`'s lineup. Empty name = `cfg` unchanged (a copy)."""
    c = copy.deepcopy(cfg)
    if not name or name == NO_TEAM:
        return c
    team = load_team(cfg, name)
    roles = team["roles"]
    missing = sorted(r for r in c.roles if r not in roles)
    if missing:
        raise ConfigError(f"team {name!r} does not choose a model for role(s) {', '.join(missing)}; "
                          "a team must cover every role so no other team's model is used")
    for role, values in roles.items():
        if role not in c.roles:
            if role != "lead":
                continue                      # a role this configuration does not use
            base = c.roles["planner"]       # the project lead is a planning call
            c.roles[role] = RoleConfig(name=role, provider=base.provider, model=values["model"],
                                       temperature=base.temperature, max_steps=base.max_steps,
                                       timeout_s=base.timeout_s, max_output_tokens=base.max_output_tokens)
        rc = c.roles[role]
        for field, value in values.items():
            setattr(rc, field, list(value) if field == "fallback_models" else value)
        if "fallback_models" not in values:
            rc.fallback_models = []           # never fall back to another team's model
        rc.price_in_per_mtok = rc.price_out_per_mtok = None   # prices of the old model no longer apply
    c.vision_models = list(team.get("vision_models") or [])
    c.team = Path(name).stem if Path(name).suffix in (".yaml", ".yml") else name
    validate_config(c)
    return c


def team_models(cfg: Config) -> set[str]:
    return {m for rc in cfg.roles.values() for m in (rc.model, *rc.fallback_models)}
