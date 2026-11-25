"""Frozen application checks and runner-owned evidence, separate from unit tests.

Commands use the existing sandbox policy. A project cannot grant itself network,
GPU, host-file or credential access through its verification contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from .sandbox import DEPS_CONTAINER_PATH, DEPS_DIR, Sandbox

LEVELS = {"unit": 0, "integration": 1, "product": 2}
CONTRACT_FILE = "swarmforge.verify.json"
MAX_CHECKS = 12
MAX_STEPS = 20
ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class VerificationError(ValueError):
    pass


def relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise VerificationError("Verification paths must be relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise VerificationError("Verification paths must stay in the verification workspace")
    if any(part in {".git", ".env", ".aws", ".ssh"} for part in path.parts):
        raise VerificationError("Verification cannot read repository metadata or credentials")
    return value


def _strings(value: object, label: str, maximum: int = 30) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or not all(isinstance(x, str) and x.strip() for x in value):
        raise VerificationError(f"{label} must be a bounded list of nonempty strings")
    return value


def parse_checks(raw: object) -> list[dict]:
    """Validate a declarative contract before developers start; reject unknown knobs."""
    if not isinstance(raw, list) or len(raw) > MAX_CHECKS:
        raise VerificationError(f"verification_checks must contain at most {MAX_CHECKS} checks")
    out, ids = [], set()
    for case in raw:
        if not isinstance(case, dict) or set(case) - {"id", "criteria", "level", "preflight", "steps", "fixtures", "timeout_s"}:
            raise VerificationError("Unknown verification check fields")
        cid = case.get("id")
        if not isinstance(cid, str) or not ID.fullmatch(cid) or cid in ids:
            raise VerificationError("Verification check IDs must be valid and unique")
        ids.add(cid)
        level = case.get("level", "product")
        if not isinstance(level, str) or level not in {"integration", "product"}:
            raise VerificationError("Executable checks must be integration or product level")
        criteria = _strings(case.get("criteria"), "Verification criteria", 20)
        if not criteria or any(not ID.fullmatch(c) for c in criteria) or len(set(criteria)) != len(criteria):
            raise VerificationError("A verification check must identify distinct acceptance criteria or requirement IDs")
        timeout = case.get("timeout_s", 120)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 1800:
            raise VerificationError("Verification timeout must be between 1 and 1800 seconds")
        preflight = case.get("preflight", {})
        if not isinstance(preflight, dict) or set(preflight) - {"executables", "modules", "files", "cuda", "platform"}:
            raise VerificationError("Unknown verification preflight fields")
        for key in ("executables", "modules", "files"):
            _strings(preflight.get(key, []), f"preflight.{key}")
        for name in preflight.get("executables", []):
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
                raise VerificationError("Preflight executables must be binary names, not paths")
        for name in preflight.get("modules", []):
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name):
                raise VerificationError("Invalid preflight module")
        for file in preflight.get("files", []):
            relative_path(file)
        if type(preflight.get("cuda", False)) is not bool:
            raise VerificationError("preflight.cuda must be boolean")
        if preflight.get("platform") not in (None, "Windows", "Linux", "Darwin"):
            raise VerificationError("preflight.platform must be Windows, Linux or Darwin")
        fixtures = _strings(case.get("fixtures", []), "Verification fixtures")
        for path in fixtures:
            relative_path(path)
        steps = case.get("steps")
        if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
            raise VerificationError("A check needs 2-20 steps including execution and an output assertion")
        executed, asserted = False, False
        for step in steps:
            if not isinstance(step, dict):
                raise VerificationError("Verification steps must be objects")
            kind = step.get("kind")
            fields = {
                "command": {"kind", "argv", "background", "expected_exit"},
                "file": {"kind", "path", "contains", "min_bytes"},
                "json": {"kind", "path", "pointer", "equals", "min_count"},
                "sqlite": {"kind", "path", "query", "contains", "min_rows"},
                "http": {"kind", "url", "status", "contains", "headers"},
                "browser": {"kind", "url", "actions", "contains", "media_time_s"},
            }
            if not isinstance(kind, str) or kind not in fields or set(step) - fields[kind]:
                raise VerificationError("Unknown verification step kind or fields")
            if kind == "command":
                argv = _strings(step.get("argv"), "command.argv", 60)
                if not argv or type(step.get("background", False)) is not bool or type(step.get("expected_exit", 0)) is not int:
                    raise VerificationError("Invalid command step")
                # Product evidence must exercise the application, not promote a pytest run.
                if level == "product" and any(re.search(r"(?:^|[/\\])(?:pytest|unittest)(?:$|[.])", x, re.I) for x in argv):
                    raise VerificationError("Product checks must run the application entry point, not a unit-test runner")
                executed = True
            else:
                asserted = True
                if kind in {"file", "json", "sqlite"}:
                    relative_path(step.get("path"))
                    if step["path"] in fixtures or step["path"] in preflight.get("files", []):
                        raise VerificationError("An observed output cannot also be a fixture or preflight input")
                if kind in {"http", "browser"}:
                    from urllib.parse import urlsplit
                    url = urlsplit(str(step.get("url", "")))
                    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.username or url.password:
                        raise VerificationError("Verification URLs must be local HTTP URLs without credentials")
                if "contains" in step and (not isinstance(step["contains"], str) or not step["contains"].strip()):
                    raise VerificationError("contains must be nonempty text")
                for key in ("min_bytes", "min_count", "min_rows", "status"):
                    if key in step and (type(step[key]) is not int or step[key] < 0):
                        raise VerificationError(f"{key} must be a nonnegative integer")
                if kind == "json" and not ("equals" in step or "min_count" in step):
                    raise VerificationError("JSON assertions need equals or min_count")
                if kind == "sqlite":
                    if not isinstance(step.get("query"), str) or not re.match(r"^\s*SELECT\b", step["query"], re.I):
                        raise VerificationError("SQLite assertions require a read-only SELECT query")
                    if "contains" not in step and step.get("min_rows", 1) < 1:
                        raise VerificationError("SQLite assertions must check returned content")
                if kind == "http" and (not isinstance(step.get("headers", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in step.get("headers", {}).items())):
                    raise VerificationError("HTTP header expectations must map strings to strings")
                if kind == "browser":
                    actions = step.get("actions", [])
                    if not isinstance(actions, list) or len(actions) > 12:
                        raise VerificationError("Browser actions must be a bounded list")
                    for action in actions:
                        if not isinstance(action, dict) or not isinstance(action.get("kind"), str) or action.get("kind") not in {"click", "fill", "press"} or set(action) - {"kind", "selector", "value"} or not isinstance(action.get("selector"), str):
                            raise VerificationError("Browser actions require kind, selector and optional value")
                    if "media_time_s" in step and (type(step["media_time_s"]) not in (int, float) or not math.isfinite(step["media_time_s"]) or step["media_time_s"] < 0):
                        raise VerificationError("Expected media time must be finite and nonnegative")
                    if "contains" not in step and "media_time_s" not in step:
                        raise VerificationError("Browser steps need an observable text or playback expectation")
        if not executed or not asserted:
            raise VerificationError("A verification check must execute the app and assert its output")
        out.append({"id": cid, "criteria": criteria, "level": level, "preflight": preflight,
                    "steps": steps, "fixtures": fixtures, "timeout_s": timeout})
    return out


def contract_hash(checks: list[dict]) -> str:
    return hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class VerificationEvidence:
    check_id: str
    criteria: list[str]
    level: str
    status: str
    reason: str
    commit_sha: str
    contract_hash: str
    report_path: str = ""


def prepare_workspace(source: Path, target: Path) -> Path:
    """Copy source without user data, installed dependencies, credentials or git state."""
    skip = {".git", ".swarmforge", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
            ".env", ".aws", ".ssh", "data", "build", "dist"}
    def ignored(directory, names):
        return [n for n in names if n in skip or n.endswith((".db", ".sqlite", ".sqlite3", ".bak"))
                or n.startswith(".env") or n in {"credentials", "secrets.json", "secrets.yaml", "id_rsa", "id_ed25519"}
                or n.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm"))
                or (Path(directory) / n).is_symlink()
                or not (Path(directory) / n).resolve().is_relative_to(source.resolve())]
    shutil.copytree(source, target, ignore=ignored)
    return target


async def run_checks(sandbox: Sandbox, workspace: Path, checks: list[dict], artifacts: Path,
                     *, commit_sha: str, timeout_s: int, cycle: int = 0) -> list[VerificationEvidence]:
    """Execute the trusted probe in the existing sandbox, with one shared time allowance."""
    checks = parse_checks(checks)
    artifacts.mkdir(parents=True, exist_ok=True)
    probe = resources.files("swarmforge").joinpath("resources/product_probe.py").read_text(encoding="utf-8")
    deadline = time.monotonic() + timeout_s
    results = []
    for check in checks:
        folder = artifacts / f"verification-c{cycle}-{check['id']}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.json").unlink(missing_ok=True)
        digest = contract_hash([check])
        request = {"check": check, "commit_sha": commit_sha, "contract_hash": digest}
        (folder / "request.json").write_text(json.dumps(request), encoding="utf-8")
        (folder / "probe.py").write_text(probe, encoding="utf-8")
        # Both sandbox implementations already translate /artifacts paths on the host.
        command = 'python "/artifacts/probe.py" "/artifacts/request.json" "/artifacts/result.json"'
        remaining = min(check["timeout_s"], deadline - time.monotonic())
        status, reason = "blocked", "Verification time allowance exhausted before this check"
        if remaining > 0:
            import uuid
            isolated = workspace.parent / (workspace.name + "-" + check["id"] + "-" + uuid.uuid4().hex[:8])
            prepare_workspace(workspace, isolated)
            # Unit tests or an earlier manual run may have left generated files
            # in the source checkout. They must not become proof of this run.
            for step in check["steps"]:
                if step["kind"] in {"file", "json", "sqlite"}:
                    output = (isolated / step["path"]).resolve()
                    output.relative_to(isolated.resolve())
                    output.unlink(missing_ok=True)
            dependencies = workspace / DEPS_DIR
            if dependencies.is_dir():
                shutil.copytree(dependencies, isolated / DEPS_DIR)
            remaining = min(check["timeout_s"], deadline - time.monotonic())
            if remaining <= 0:
                evidence = VerificationEvidence(check["id"], check["criteria"], check["level"], "blocked",
                                                "Verification time allowance exhausted preparing the workspace", commit_sha, digest)
                results.append(evidence)
                continue
            completed = await sandbox.run(isolated, command, timeout_s=max(1, math.ceil(remaining)), artifacts_dir=folder)
            (folder / "execution.log").write_text(completed.summary(30000), encoding="utf-8")
            status, reason = "failed", "Verification runner did not produce a valid result"
            if completed.timed_out:
                reason = "Application verification timed out; inspect execution.log"
            elif (folder / "result.json").is_file():
                try:
                    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
                    if result.get("commit_sha") == commit_sha and result.get("contract_hash") == digest and result.get("status") in {"passed", "empty", "blocked", "failed"}:
                        status, reason = result["status"], str(result.get("reason", ""))[:6000]
                        if completed.exit_code != 0 and status in {"passed", "empty"}:
                            status, reason = "failed", "Verification process exited unsuccessfully"
                except (OSError, ValueError, TypeError):
                    pass
        evidence = VerificationEvidence(check["id"], check["criteria"], check["level"], status, reason,
                                        commit_sha, digest, str(folder / "result.json"))
        (folder / "evidence.json").write_text(json.dumps(asdict(evidence), indent=2), encoding="utf-8")
        results.append(evidence)
    return results


async def install_application(sandbox: Sandbox, workspace: Path, artifacts: Path, timeout_s: int) -> str | None:
    """Install the app itself into scoped dependencies so its documented entry point works."""
    package = any((workspace / name).is_file() for name in ("setup.py", "setup.cfg"))
    if (workspace / "pyproject.toml").is_file():
        import tomllib
        try:
            metadata = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            return "The generated application package metadata is invalid: " + str(exc)
        package = package or "project" in metadata or "poetry" in metadata.get("tool", {})
    if not package:
        return None
    artifacts.mkdir(parents=True, exist_ok=True)
    command = f'python -m pip install --no-deps --no-build-isolation --target "{DEPS_CONTAINER_PATH}" .'
    result = await sandbox.run(workspace, command, artifacts_dir=artifacts, timeout_s=timeout_s)
    (artifacts / "application-install.log").write_text(result.summary(30000), encoding="utf-8")
    if result.exit_code or result.timed_out:
        return "The application could not be installed in the fresh verification workspace; inspect application-install.log"
    return None


def enforce_evidence(results: list[dict], criteria: list, evidence: list[VerificationEvidence],
                     *, commit_sha: str) -> list[dict]:
    """Unit tests cannot satisfy stronger requirements, regardless of model claims."""
    for row, criterion in zip(results, criteria):
        required = getattr(criterion, "evidence_level", "unit")
        rid = getattr(criterion, "requirement_id", None)
        row.update(requirement_id=rid, evidence_level="unit", required_level=required, commit_sha=commit_sha)
        reference = rid or criterion.id
        matches = [e for e in evidence if reference in e.criteria
                   and LEVELS[e.level] >= LEVELS[required] and e.commit_sha == commit_sha]
        if not matches:
            if required != "unit" and row["status"] != "failed":
                row.update(status="unverified", evidence=f"{required} execution required; passing unit tests are insufficient. Add a frozen verification check for {rid or criterion.id}.")
            continue
        failed = [e for e in matches if e.status in {"failed", "empty"}]
        blocked = [e for e in matches if e.status == "blocked"]
        selected = failed or blocked or matches
        row["evidence_level"] = max((e.level for e in selected), key=LEVELS.__getitem__)
        row["evidence"] = "\n".join(f"{e.check_id}: {e.status}: {e.reason} ({e.report_path})" for e in selected)
        if row["status"] == "failed" or failed:
            row["status"] = "failed"
        elif blocked:
            row["status"] = "unverified"
        else:
            row["status"] = "satisfied"
    return results


def project_requirements(definition: dict, through: int | None = None) -> list[dict]:
    """IDs stay stable when criterion display wording is paraphrased by a planner.

    IDs are positional inside the human-owned, byte-protected specification.
    Contracts are separately scoped to that specification's hash.
    """
    rows = []
    for mi, milestone in enumerate(definition["milestones"]):
        if through is not None and mi > through:
            break
        levels = milestone.get("evidence_requirements", {})
        for ci, text in enumerate(milestone["checks"]):
            rid = f"M{mi + 1}C{ci + 1}"
            rows.append({"id": rid, "text": text, "evidence_level": levels.get(rid, "product")})
    return rows


def bind_requirements(plan, catalog: list[dict], assigned: list[str]):
    """A model cannot downgrade or omit a task's assigned approved requirements."""
    from .plan import Criterion
    known = {r["id"]: r for r in catalog}
    for row in plan.acceptance_criteria:
        # Compatibility with old plans; new evidence always records the stable ID.
        if row.requirement_id is None:
            match = [r for r in catalog if r["text"] == row.text]
            if len(match) == 1:
                row.requirement_id = match[0]["id"]
        if row.requirement_id:
            if row.requirement_id not in known:
                raise VerificationError(f"Unknown approved requirement: {row.requirement_id}")
            minimum = known[row.requirement_id]["evidence_level"]
            if LEVELS[minimum] > LEVELS[row.evidence_level]:
                row.evidence_level = minimum
    represented = {r.requirement_id for r in plan.acceptance_criteria}
    for rid in assigned:
        if rid not in known:
            raise VerificationError(f"Unknown assigned requirement: {rid}")
        if rid not in represented:
            if any(c.id == rid for c in plan.acceptance_criteria):
                raise VerificationError(f"Criterion ID {rid} collides with an assigned requirement")
            requirement = known[rid]
            plan.acceptance_criteria.append(Criterion(rid, requirement["text"], requirement["evidence_level"], rid))
    return plan


def freeze_checks(existing: list[dict], proposed: list[dict], criteria: list) -> list[dict]:
    """Only append new checks; repairs and later tasks cannot weaken old expectations."""
    combined = {case["id"]: case for case in parse_checks(existing)}
    for case in parse_checks(proposed):
        if case["id"] in combined and case != combined[case["id"]]:
            raise VerificationError(f"Frozen verification check {case['id']} cannot be changed; repair the implementation")
        combined[case["id"]] = case
    checks = parse_checks(list(combined.values()))
    for criterion in criteria:
        if criterion.evidence_level != "unit" and not any(
                (criterion.requirement_id or criterion.id) in case["criteria"]
                and LEVELS[case["level"]] >= LEVELS[criterion.evidence_level] for case in checks):
            raise VerificationError(f"{criterion.requirement_id or criterion.id} requires a frozen {criterion.evidence_level} application check before coding")
    return checks
