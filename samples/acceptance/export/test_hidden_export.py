"""Hidden acceptance tests for the CSV/JSON export issue.

Used only by `swarmforge bench --acceptance`: copied into a fresh checkout of each result branch
AFTER the agents finish, so no agent ever sees them.
"""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from inventory.models import Item

ITEMS = [
    Item("C-3", 'Cable, "braided"', 5, 3.0),
    Item("A-1", "Anvil", 1, 99.5),
    Item("B-2", "Bolt", 100, 0.25),
]


def _exp():
    import inventory.exporters as e
    return e


def test_csv_header_sorted_and_quoted(tmp_path):
    out = tmp_path / "x.csv"
    n = _exp().export_csv(ITEMS, out)
    assert n == 3
    rows = list(csv.reader(out.open(encoding="utf-8", newline="")))
    assert rows[0] == ["sku", "name", "quantity", "unit_price"]
    assert [r[0] for r in rows[1:]] == ["A-1", "B-2", "C-3"]
    assert rows[3][1] == 'Cable, "braided"'
    assert float(rows[1][3]) == 99.5 and int(rows[2][2]) == 100


def test_json_sorted_keys(tmp_path):
    out = tmp_path / "x.json"
    n = _exp().export_json(ITEMS, out)
    assert n == 3
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [d["sku"] for d in data] == ["A-1", "B-2", "C-3"]
    assert set(data[0]) == {"sku", "name", "quantity", "unit_price"}
    assert data[0]["unit_price"] == 99.5


def test_empty_exports(tmp_path):
    e = _exp()
    assert e.export_csv([], tmp_path / "e.csv") == 0
    rows = list(csv.reader((tmp_path / "e.csv").open(encoding="utf-8", newline="")))
    assert rows == [["sku", "name", "quantity", "unit_price"]]
    assert e.export_json([], tmp_path / "e.json") == 0
    assert json.loads((tmp_path / "e.json").read_text()) == []


@pytest.mark.parametrize("fmt", ["csv", "JSON"])
def test_dispatch(tmp_path, fmt):
    assert _exp().export_items(ITEMS, tmp_path / f"d.{fmt.lower()}", fmt) == 3


def test_unknown_format(tmp_path):
    e = _exp()
    with pytest.raises(e.ExportError):
        e.export_items(ITEMS, tmp_path / "x.xml", "xml")


def test_missing_parent_and_directory(tmp_path):
    e = _exp()
    with pytest.raises(e.ExportError):
        e.export_csv(ITEMS, tmp_path / "nope" / "x.csv")
    with pytest.raises(e.ExportError):
        e.export_json(ITEMS, tmp_path)


def _cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "inventory", *args], capture_output=True, text=True,
                          cwd=cwd)


@pytest.fixture
def db(tmp_path):
    from inventory.store import InventoryStore
    p = tmp_path / "inv.json"
    s = InventoryStore(p)
    for i in ITEMS:
        s.add(i)
    s.save()
    return p


ROOT = Path(__file__).resolve().parents[1]


def test_cli_inferred_csv(db, tmp_path):
    out = tmp_path / "o.CSV"
    r = _cli("--db", str(db), "export", "--output", str(out), cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "Exported 3 items to" in r.stdout
    assert out.read_text(encoding="utf-8").splitlines()[0] == "sku,name,quantity,unit_price"


def test_cli_explicit_json(db, tmp_path):
    out = tmp_path / "o.data"
    r = _cli("--db", str(db), "export", "--output", str(out), "--format", "json", cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert len(json.loads(out.read_text())) == 3


def test_cli_cannot_infer(db, tmp_path):
    r = _cli("--db", str(db), "export", "--output", str(tmp_path / "o.txt"), cwd=ROOT)
    assert r.returncode == 2
    assert "cannot infer" in r.stderr


def test_cli_bad_format(db, tmp_path):
    r = _cli("--db", str(db), "export", "--output", str(tmp_path / "o.csv"), "--format", "xml", cwd=ROOT)
    assert r.returncode == 2


def test_cli_export_error_exit_1(db, tmp_path):
    r = _cli("--db", str(db), "export", "--output", str(tmp_path / "missing" / "o.csv"), cwd=ROOT)
    assert r.returncode == 1
    assert r.stderr.startswith("error:")


def test_readme_documents_export():
    text = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "export" in text and "--output" in text
