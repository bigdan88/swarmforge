"""Named model teams: whole lineups chosen by name, fallbacks only from the same team."""
import copy
import subprocess
from pathlib import Path

import pytest
import yaml

from swarmforge import launcher, runqueue
from swarmforge.cli import main
from swarmforge.config import ConfigError, parse_config
from swarmforge.db import Store
from swarmforge.evaluation import run_suite, summarize
from swarmforge.teams import apply_team, list_teams, team_models

from .conftest import base_raw

BUILTIN = ("chinese", "american-premium", "american-open", "balanced")


def _team(path: Path, name: str, roles: dict, **extra) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    f = path / f"{name}.yaml"
    f.write_text(yaml.safe_dump({"description": name, "roles": roles, **extra}))
    return f


def _full(prefix: str, *, fallbacks=True) -> dict:
    roles = {r: {"model": f"{prefix}-{r}"} for r in
             ("planner", "developer_backend", "developer_frontend", "reviewer", "tester")}
    if fallbacks:
        for r, v in roles.items():
            v["fallback_models"] = [f"{prefix}-{r}-backup"]
    return roles


def test_builtin_teams_are_complete_and_keep_the_reviewer_independent(cfg):
    teams = list_teams(cfg)
    assert set(BUILTIN) <= set(teams)
    for name in BUILTIN:
        assert "error" not in teams[name]
        c = apply_team(cfg, name)
        assert c.team == name and "lead" in c.roles
        dev_models = {m for d in c.developer_roles for m in (c.roles[d].model, *c.roles[d].fallback_models)}
        rev = c.roles["reviewer"]
        assert not dev_models & {rev.model, *rev.fallback_models}
    premium = apply_team(cfg, "american-premium")
    assert all(m.startswith("anthropic/") for m in team_models(premium))
    american = {"nvidia", "openai", "google"}
    assert all(m.split("/")[0] in american for m in team_models(apply_team(cfg, "american-open")))


