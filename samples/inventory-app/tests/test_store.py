import pytest

from inventory.models import Item
from inventory.store import InventoryStore, StoreError


def test_add_save_reload(tmp_path):
    db = tmp_path / "inv.json"
    s = InventoryStore(db)
    s.add(Item("B-2", "Bolt", 10, 0.25))
    s.add(Item("A-1", "Anvil", 1, 99.0))
    s.save()
    s2 = InventoryStore(db)
    assert [i.sku for i in s2.items()] == ["A-1", "B-2"]
    assert s2.total_value() == 101.5


def test_duplicate_and_unknown(tmp_path):
    s = InventoryStore(tmp_path / "inv.json")
    s.add(Item("A-1", "Anvil", 1, 99.0))
    with pytest.raises(StoreError):
        s.add(Item("A-1", "Anvil", 1, 99.0))
    with pytest.raises(StoreError):
        s.remove("ZZ-9")


def test_corrupt_file(tmp_path):
    db = tmp_path / "inv.json"
    db.write_text("{not json")
    with pytest.raises(StoreError):
        InventoryStore(db)
