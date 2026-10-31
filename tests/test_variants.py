"""Variant archive + DGM-style evolve loop."""
import json
import random

import pytest

from swarmforge import variants as V
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator

from . import fake_team
from .conftest import ROOT, FakeProvider

SUITE = str(ROOT / "bench" / "suite.yaml")


def test_overlay_validation_blocks_safety_settings_and_bad_values(cfg):
    assert V.validate_overlay({"roles": {"planner": {"max_steps": 20}},
                               "limits": {"max_tasks": 3},
                               "prompt_addenda": {"developer": "Write tests first."}}, cfg) == []
    errs = V.validate_overlay({"sandbox": {"network": "bridge"}, "github": {"require_push_approval": False},
                               "limits": {"budget_usd": 999, "max_tasks": 50},
                               "roles": {"planner": {"provider": "x", "max_steps": 1}, "ghost": {}},
                               "prompt_addenda": {"hacker": "x", "developer": "y" * 3000}}, cfg)
    text = " | ".join(errs)
    for frag in ("'sandbox' cannot be changed", "'github' cannot be changed", "limits.budget_usd cannot",
                 "limits.max_tasks must be", "roles.planner.provider cannot", "roles.planner.max_steps=1",
                 "roles.ghost: unknown role", "prompt_addenda.hacker", "prompt_addenda.developer must be"):
        assert frag in text, frag
    # the reviewer may never be one of the developer models
    assert V.validate_overlay({"roles": {"reviewer": {"model": "deepseek-fake"}}}, cfg)


def test_apply_overlay_and_orchestrator_uses_active_variant(cfg, tmp_path):
    st = Store(tmp_path / "db")
    st.variant_add("v1", {"roles": {"planner": {"max_steps": 22}}, "limits": {"max_tasks": 3},
                          "prompt_addenda": {"developer": "Run check_code."}}, "v0", "test")
    c = V.apply_overlay(cfg, st.variant("v1")["overlay"], "v1")
    assert c.roles["planner"].max_steps == 22 and c.limits.max_tasks == 3 and cfg.limits.max_tasks != 3
    cfg.active_variant = "v1"
    o = Orchestrator(cfg, st, echo=None)
    assert o.cfg.roles["planner"].max_steps == 22 and o.cfg.prompt_addenda == {"developer": "Run check_code."}
    assert o._prompt_group("developer_backend") == "developer" and o._prompt_group("reviewer") == "reviewer"
    cfg.active_variant = "nope"
    o = Orchestrator(cfg, st, echo=None)
    assert o.cfg.active_variant is None and "not found" in o._variant_note


def test_parent_selection_prefers_good_and_unexplored(tmp_path):
    def v(i, parent, score):
        return {"id": i, "parent": parent, "status": "scored", "overlay": {}, "rationale": "",
                "scores": [{"suite": "s", "score": score}]}
    vs = [v("v0", None, 0.6), v("v1", "v0", 0.6), v("v2", "v0", 0.6), v("v3", "v0", 0.1),
          {"id": "v4", "parent": "v0", "status": "new", "overlay": {}, "rationale": "", "scores": []}]
    picks = [V.select_parent(vs, "s", random.Random(i))["id"] for i in range(400)]
    assert "v4" not in picks                                   # unscored variants are never parents
    assert picks.count("v1") > picks.count("v0") * 2           # v0 already has 4 children
    assert picks.count("v1") > picks.count("v3") * 3           # low score -> rarely picked
    with pytest.raises(ValueError):
        V.select_parent([vs[-1]], "s")


def test_parse_mutation_and_set_active(tmp_path):
    ov, why = V.parse_mutation('Sure! {"overlay": {"limits": {"max_tasks": 3}}, "rationale": "fewer tasks"}')
    assert ov == {"limits": {"max_tasks": 3}} and why == "fewer tasks"
    assert V.parse_mutation("no json")[0] is None
    cfgf = tmp_path / "swarmforge.yaml"
    cfgf.write_text("limits:\n  max_tasks: 5\nactive_variant: v1\n")
    V.set_active(cfgf, "v3")
    assert cfgf.read_text().count("active_variant") == 1 and "active_variant: v3" in cfgf.read_text()
    V.set_active(cfgf, None)
    assert "active_variant" not in cfgf.read_text() and "max_tasks: 5" in cfgf.read_text()


@pytest.mark.slow
async def test_evolve_scores_v0_proposes_child_and_scores_it(cfg, tmp_path):
    st = Store(cfg.db_path)
    mutations = []

    def planner(body):
        if "You improve an autonomous multi-agent" in body["messages"][0]["content"]:
            mutations.append(body)
            if len(mutations) == 1:   # first answer is invalid -> must be corrected
                return {"content": '{"overlay": {"sandbox": {"network": "bridge"}}, "rationale": "x"}'}
            return {"content": json.dumps({"overlay": {"prompt_addenda": {"developer": "Write the tests first."}},
                                           "rationale": "parent missed edge cases"})}
        return fake_team.planner(body)

    fake = FakeProvider({"qwen-fake": planner, "deepseek-fake": fake_team.developer,
                         "kimi-fake": fake_team.developer, "minimax-fake": fake_team.tester,
                         "glm-fake": fake_team.Reviewer()})
    made = await V.evolve(cfg, st, SUITE, generations=1, seed=1, echo=lambda m: None, transport=fake.transport,
                          orchestrator_kwargs={"llm_transport": fake.transport})
    assert made == ["v1"]
    v0, v1 = st.variant("v0"), st.variant("v1")
    assert v0["scores"][0]["score"] > 0 and not v0["scores"][0]["cases"][0]["hidden"].endswith("/0")
    assert v1["parent"] == "v0" and v1["overlay"] == {"prompt_addenda": {"developer": "Write the tests first."}}
    assert v1["scores"] and v1["scores"][0]["suite"] == "samples"
    assert "Rejected" in mutations[1]["messages"][-1]["content"]          # invalid overlay was refused
    run = st.get_run(v1["scores"][0]["runs"][0])
    assert run["variant_id"] == "v1"
    dev_sys = [r["messages"][0]["content"] for r in fake.requests if r["model"] == "deepseek-fake"]
    assert any("Write the tests first." in s for s in dev_sys)            # the addendum reached developers
