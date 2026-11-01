"""Agent variants and an archive-based improvement loop (after the Darwin Gödel Machine).

A *variant* is a small, validated overlay on swarmforge.yaml: models and step/time limits per role,
team limits, and extra prompt guidance per role group. Variants form a tree (each has a parent) and
ALL of them are kept in an archive with their benchmark scores. `evolve` repeatedly
  1. picks a parent from the archive, favouring high scores and variants with few children
     (so promising-but-unexplored branches keep getting tried, not only the current best),
  2. asks the planner model to propose ONE improvement, given the parent's results and recent
     failure lessons,
  3. validates the overlay (safety settings can never be changed), and
  4. scores the child on a benchmark suite of issues with hidden acceptance tests.
Nothing changes for real runs until you pick a variant with `swarmforge variants use ID`.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

import yaml

from .config import Config, ConfigError, validate_config
from .db import Store

ROLE_KEYS = {"model": str, "fallback_models": list, "temperature": (int, float), "max_steps": int,
             "timeout_s": int, "max_output_tokens": int, "reasoning_effort": str}
ROLE_RANGES = {"temperature": (0.0, 1.5), "max_steps": (5, 150), "timeout_s": (120, 7200),
               "max_output_tokens": (1024, 65536)}
LIMIT_RANGES = {"max_concurrent_agents": (1, 4), "max_repair_cycles": (0, 5), "max_tasks": (1, 8),
                "task_retries": (0, 3), "max_planning_attempts": (1, 4)}
PROMPT_GROUPS = ("planner", "developer", "tester", "reviewer", "librarian")
MAX_ADDENDUM = 2000


# ================================================================== overlays
def validate_overlay(overlay: dict, cfg: Config) -> list[str]:
    errs: list[str] = []
    if not isinstance(overlay, dict):
        return ["overlay must be a JSON object"]
    for top in overlay:
        if top not in ("roles", "limits", "prompt_addenda"):
            errs.append(f"'{top}' cannot be changed by a variant (only roles, limits, prompt_addenda)")
    for role, vals in (overlay.get("roles") or {}).items():
        if role not in cfg.roles:
            errs.append(f"roles.{role}: unknown role")
            continue
        if not isinstance(vals, dict):
            errs.append(f"roles.{role} must be an object")
            continue
        for k, v in vals.items():
            if k not in ROLE_KEYS:
                errs.append(f"roles.{role}.{k} cannot be changed (allowed: {', '.join(ROLE_KEYS)})")
            elif not isinstance(v, ROLE_KEYS[k]) or isinstance(v, bool):
                errs.append(f"roles.{role}.{k} has the wrong type")
            elif k in ROLE_RANGES and not ROLE_RANGES[k][0] <= v <= ROLE_RANGES[k][1]:
                errs.append(f"roles.{role}.{k}={v} outside {ROLE_RANGES[k]}")
            elif k == "reasoning_effort" and v not in ("none", "low", "medium", "high"):
                errs.append(f"roles.{role}.reasoning_effort must be none, low, medium or high")
            elif k == "fallback_models" and not all(isinstance(m, str) for m in v):
                errs.append(f"roles.{role}.fallback_models must be a list of model ids")
    for k, v in (overlay.get("limits") or {}).items():
        if k not in LIMIT_RANGES:
            errs.append(f"limits.{k} cannot be changed (allowed: {', '.join(LIMIT_RANGES)})")
        elif not isinstance(v, int) or isinstance(v, bool) or not LIMIT_RANGES[k][0] <= v <= LIMIT_RANGES[k][1]:
            errs.append(f"limits.{k} must be an integer in {LIMIT_RANGES[k]}")
    for g, text in (overlay.get("prompt_addenda") or {}).items():
        if g not in PROMPT_GROUPS:
            errs.append(f"prompt_addenda.{g}: unknown group (allowed: {', '.join(PROMPT_GROUPS)})")
        elif not isinstance(text, str) or len(text) > MAX_ADDENDUM:
            errs.append(f"prompt_addenda.{g} must be text of at most {MAX_ADDENDUM} characters")
    if not errs:
        try:
            apply_overlay(cfg, overlay, "check")
        except ConfigError as e:
            errs.append(str(e))
    return errs


def apply_overlay(cfg: Config, overlay: dict, vid: str) -> Config:
    c = copy.deepcopy(cfg)
    for role, vals in (overlay.get("roles") or {}).items():
        for k, v in vals.items():
            setattr(c.roles[role], k, list(v) if k == "fallback_models" else v)
    for k, v in (overlay.get("limits") or {}).items():
        setattr(c.limits, k, v)
    c.prompt_addenda = dict(overlay.get("prompt_addenda") or {})
    c.active_variant, c.variant_applied = vid, True
    validate_config(c)          # e.g. the reviewer must still differ from every developer model
    return c


def ensure_root(store: Store) -> dict:
    if not store.variant("v0"):
        store.variant_add("v0", {}, None, "the base configuration in swarmforge.yaml")
    return store.variant("v0")


def next_id(store: Store) -> str:
    nums = [int(v["id"][1:]) for v in store.variants() if re.fullmatch(r"v\d+", v["id"])]
    return f"v{max(nums, default=-1) + 1}"


def suite_score(variant: dict, suite: str) -> float | None:
    vals = [s["score"] for s in variant["scores"] if s.get("suite") == suite and s.get("score") is not None]
    return sum(vals) / len(vals) if vals else None


# ================================================================== selection
def select_parent(variants: list[dict], suite: str, rng: random.Random | None = None) -> dict:
    """DGM-style: probability ~ sigmoid(score) / (1 + children). Unscored variants are skipped."""
    rng = rng or random.Random()
    pool = [v for v in variants if v["status"] != "invalid" and suite_score(v, suite) is not None]
    if not pool:
        raise ValueError("no scored variant to start from; evaluate v0 first")
    kids = {v["id"]: 0 for v in variants}
    for v in variants:
        if v["parent"] in kids:
            kids[v["parent"]] += 1
    weights = [1 / (1 + math.exp(-10 * (suite_score(v, suite) - 0.5))) / (1 + kids[v["id"]]) for v in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


# ================================================================== evaluation
def load_suite(path: str | Path) -> tuple[str, list[dict]]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cases = []
    for c in data.get("cases") or []:
        case = {"name": c["name"]}
        for k in ("issue", "repo_template", "repo", "acceptance"):
            if c.get(k):
                case[k] = str((p.parent / c[k]).resolve()) if not str(c[k]).startswith("http") else c[k]
        if "issue" not in case or "acceptance" not in case or not (case.get("repo") or case.get("repo_template")):
            raise ValueError(f"suite case {c.get('name')!r} needs issue, acceptance and repo or repo_template")
        cases.append(case)
    if not cases:
        raise ValueError(f"{p} has no cases")
    return data.get("name") or p.stem, cases


def _fresh_repo(template: str, dest: Path) -> Path:
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git"))
    for a in (["init", "-q", "-b", "main"], ["add", "-A"],
              ["-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *a], cwd=dest, check=True, capture_output=True)
    return dest


async def evaluate(cfg: Config, store: Store, vid: str, suite_path: str, *, runs_per_case: int = 1,
                   echo: Callable[[str], None] = print, orchestrator_kwargs: dict | None = None) -> dict:
    """Run every case of the suite with the variant and score it by hidden acceptance tests."""
    from .benchmark import evaluate_hidden
    from .orchestrator import Orchestrator
    v = store.variant(vid)
    if not v:
        raise KeyError(vid)
    suite, cases = load_suite(suite_path)
    c = apply_overlay(cfg, v["overlay"], vid)
    c.github.create_pr = False
    c.knowledge.enabled = False      # every variant starts from the same knowledge
    c.limits.one_run_per_repo = False
    per_case, run_ids, cost, elapsed = [], [], 0.0, 0.0
    for case in cases:
        for k in range(runs_per_case):
            echo(f"[{vid}] case {case['name']} ({k + 1}/{runs_per_case})")
            repo = case.get("repo")
            if not repo:
                dest = cfg.workdir / "variants" / "repos" / f"{vid}-{case['name']}-{int(time.time() * 1000)}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                repo = str(_fresh_repo(case["repo_template"], dest))
            orch = Orchestrator(c, store, approve=lambda q: False, echo=None, **(orchestrator_kwargs or {}))
            rid = await orch.execute(issue_file=case["issue"], repo=repo)
            run = store.get_run(rid)
            hidden = await evaluate_hidden(c, run, Path(case["acceptance"]), f"variant-{vid}")
            score = (hidden["passed"] / hidden["total"]) if hidden.get("total") else 0.0
            per_case.append({"case": case["name"], "run_id": rid, "state": run["state"], "score": round(score, 3),
                             "hidden": f"{hidden.get('passed', 0)}/{hidden.get('total', 0)}"})
            run_ids.append(rid)
            cs = store.cost_summary(rid)
            cost += cs["total"] or 0
            elapsed += (run["finished_at"] or time.time()) - run["created_at"]
    entry = {"suite": suite, "score": round(sum(p["score"] for p in per_case) / len(per_case), 3),
             "cost_usd": round(cost, 4), "elapsed_s": round(elapsed), "runs": run_ids, "cases": per_case,
             "ts": time.time()}
    store.variant_update(vid, scores=v["scores"] + [entry], status="scored")
    echo(f"[{vid}] {suite}: score {entry['score']:.2f}, ${cost:.2f}, {elapsed / 60:.0f} min")
    return entry


# ================================================================== mutation
MUTATOR_SYSTEM = """You improve an autonomous multi-agent software team (planner -> parallel developers ->
tester -> independent reviewer -> repair cycles). Its behaviour is set by a configuration overlay.
Propose ONE focused change to the parent overlay that is likely to raise the benchmark score (the share
of hidden acceptance tests passed) without large cost increases. Base it on the evidence: the parent's
per-case results and the failure lessons. Prefer small, testable changes: one prompt guidance
paragraph, one model swap, or one limit. Keep everything from the parent that you don't change.

