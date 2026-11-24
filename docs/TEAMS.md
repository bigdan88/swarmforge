# Model teams

A team is a complete lineup: a model (and backups) for every role. Pick one by name instead of
editing `roles:` in swarmforge.yaml, and benchmark several teams on the same tasks.

| Team | Planner / lead | Developers | Reviewer | Tester | Librarian | Designer (vision) |
|---|---|---|---|---|---|---|
| `balanced` | Qwen3.8-Flash planner / Qwen4-Max lead | DeepSeek V4.1 Flash 1017 backend / MiMo V2.6 Pro frontend | GLM-5.3 | Qwen3.8-Flash | DeepSeek V4 Flash 0731 | Qwen3-VL-235B |
| `chinese` | Qwen3.8-Flash | GLM-5.3 | Kimi-K2.6 | Qwen3.8-Flash | DeepSeek-V4-Flash | Qwen3-VL-235B |
| `american-premium` | Claude Sonnet 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Claude Haiku 4.5 | Claude Haiku 4.5 | Claude Sonnet 5.5 |
| `american-open` | Nemotron 3 Ultra | gpt-oss-120b | Nemotron 3 Ultra | Nemotron 3 Super | Gemma 4 26B | Nemotron 3.5 Super VL |

All models are served by DeepInfra with the same API key. `swarmforge teams` prints the exact model
ids, including fallbacks. Claude costs several times more per token than the other teams, so start
with a small budget.

The local roster selected on November 22, 2026 is `balanced`. Its backend backup is DeepSeek
V4 Flash 0731, frontend backup is MiMo V2.6 Flash, and reviewer backup is Kimi K2.6. Neither
developer can fall back to a reviewer model. The project lead has Qwen3.8-Max as its backup.
Chat uses DeepSeek V4.1 Flash 1017 when the configuration defines a chat role. Model settings,
including existing developer harnesses, are inherited unless the team explicitly overrides them.
This selection is not a benchmark result and does not change installation defaults.

Select it with `team: balanced` or `swarmforge --team balanced ...`. Existing runs keep their
launch-time configuration, and projects/tasks with an explicit team retain that choice. The queue
worker reloads configuration before starting each new task; active runs are not interrupted.
Return to `team: chinese` for the previous named lineup.

## Rules

* A team must choose a model for **every** role in your configuration; otherwise it is refused.
* **Backups come from the same team.** A role's `fallback_models` are taken from the team file only;
  if the team gives none, that role has none. A busy model never falls back to another team's model.
* The reviewer may not share a model (fallbacks included) with any developer.
* Teams change models and model settings only (runtime, steps, timeouts, temperature, reasoning
  effort). Budgets, permissions, sandbox and merge policy stay in swarmforge.yaml.

## Choosing a team

* Default for everything: `team: american-open` in swarmforge.yaml (no `team:` = the `roles:` there).
* One command: `swarmforge --team american-premium run ...` (any command takes `--team`).
* Dashboard: **Model team** on New task (start now or queue) and on a project before Approve and launch.
  A project's lead and all its tasks use the project's team.
* Test a team's models first: `swarmforge --team american-premium check --live`.

## Benchmarking teams

```sh
swarmforge bench-suite bench/suite.yaml --teams chinese american-open american-premium \
    --modes swarm --runs 3 --budget 15
```

With `--teams`, `--budget` applies to **each** team: a team that uses up its budget stops while the
others carry on. Runs alternate between teams so provider load does not favor one. Results are grouped
as `team / mode` (delivery rate, median and p95 time, cost, cost per success) in the JSON report and
on the **Benchmarks** page.

## Your own teams

Built-in teams ship in `src/swarmforge/resources/teams/`. Put YAML files in `teams/` next to
swarmforge.yaml to add teams or override a built-in one with the same name:

```yaml
description: My mix
roles:
  planner: {model: anthropic/claude-sonnet-5-5, fallback_models: [anthropic/claude-sonnet-5]}
  developer_backend: {model: openai/gpt-oss-120b}
  # ... every role ...
vision_models: [anthropic/claude-]   # which of the team's models accept screenshots
```