def test_balanced_team_preserves_execution_policy_and_enables_selected_vision_models(cfg):
    from dataclasses import asdict
    from swarmforge.llm import model_supports_vision

    cfg.roles["developer_backend"].runtime = "pi"
    before = copy.deepcopy(cfg)
    selected = apply_team(cfg, "balanced")
    assert selected.roles["lead"].model == "Qwen/Qwen4-Max"
    assert selected.roles["developer_backend"].runtime == "pi"
    assert selected.roles["reviewer"].fallback_models == ["moonshotai/Kimi-K2.6"]
    for field in ("limits", "sandbox", "github", "projects", "review"):
        assert asdict(getattr(selected, field)) == asdict(getattr(before, field))
    for model in ("Qwen/Qwen4-Max", "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                  "XiaomiMiMo/MiMo-V2.6-Pro", "XiaomiMiMo/MiMo-V2.6-Flash"):
        assert model_supports_vision(model, selected.vision_models)
    assert cfg.team is None
    assert cfg.roles["developer_backend"].model == before.roles["developer_backend"].model


def test_team_replaces_every_model_and_never_keeps_other_teams_fallbacks(cfg, tmp_path):
    cfg.roles["planner"].fallback_models = ["chinese-backup"]
    cfg.source_path = tmp_path / "swarmforge.yaml"
    roles = _full("us", fallbacks=False)
    roles["tester"]["fallback_models"] = ["us-tester-backup"]
    _team(tmp_path / "teams", "mine", roles, vision_models=["us-vl"])
    c = apply_team(cfg, "mine")
    assert c.roles["planner"].model == "us-planner" and c.roles["planner"].fallback_models == []
    assert c.roles["tester"].fallback_models == ["us-tester-backup"]
    assert team_models(c) == {*(v["model"] for v in roles.values()), "us-tester-backup"}
    assert c.vision_models == ["us-vl"]
    assert cfg.roles["planner"].model == "qwen-fake" and cfg.team is None   # the base is untouched
    assert c.roles["planner"].provider == cfg.roles["planner"].provider     # permissions/providers stay


def test_a_team_must_cover_every_role(cfg, tmp_path):
    cfg.source_path = tmp_path / "swarmforge.yaml"
    roles = _full("us")
    del roles["tester"]
    _team(tmp_path / "teams", "partial", roles)
    with pytest.raises(ConfigError, match="tester"):
        apply_team(cfg, "partial")
    with pytest.raises(ConfigError, match="unknown team"):
        apply_team(cfg, "nope")


def test_team_cannot_let_a_developer_review_its_own_work(cfg, tmp_path):
    cfg.source_path = tmp_path / "swarmforge.yaml"
    roles = _full("us")
    roles["reviewer"]["fallback_models"] = ["us-developer_backend"]
    _team(tmp_path / "teams", "selfreview", roles)
    with pytest.raises(ConfigError, match="reviewer"):
        apply_team(cfg, "selfreview")


def test_local_teams_folder_overrides_and_adds(cfg, tmp_path):
    cfg.source_path = tmp_path / "swarmforge.yaml"
    _team(tmp_path / "teams", "chinese", _full("override"))
    _team(tmp_path / "teams", "extra", _full("extra"))
    teams = list_teams(cfg)
    assert teams["chinese"]["models"]["planner"] == "override-planner"
    assert "extra" in teams and "american-open" in teams


def test_team_key_in_config_selects_the_lineup(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["team"] = "american-open"
    c = parse_config(raw)
    assert c.team == "american-open"
    assert c.roles["developer_backend"].model == "openai/gpt-oss-120b"


def _write_cfg(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("FAKE_KEY", "k")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(base_raw(tmp_path / "work")))
    return p


def test_cli_lists_teams_and_applies_team_flag(tmp_path, monkeypatch, capsys):
    p = _write_cfg(tmp_path, monkeypatch)
    assert main(["-c", str(p), "teams"]) == 0
    out = capsys.readouterr().out
    assert all(n in out for n in BUILTIN) and "anthropic/claude-opus-5-5" in out
    assert main(["-c", str(p), "--team", "american-premium", "teams"]) == 0
    assert "american-premium  (current)" in capsys.readouterr().out
    assert main(["-c", str(p), "--team", "missing", "teams"]) == 2
    assert "unknown team" in capsys.readouterr().err


def test_launcher_passes_the_team_to_the_run_process(cfg, tmp_path, monkeypatch):
    cfg.source_path = tmp_path / "swarmforge.yaml"
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.setdefault("args", args))
    launcher.start_run(cfg, repo=str(tmp_path), issue_title="t", issue_body="b", team="american-open")
    args = seen["args"]
    assert args[args.index("--team") + 1] == "american-open" and args.index("--team") < args.index("run")


async def test_queue_task_with_a_broken_team_fails_without_running(cfg, tmp_path, monkeypatch):
    store = Store(cfg.db_path)
    monkeypatch.setattr(runqueue, "fresh_config", lambda c, echo=None: c)
    iid = store.queue_add("Do it", "body", str(tmp_path), team="no-such-team")
    item = await runqueue.run_item(cfg, store, store.queue_item(iid))
    assert item["status"] == "failed" and "model team problem" in item["note"]
    assert item["team"] == "no-such-team" and not item["run_id"]


async def test_team_comparison_budgets_each_team_separately(cfg, tmp_path, monkeypatch):
    from swarmforge import evaluation
    template = tmp_path / "template"
    template.mkdir()
    (template / "app.py").write_text("x = 1\n")
    issue = tmp_path / "issue.md"
    issue.write_text("Do the work")
    acceptance = tmp_path / "acceptance"
    acceptance.mkdir()
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"cases": [{"name": "one", "repo_template": str(template),
                                                "issue": str(issue), "acceptance": str(acceptance)}]}))
    store = Store(tmp_path / "db")
    seen = []
    costs = {"chinese": .1, "american-premium": .6}

    class Fake:
        def __init__(self, config, store, **kw):
            self.config, self.store = copy.deepcopy(config), store

        async def execute(self, **kw):
            rid = f"r{len(seen)}"
            seen.append((self.config.team, self.config.roles["planner"].model, self.config.limits.budget_usd))
            self.store.create_run(rid, mode="single", state="COMPLETED")
            return rid

    def metrics(store, rid):
        team = seen[int(rid[1:])][0]
        return dict(run_id=rid, mode="single", final_state="COMPLETED", elapsed_s=1, cost_usd=costs[team],
                    repair_cycles=0, human_interventions=0, calls_without_cost=0)
    monkeypatch.setattr(evaluation, "metrics", metrics)
    monkeypatch.setattr(store, "cost_summary", lambda rid: {"total": costs[seen[int(rid[1:])][0]]})

    async def hidden(*_):
        return {"total": 1, "passed": 1}
    monkeypatch.setattr(evaluation, "evaluate_hidden", hidden)
    result = await run_suite(cfg, store, suite, runs=3, modes=["single"], budget=1,
                             orchestrator_factory=Fake, echo=lambda _: None,
                             teams=["chinese", "american-premium"])
    by_team = [t for t, _, _ in seen]
    # premium spends $0.60 per run: one run, then a second with $0.40 left, then it is out of budget
    assert by_team.count("american-premium") == 2 and by_team.count("chinese") == 3
    assert result["teams_out_of_budget"] == ["american-premium"] and result["status"] == "completed"
    assert {m for t, m, _ in seen if t == "american-premium"} == {"anthropic/claude-sonnet-5-5"}
    assert all(b <= 1 for _, _, b in seen)
    assert set(result["summary"]) == {"chinese / single", "american-premium / single"}
    assert result["spent_by_team"] == {"chinese": .3, "american-premium": 1.2}
    assert set(result["team_configs"]) == {"chinese", "american-premium"}


def test_summary_without_teams_keeps_mode_keys():
    row = dict(mode="swarm", final_state="COMPLETED", elapsed_s=1, cost_usd=.1, repair_cycles=0,
               human_interventions=0, calls_without_cost=0, hidden_acceptance={"total": 1, "passed": 1})
    assert set(summarize([row])) == {"swarm"}
    assert set(summarize([{**row, "team": "x"}])) == {"x / swarm"}


async def test_bad_team_in_comparison_fails_before_spending(cfg, tmp_path):
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"cases": [{"name": "one", "repo_template": str(tmp_path),
                                                "issue": str(suite), "acceptance": str(tmp_path)}]}))

    def never(*a, **k):
        raise AssertionError("no run may start")
    with pytest.raises(ConfigError, match="unknown team"):
        await run_suite(cfg, Store(tmp_path / "db"), suite, budget=1, teams=["chinese", "nope"],
                        orchestrator_factory=never, echo=lambda _: None)
