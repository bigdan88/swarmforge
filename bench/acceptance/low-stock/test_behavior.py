
import pytest
from inventory.models import ValidationError

def test_low_stock(store):
    assert [i.sku for i in store.low_stock(2)] == ['AA', 'BB']
    assert store.low_stock(0) == []
    for value in [-1, True, 1.5]:
        with pytest.raises(ValidationError): store.low_stock(value)
