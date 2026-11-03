import pytest
from inventory.models import Item
from inventory.store import InventoryStore

@pytest.fixture
def store(tmp_path):
    store = InventoryStore(tmp_path / 'inventory.json')
    for item in [Item('AA', 'Apple', 2, 1.5), Item('BB', 'Pear', 1, 2.5), Item('CC', 'Pineapple', 4, 1.875)]:
        store.add(item)
    return store
