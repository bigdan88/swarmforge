"""Scripted stand-ins for the five model roles, used only by the offline end-to-end test.

They drive the real orchestrator, git, scheduler, sandbox and test runner through the same
tool-calling protocol a real model uses."""
import json
import re

from .conftest import tool_call

EXPORTERS = '''"""CSV and JSON export of inventory items."""
from __future__ import annotations

import csv
import json
from pathlib import Path

FIELDS = ["sku", "name", "quantity", "unit_price"]


class ExportError(Exception):
    """Raised when an export cannot be written."""


def _check(path) -> Path:
    p = Path(path)
    if p.is_dir():
        raise ExportError(f"{p} is a directory")
    if not p.parent.exists():
        raise ExportError(f"directory {p.parent} does not exist")
    return p


def _rows(items):
    return [i.to_dict() for i in sorted(items, key=lambda i: i.sku)]


def export_csv(items, path) -> int:
    p = _check(path)
    rows = _rows(items)
    try:
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
    except OSError as e:
        raise ExportError(f"cannot write {p}: {e}") from e
    return len(rows)


def export_json(items, path) -> int:
    p = _check(path)
    rows = _rows(items)
    try:
        p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError as e:
        raise ExportError(f"cannot write {p}: {e}") from e
    return len(rows)


def export_items(items, path, fmt) -> int:
    f = str(fmt).lower()
    if f == "csv":
        return export_csv(items, path)
    if f == "json":
        return export_json(items, path)
    raise ExportError(f"unsupported export format {fmt!r}")
'''

EXPORTER_TESTS = '''import json
import pytest
from inventory.models import Item
from inventory.exporters import ExportError, export_csv, export_items, export_json

ITEMS = [Item("B-2", "Bolt", 2, 1.0), Item("A-1", "Anvil, big", 1, 9.5)]


def test_csv(tmp_path):
    assert export_csv(ITEMS, tmp_path / "a.csv") == 2
    lines = (tmp_path / "a.csv").read_text().splitlines()
    assert lines[0] == "sku,name,quantity,unit_price"
    assert lines[1].startswith('A-1,"Anvil, big"')


def test_json(tmp_path):
    export_json(ITEMS, tmp_path / "a.json")
    assert [d["sku"] for d in json.loads((tmp_path / "a.json").read_text())] == ["A-1", "B-2"]


def test_errors(tmp_path):
    with pytest.raises(ExportError):
        export_items(ITEMS, tmp_path / "a.x", "xml")
    with pytest.raises(ExportError):
        export_csv(ITEMS, tmp_path / "missing" / "a.csv")
'''

CLI_PATCH_OLD = '''    sub.add_parser("value", help="print total inventory value")
    return p'''
CLI_PATCH_NEW = '''    sub.add_parser("value", help="print total inventory value")

    e = sub.add_parser("export", help="export items to CSV or JSON")
    e.add_argument("--output", required=True)
    e.add_argument("--format", choices=["csv", "json"], type=str.lower)
    return p'''
CLI_PATCH2_OLD = '''        elif args.command == "value":
            print(f"{store.total_value():.2f}")
    except (StoreError, ValidationError) as e:'''
CLI_PATCH2_NEW = '''        elif args.command == "value":
            print(f"{store.total_value():.2f}")
        elif args.command == "export":
            fmt = args.format or args.output.rsplit(".", 1)[-1].lower()
            if fmt not in ("csv", "json"):
                print(f"error: cannot infer export format from {args.output}", file=sys.stderr)
                return 2
            n = export_items(store.items(), args.output, fmt)
            print(f"Exported {n} items to {args.output}")
    except (StoreError, ValidationError, ExportError) as e:'''

