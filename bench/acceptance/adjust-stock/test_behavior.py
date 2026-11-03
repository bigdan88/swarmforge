
import pytest
from inventory.models import ValidationError
from inventory.store import StoreError

def test_adjust(store):
    assert store.adjust('AA', -2).quantity == 0
    assert store.adjust('AA', 5).quantity == 5
    assert store.get('AA').name == 'Apple'
    assert store.get('AA').unit_price == 1.5
    with pytest.raises(ValidationError): store.adjust('AA', -6)
    assert store.get('AA').quantity == 5
    with pytest.raises(ValidationError): store.adjust('AA', True)
    with pytest.raises(ValidationError): store.adjust('AA', 1.5)
    with pytest.raises(StoreError): store.adjust('ZZ', 1)