Allowed keys only:
- roles.<role>.{model, fallback_models, temperature, max_steps, timeout_s, max_output_tokens,
  reasoning_effort (none|low|medium|high)}
- limits.{max_concurrent_agents (1-4), max_repair_cycles (0-5), max_tasks (1-8), task_retries (0-3),
  max_planning_attempts (1-4)}
- prompt_addenda.{planner, developer, tester, reviewer, librarian}: extra instructions (<= 2000 chars)
Only use model ids that already appear in the base configuration or the parent overlay. The reviewer
model must differ from every developer model (including fallbacks).

Answer with ONE JSON object and nothing else:
{"overlay": {...the complete child overlay...}, "rationale": "what you changed and why, citing evidence"}"""


def _base_summary(cfg: Config) -> str:
    L = ["Base configuration:"]
    for name, r in cfg.roles.items():
        L.append(f"- roles.{name}: model={r.model} fallback={r.fallback_models} temperature={r.temperature} "
                 f"max_steps={r.max_steps} timeout_s={r.timeout_s} "
                 f"reasoning_effort={r.reasoning_effort or cfg.reasoning_effort or 'provider default'}")
    lim = cfg.limits
    L.append(f"- limits: max_concurrent_agents={lim.max_concurrent_agents} max_repair_cycles="
             f"{lim.max_repair_cycles} max_tasks={lim.max_tasks} task_retries={lim.task_retries}")
    L.append(f"- developer roles: {cfg.developer_roles}")
    return "\n".join(L)


def mutation_prompt(cfg: Config, store: Store, parent: dict, suite: str) -> str:
    runs = [r for s in parent["scores"] if s.get("suite") == suite for r in s.get("runs", [])]
    lessons = []
    for rid in runs[-6:]:
        f = store.failure(rid)
        if f:
            lessons.append(f"- run {rid}: {f.get('category')}: {f.get('root_cause') or ''} LESSON: {f.get('lesson') or ''}")
    for f in store.failures(limit=8):
        if f.get("lesson") and f["run_id"] not in runs:
            lessons.append(f"- (other run) {f.get('category')}: {f['lesson']}")
    results = [c for s in parent["scores"] if s.get("suite") == suite for c in s.get("cases", [])]
    return "\n\n".join([
        _base_summary(cfg),
        f"Parent variant {parent['id']} overlay:\n{json.dumps(parent['overlay'], indent=1)}",
        f"Parent rationale: {parent.get('rationale') or '-'}",
        f"Parent results on suite '{suite}' (score = share of hidden tests passed):\n"
        + json.dumps(results[-10:], indent=1),
        "Failure lessons:\n" + ("\n".join(lessons[:12]) or "(none recorded)"),
    ])


def parse_mutation(text: str) -> tuple[dict | None, str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None, ""
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(d, dict) or not isinstance(d.get("overlay"), dict):
        return None, ""
    return d["overlay"], str(d.get("rationale") or "")[:2000]


async def propose_child(cfg: Config, store: Store, parent: dict, suite: str, *, transport=None) -> tuple[dict, str]:
    from .llm import Budget, LLMClient
    llm = LLMClient(cfg, None, None, Budget(5.0), transport=transport)
    messages = [{"role": "system", "content": MUTATOR_SYSTEM},
                {"role": "user", "content": mutation_prompt(cfg, store, parent, suite)}]
    for _ in range(3):
        res = await llm.chat("planner", messages, task_key="evolve")
        overlay, why = parse_mutation(res.content)
        errs = ["no valid JSON object with an 'overlay' key"] if overlay is None else validate_overlay(overlay, cfg)
        if not errs:
            if overlay == parent["overlay"]:
                errs = ["the child overlay is identical to the parent; change one thing"]
            else:
                return overlay, why
        messages += [{"role": "assistant", "content": res.content},
                     {"role": "user", "content": "Rejected:\n- " + "\n- ".join(errs) + "\nFix it and answer again."}]
    raise ValueError("the model did not propose a valid variant: " + "; ".join(errs))


async def evolve(cfg: Config, store: Store, suite_path: str, *, generations: int = 1, runs_per_case: int = 1,
                 seed: int | None = None, echo: Callable[[str], None] = print, transport=None,
                 orchestrator_kwargs: dict | None = None) -> list[str]:
    suite, _ = load_suite(suite_path)
    rng = random.Random(seed)
    root = ensure_root(store)
    if suite_score(root, suite) is None:
        echo("scoring the base configuration (v0) first")
        await evaluate(cfg, store, "v0", suite_path, runs_per_case=runs_per_case, echo=echo,
                       orchestrator_kwargs=orchestrator_kwargs)
    made = []
    for g in range(generations):
        parent = select_parent(store.variants(), suite, rng)
        echo(f"generation {g + 1}: parent {parent['id']} (score {suite_score(parent, suite):.2f})")
        try:
            overlay, why = await propose_child(cfg, store, parent, suite, transport=transport)
        except Exception as e:  # noqa: BLE001
            echo(f"generation {g + 1}: no child: {e}")
            continue
        vid = next_id(store)
        store.variant_add(vid, overlay, parent["id"], why)
        echo(f"{vid} <- {parent['id']}: {why[:300]}")
        await evaluate(cfg, store, vid, suite_path, runs_per_case=runs_per_case, echo=echo,
                       orchestrator_kwargs=orchestrator_kwargs)
        made.append(vid)
    echo(render_archive(store.variants(), suite))
    return made


def render_archive(variants: list[dict], suite: str | None = None) -> str:
    kids: dict[str, list[str]] = {}
    for v in variants:
        kids.setdefault(v["parent"] or "", []).append(v["id"])
    by_id = {v["id"]: v for v in variants}
    lines = [f"Variant archive{' — suite ' + suite if suite else ''}:"]

    def walk(vid: str, depth: int) -> None:
        v = by_id[vid]
        scores = {s["suite"]: s for s in v["scores"]}
        if suite:
            sc = suite_score(v, suite)
            cell = f"score {sc:.2f}" if sc is not None else "not scored"
        else:
            cell = ", ".join(f"{k}: {suite_score(v, k):.2f}" for k in scores) or "not scored"
        lines.append(f"{'  ' * depth}- {vid} [{cell}] {(v.get('rationale') or '')[:90]}")
        for k in kids.get(vid, []):
            walk(k, depth + 1)
    for root in kids.get("", []):
        walk(root, 0)
    return "\n".join(lines)


def set_active(config_path: Path, vid: str | None) -> None:
    """Write `active_variant: <id>` (or remove it) in swarmforge.yaml, keeping everything else."""
    text = config_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not re.match(r"^active_variant\s*:", ln)]
    if vid:
        lines.append(f"active_variant: {vid}   # set by `swarmforge variants use`")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