CLI_TESTS = '''from inventory.cli import main


def test_export_inferred(tmp_path, capsys):
    db = str(tmp_path / "i.json")
    main(["--db", db, "add", "A-1", "Anvil", "1", "2"])
    assert main(["--db", db, "export", "--output", str(tmp_path / "o.csv")]) == 0
    assert "Exported 1 items" in capsys.readouterr().out


def test_export_cannot_infer(tmp_path):
    assert main(["--db", str(tmp_path / "i.json"), "export", "--output", str(tmp_path / "o.txt")]) == 2


def test_export_error(tmp_path):
    assert main(["--db", str(tmp_path / "i.json"), "export", "--output", str(tmp_path / "no" / "o.csv")]) == 1
'''

ACCEPT_TESTS = '''from inventory.exporters import export_csv, export_json
from inventory.models import Item


def test_ac1_csv(tmp_path):
    assert export_csv([Item("A-1", "x", 1, 1.0)], tmp_path / "a.csv") == 1


def test_ac2_json(tmp_path):
    assert export_json([Item("A-1", "x", 1, 1.0)], tmp_path / "a.json") == 1
'''


def _n_assistant(body) -> int:
    return sum(1 for m in body["messages"] if m["role"] == "assistant")


def _user(body) -> str:
    return body["messages"][1]["content"]


def librarian(body):
    n = _n_assistant(body)
    if n == 0:
        return {"content": "", "tool_calls": [tool_call("read_file", {"path": "inventory/exporters.py"})]}
    notes = [
        {"title": "Exporters must wrap every OSError in ExportError", "type": "pattern", "tags": ["errors"],
         "body": "The reviewer blocked the run until file-system errors were converted. Wrap open/write calls.",
         "links": ["README must document every CLI flag"]},
        {"title": "README must document every CLI flag", "type": "review-lesson", "tags": ["docs"],
         "body": "A major review finding was raised because --format was undocumented in README.md.",
         "links": []},
    ]
    return {"content": "", "tool_calls": [tool_call("submit_notes", {"notes": notes})]}


def postmortem(body):
    return {"content": json.dumps({
        "category": "review_rejected",
        "root_cause": "The reviewer blocked the run because README.md did not document --format, and no "
                      "repair cycle was allowed to fix it.",
        "fix": "Document the --format flag in README.md, then re-run the tests.",
        "retry": True,
        "lesson": "Documentation gaps are blocking review findings; update README with every CLI change."})}


def planner(body):
    if "post-mortem analyst" in body["messages"][0]["content"]:
        return postmortem(body)
    if "Librarian" in body["messages"][0]["content"]:
        return librarian(body)
    n = _n_assistant(body)
    fix = "CORRECTIVE" in body["messages"][0]["content"]
    if n == 0:
        return {"content": "", "tool_calls": [tool_call("list_files", {"path": "."})]}
    if fix:
        plan = {"summary": "Document --format in README.", "acceptance_criteria": [], "tasks": [
            {"id": "F1", "title": "Document --format", "description": "Add --format docs to README.",
             "role": "developer_frontend", "depends_on": ["T2"], "acceptance_criteria": ["README mentions --format"],
             "expected_files": ["README.md"]}]}
    else:
        plan = {
            "summary": "Add exporters module, CLI command and docs.",
            "acceptance_criteria": [{"id": "AC1", "text": "CSV export"}, {"id": "AC2", "text": "JSON export"},
                                    {"id": "AC3", "text": "CLI export command"}],
            "tasks": [
                {"id": "T1", "title": "Exporters module", "description": "inventory/exporters.py",
                 "role": "developer_backend", "depends_on": [], "acceptance_criteria": ["AC1", "AC2"],
                 "expected_files": ["inventory/exporters.py", "tests/test_exporters.py"]},
                {"id": "T2", "title": "README docs", "description": "Exporting section",
                 "role": "developer_frontend", "depends_on": [], "acceptance_criteria": ["docs"],
                 "expected_files": ["README.md"]},
                {"id": "T3", "title": "CLI export", "description": "export subcommand",
                 "role": "developer_backend", "depends_on": ["T1"], "acceptance_criteria": ["AC3"],
                 "expected_files": ["inventory/cli.py", "tests/test_cli_export.py"]},
            ]}
    if n == 1 and not fix:
        bad = json.loads(json.dumps(plan))
        bad["tasks"][2]["depends_on"] = ["T9"]  # exercise validation feedback
        return {"content": "", "tool_calls": [tool_call("submit_plan", bad)]}
    return {"content": "", "tool_calls": [tool_call("submit_plan", plan)]}


