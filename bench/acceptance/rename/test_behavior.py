
import pytest
from inventory.models import ValidationError
from inventory.store import StoreError

def test_rename(store):
    result = store.rename('AA', 'Green apple')
    assert result.name == store.get('AA').name == 'Green apple'
    assert result.quantity == 2 and result.unit_price == 1.5
    with pytest.raises(ValidationError): store.rename('AA', '  ')
    assert store.get('AA').name == 'Green apple'
    with pytest.raises(StoreError): store.rename('ZZ', 'X')
