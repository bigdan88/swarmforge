
import pytest
from inventory.models import Item
from inventory.store import StoreError

def test_merge(store):
    store.merge(iter([Item('AA','Apple',1,1.5), Item('DD','Dates',2,4)]))
    assert store.get('AA').quantity == 3 and store.get('DD').quantity == 2
    before = store.items()
    with pytest.raises(StoreError): store.merge([Item('EE','Egg',1,1), Item('AA','Wrong',1,1.5)])
    assert store.items() == before
    store.merge([Item('DD','Dates',1,4), Item('DD','Dates',2,4)])
    assert store.get('DD').quantity == 5
