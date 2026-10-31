"""Deterministic test execution and evidence collection.

Test outcomes come only from actually executing the configured test command in the sandbox
and reading its exit code and JUnit XML. No LLM can mark tests as passed.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import CommandResult, Sandbox


@dataclass
class TestEvidence:
    __test__ = False  # not a pytest test class
    label: str
    command: str
    exit_code: int
    timed_out: bool
    duration_s: float
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    junit_found: bool = False
    cases: dict[str, str] = field(default_factory=dict)  # normalized id -> passed|failed|error|skipped
    failures: list[dict] = field(default_factory=list)   # {id, message, text}
    log_path: str | None = None
    junit_path: str | None = None
    output_tail: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def ok(self) -> bool:
        """Passing requires exit 0 AND (when JUnit is available) at least one executed test."""
        if self.timed_out or self.exit_code != 0:
            return False
        if self.junit_found:
            return self.passed > 0 and self.failed == 0 and self.errors == 0
        return True

    def short(self) -> str:
        if self.timed_out:
            return f"TIMED OUT after {self.duration_s:.0f}s"
        if self.junit_found:
            return (f"exit {self.exit_code}: {self.passed} passed, {self.failed} failed, "
                    f"{self.errors} errors, {self.skipped} skipped")
        return f"exit {self.exit_code} (no JUnit report)"

    def failure_report(self, limit: int = 12_000) -> str:
        parts = [f"Test command: {self.command}", f"Result: {self.short()}"]
        for f in self.failures[:20]:
            parts.append(f"--- {f['id']} ---\n{f.get('message', '')}\n{f.get('text', '')[:2500]}")
        if not self.failures:
            parts.append("Output tail:\n" + self.output_tail[-6000:])
        s = "\n".join(parts)
        return s[:limit]


def normalize_test_id(s: str) -> str:
    """'tests/test_x.py::TestA::test_b[1]' and junit 'tests.test_x.TestA'+'test_b[1]' map to
    the same string 'tests.test_x.TestA.test_b[1]'."""
    s = s.strip().replace("\\", "/")
    m = re.match(r"^(.*?)(\[.*\])?$", s)
    core, param = m.group(1), m.group(2) or ""
    core = re.sub(r"\.py(?=::|$)", "", core).replace("::", ".").replace("/", ".")
    return core.strip(".") + param


def parse_junit(path: Path) -> tuple[dict[str, str], list[dict], dict[str, int]]:
    tree = ET.parse(path)
    cases: dict[str, str] = {}
    failures: list[dict] = []
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for tc in tree.iter("testcase"):
        cid = normalize_test_id(f"{tc.get('classname', '')}.{tc.get('name', '')}")
        status = "passed"
        for child in tc:
            if child.tag == "failure":
                status = "failed"
            elif child.tag == "error":
                status = "error"
            elif child.tag == "skipped":
                status = "skipped"
            if child.tag in ("failure", "error"):
                failures.append({"id": cid, "message": child.get("message", ""),
                                 "text": (child.text or "")})
        cases[cid] = status
        counts[{"passed": "passed", "failed": "failed", "error": "errors", "skipped": "skipped"}[status]] += 1
    return cases, failures, counts


def with_junit(command: str, junit_container_path: str) -> str:
    if "pytest" in command and "--junitxml" not in command:
        return f"{command} --junitxml={junit_container_path}"
    return command


async def run_tests(sandbox: Sandbox, workspace: Path, command: str, artifacts_dir: Path,
                    label: str, timeout_s: int) -> TestEvidence:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
    junit_host = artifacts_dir / f"junit-{safe}.xml"
    if junit_host.exists():
        junit_host.unlink()
    cmd = with_junit(command, f"/artifacts/junit-{safe}.xml")
    res: CommandResult = await sandbox.run(workspace, cmd, timeout_s=timeout_s, artifacts_dir=artifacts_dir)
    log_path = artifacts_dir / f"test-{safe}.log"
    log_path.write_text(f"$ {cmd}\nexit={res.exit_code} timed_out={res.timed_out}\n\n{res.output}",
                        encoding="utf-8")
    ev = TestEvidence(label=label, command=cmd, exit_code=res.exit_code, timed_out=res.timed_out,
                      duration_s=res.duration_s, log_path=str(log_path), output_tail=res.output[-8000:])
    if junit_host.is_file():
        try:
            cases, failures, counts = parse_junit(junit_host)
            ev.cases, ev.failures, ev.junit_found = cases, failures, True
            ev.passed, ev.failed, ev.errors, ev.skipped = (
                counts["passed"], counts["failed"], counts["errors"], counts["skipped"])
            ev.junit_path = str(junit_host)
        except ET.ParseError:
            pass
    return ev


def evaluate_criteria(mapping: dict[str, list[str]], criteria: list[tuple[str, str]],
                      ev: TestEvidence) -> list[dict]:
    """Decide per acceptance criterion from executed-test evidence only.

    satisfied  - >=1 mapped test executed and every mapped test that executed passed
    failed     - some mapped test executed and failed/errored
    unverified - no mapped test was found among executed tests
    """
    out = []
    for cid, text in criteria:
        tests = mapping.get(cid, [])
        matched: dict[str, str] = {}
        for t in tests:
            nt = normalize_test_id(t)
            for case_id, status in ev.cases.items():
                if case_id == nt or case_id.startswith(nt + ".") or case_id.startswith(nt + "["):
                    matched[case_id] = status
        executed = {k: v for k, v in matched.items() if v != "skipped"}
        if not executed:
            status, evidence = "unverified", (
                "no mapped tests were executed" if tests else "no tests mapped to this criterion")
        elif all(v == "passed" for v in executed.values()):
            status, evidence = "satisfied", f"{len(executed)} test(s) passed: " + ", ".join(sorted(executed)[:8])
        else:
            bad = [k for k, v in executed.items() if v != "passed"]
            status, evidence = "failed", "failing: " + ", ".join(sorted(bad)[:8])
        out.append({"criterion_id": cid, "text": text, "status": status, "evidence": evidence})
    return out
