"""Model-team picker shared by New task, the queue forms and Projects."""
from __future__ import annotations


def team_options(cfg) -> dict[str, str]:
    """value -> label. "" = the default lineup (swarmforge.yaml, or its `team:`)."""
    from swarmforge.teams import list_teams
    default = f"Default ({cfg.team})" if cfg.team else "Default (swarmforge.yaml)"
    opts = {"": default}
    for name, t in list_teams(cfg).items():
        if not t.get("error"):
            opts[name] = name
    return opts


def team_picker(cfg, container, key: str, help_extra: str = "") -> str | None:
    """A selectbox of model teams; returns the team name, or None for the default lineup."""
    opts = team_options(cfg)
    choice = container.selectbox(
        "Model team", list(opts), key=key, format_func=opts.get,
        help="Which models fill every role. Fallback models come from the same team only. "
             "Teams live in `teams/` next to swarmforge.yaml (`swarmforge teams` lists them)." + help_extra)
    return choice or None


def team_config(cfg, team: str | None):
    """The configuration a run with `team` would use, or (cfg, error message)."""
    from swarmforge.config import ConfigError
    from swarmforge.teams import apply_team
    try:
        return apply_team(cfg, team), None
    except ConfigError as e:
        return cfg, str(e)