def developer(body):
    n = _n_assistant(body)
    u = _user(body)
    task = re.search(r"## Your task (\w+)", u)
    task = task.group(1) if task else "MERGE"
    if task.startswith("R"):
        steps = [("write_file", {"path": f"repair/{task}.md",
                                "content": f"# Repair {task}\n\nAddressed reviewer finding.\n"})]
    else:
        steps = {
            "T1": [("write_file", {"path": "inventory/exporters.py", "content": EXPORTERS}),
                   ("write_file", {"path": "tests/test_exporters.py", "content": EXPORTER_TESTS})],
            "T2": [("edit_file", {"path": "README.md", "old_text": "## Development",
                                  "new_text": "## Exporting\n\n```\npython -m inventory export --output items.csv\n```\n\n## Development"})],
            "T3": [("edit_file", {"path": "inventory/cli.py", "old_text": CLI_PATCH_OLD, "new_text": CLI_PATCH_NEW}),
                   ("edit_file", {"path": "inventory/cli.py", "old_text": CLI_PATCH2_OLD, "new_text": CLI_PATCH2_NEW}),
                   ("edit_file", {"path": "inventory/cli.py", "old_text": "from .store import InventoryStore, StoreError",
                                  "new_text": "from .exporters import ExportError, export_items\nfrom .store import InventoryStore, StoreError"}),
                   ("write_file", {"path": "tests/test_cli_export.py", "content": CLI_TESTS})],
            "F1": [("edit_file", {"path": "README.md", "old_text": "export --output items.csv",
                                  "new_text": "export --output items.csv\npython -m inventory export --output data.out --format json"})],
        }[task]
    script = steps + [("run_command", {"command": "python -m pytest -q -p no:cacheprovider"}),
                      ("finish", {"summary": f"{task} done", "tests_run": "pytest"})]
    name, args = script[min(n, len(script) - 1)]
    return {"content": "", "tool_calls": [tool_call(name, args, f"c{n}")]}


def tester(body):
    n = _n_assistant(body)
    script = [
        ("write_file", {"path": "tests/test_acceptance_swarmforge.py", "content": ACCEPT_TESTS}),
        ("write_file", {"path": "inventory/models.py", "content": "# tester must not edit this\n"}),
        ("run_command", {"command": "python -m pytest -q -p no:cacheprovider"}),
        ("submit_test_report", {"summary": "ok", "defects": [], "criteria_tests": [
            {"criterion_id": "AC1", "tests": ["tests/test_acceptance_swarmforge.py::test_ac1_csv"]},
            {"criterion_id": "AC2", "tests": ["tests/test_acceptance_swarmforge.py::test_ac2_json"]},
            {"criterion_id": "AC3", "tests": ["tests/test_cli_export.py"]}]}),
    ]
    name, args = script[min(n, len(script) - 1)]
    return {"content": "", "tool_calls": [tool_call(name, args, f"t{n}")]}


class Reviewer:
    def __init__(self):
        self.sessions = 0

    def __call__(self, body):
        n = _n_assistant(body)
        if n == 0:
            self.sessions += 1
            return {"content": "", "tool_calls": [tool_call("read_file", {"path": "README.md"})]}
        if "--format" in json.dumps(body["messages"][-1]["content"]) or self.sessions > 1:
            rv = {"verdict": "approve", "summary": "Looks good.", "findings": [
                {"severity": "minor", "file": "inventory/cli.py", "line": 30, "title": "nit",
                 "description": "naming", "recommendation": "rename"}]}
        else:
            rv = {"verdict": "request_changes", "summary": "Docs incomplete.", "findings": [
                {"severity": "major", "file": "README.md", "title": "--format undocumented",
                 "description": "README does not explain --format", "recommendation": "document --format"}]}
        return {"content": "", "tool_calls": [tool_call("submit_review", rv)]}
