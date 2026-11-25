"""Application-verification integration with the existing planning and repair loop."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .state import RunAborted, RunState
from .verification import (CONTRACT_FILE, VerificationError, VerificationEvidence, bind_requirements, contract_hash, install_application,
                           freeze_checks, parse_checks, prepare_workspace, project_requirements,
                           run_checks)


def configure(owner) -> None:
    owner._requirement_catalog = []
    owner._assigned_requirements = []
    owner._verification_key = "verification_contract:run:" + owner.run_id
    queued = owner.store._one("SELECT project_id,milestone,requirement_ids FROM queue_items WHERE run_id=? AND project_id IS NOT NULL ORDER BY id DESC LIMIT 1", (owner.run_id,))
    if queued:
        from .projects.spec import fixed
        from .projects.store import Projects
        project = Projects(owner.store).get(queued["project_id"])
        owner._requirement_catalog = project_requirements(project["definition"], queued["milestone"])
        owner._assigned_requirements = json.loads(queued.get("requirement_ids") or "[]")
        revision = hashlib.sha256(fixed(project["spec"]).encode()).hexdigest()
        owner._verification_key = f"verification_contract:project:{project['id']}:{revision}"
    saved = owner.store.kv_get(owner._verification_key)
    owner._previous_verification = parse_checks(json.loads(saved)) if saved else []
    profile = owner.ws.repo / CONTRACT_FILE
    if profile.is_file():
        raw = json.loads(profile.read_text(encoding="utf-8"))
        declared = parse_checks(raw["checks"] if isinstance(raw, dict) else raw)
        if owner._requirement_catalog and any(not ref.startswith("M") or "C" not in ref for case in declared for ref in case["criteria"]):
            raise VerificationError("Project repository contracts must name stable requirement IDs, not reusable local AC IDs")
        owner._previous_verification = freeze_checks(owner._previous_verification, declared, [])


def validate(owner, plan):
    bind_requirements(plan, getattr(owner, "_requirement_catalog", []), getattr(owner, "_assigned_requirements", []))
    bindings = {c.id: c.requirement_id or c.id for c in plan.acceptance_criteria}
    # AC1 is local to a task. Persist M1C1 instead, so another task's AC1 cannot
    # accidentally reuse this proof for a different approved requirement.
    plan.verification_checks = [{**case, "criteria": list(dict.fromkeys(bindings.get(ref, ref) for ref in case["criteria"]))}
                                for case in parse_checks(plan.verification_checks)]
    freeze_checks(getattr(owner, "_previous_verification", []), plan.verification_checks, plan.acceptance_criteria)
    return plan


def freeze(owner, plan) -> None:
    validate(owner, plan)
    owner._verification_checks = freeze_checks(owner._previous_verification, plan.verification_checks, plan.acceptance_criteria)
    # A cached baseline (common on resume and self-healing) does not run tests,
    # so their usual artifact-directory creation has not happened yet.
    owner.artifacts.mkdir(parents=True, exist_ok=True)
    # This host-owned artifact is the repair contract; no tester report can replace it.
    (owner.artifacts / "verification-contract.json").write_text(json.dumps({
        "hash": contract_hash(owner._verification_checks), "checks": owner._verification_checks,
        "requirements": owner._requirement_catalog,
    }, indent=2), encoding="utf-8")
    owner.store.kv_set(owner._verification_key, json.dumps(owner._verification_checks, sort_keys=True))


async def execute(owner, cycle):
    checks = getattr(owner, "_verification_checks", [])
    if not checks:
        owner._verification_results = []
        return [], owner.ws.head(owner.ws.repo)
    commit = owner.ws.head(owner.ws.repo)
    target = owner.run_dir / f"verification-workspace-c{cycle}"
    if target.exists():
        # A resumed cycle gets a new workspace, rather than reusing stale outputs.
        import uuid
        target = target.with_name(target.name + "-" + uuid.uuid4().hex[:8])
    prepare_workspace(owner.ws.repo, target)
    owner.log("verification", f"Checking real application behavior in a fresh workspace ({len(checks)} checks)")
    status = await owner._ensure_deps(target, f"verification-c{cycle}")
    if status == "failed":
        raise RunAborted(RunState.BLOCKED, "Product verification dependencies unavailable; inspect the dependency-install log")
    problem = await install_application(owner.sandbox, target, owner.artifacts, owner.cfg.sandbox.command_timeout_s)
    if problem:
        evidence = [VerificationEvidence(case["id"], case["criteria"], case["level"], "failed", problem,
                                         commit, contract_hash([case]), str(owner.artifacts / "application-install.log")) for case in checks]
    else:
        evidence = await run_checks(owner.sandbox, target, checks, owner.artifacts,
                                commit_sha=commit, timeout_s=owner.cfg.sandbox.command_timeout_s, cycle=cycle)
    owner._verification_results = evidence
    owner.store.kv_set(f"verification_results:{owner.run_id}:{cycle}", json.dumps([asdict(e) for e in evidence]))
    for result in evidence:
        owner.log("verification", f"{result.check_id}: {result.status}: {result.reason}",
                  level="info" if result.status == "passed" else "warn", data=asdict(result))
    return evidence, commit


def unavailable(evidence) -> None:
    blocked = [row for row in evidence if row.status == "blocked"]
    if blocked:
        raise RunAborted(RunState.BLOCKED, "Product verification environment unavailable: " +
                         "; ".join(f"{row.check_id}: {row.reason}" for row in blocked)[:2000])


def findings(owner) -> list[dict]:
    from pathlib import Path
    problems = []
    for row in getattr(owner, "_verification_results", []):
        if row.status == "passed":
            continue
        folder = Path(row.report_path).parent
        check = next((c for c in getattr(owner, "_verification_checks", []) if c["id"] == row.check_id), {})
        logs = []
        for path in [folder / "application-install.log", *sorted(folder.glob("command-*.log"))]:
            if path.is_file():
                logs.append(path.name + ":\n" + path.read_text(encoding="utf-8", errors="replace")[-2000:])
        problems.append({"kind": "verification", "summary": f"Application check {row.check_id}: {row.status}",
                         "detail": "Fix the implementation while preserving the frozen check.\n" + row.reason +
                                   "\nFrozen commands and expected outcomes:\n" + json.dumps(check) +
                                   "\nCaptured command output:\n" + "\n".join(logs)[-8000:] +
                                   f"\nCheck contract: {owner.artifacts / 'verification-contract.json'}\nEvidence: {row.report_path}"})
    return problems
