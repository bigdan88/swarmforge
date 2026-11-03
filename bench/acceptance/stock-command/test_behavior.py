
import pytest
from inventory.cli import main
from inventory.store import InventoryStore

def test_stock_cli(store, capsys):
    store.save()
    assert main(['--db', str(store.path), 'stock', 'AA', '3']) == 0
    assert '5' in capsys.readouterr().out
    assert InventoryStore(store.path).get('AA').quantity == 5
    assert main(['--db', str(store.path), 'stock', 'AA', '-6']) == 1
    assert InventoryStore(store.path).get('AA').quantity == 5
    assert main(['--db', str(store.path), 'stock', 'ZZ', '1']) == 1
