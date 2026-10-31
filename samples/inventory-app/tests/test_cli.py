from inventory.cli import main


def test_add_list_value(tmp_path, capsys):
    db = str(tmp_path / "inv.json")
    assert main(["--db", db, "add", "A-1", "Anvil", "2", "10"]) == 0
    assert main(["--db", db, "add", "B-2", "Bolt", "4", "0.5"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "A-1" in out and "B-2" in out
    assert main(["--db", db, "value"]) == 0
    assert capsys.readouterr().out.strip() == "22.00"


def test_remove_unknown_sku_errors(tmp_path, capsys):
    db = str(tmp_path / "inv.json")
    assert main(["--db", db, "remove", "NOPE-1"]) == 1
    assert "unknown SKU" in capsys.readouterr().err
