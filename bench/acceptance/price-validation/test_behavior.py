
import pytest
from inventory.models import Item, ValidationError

@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), True, '2.0', None, -1])
def test_bad_prices(value):
    with pytest.raises(ValidationError): Item('AA', 'Apple', 1, value)

def test_valid_prices():
    assert Item('AA','Apple',1,0).unit_price == 0
    assert Item('AA','Apple',1,2.5).total_value == 2.5
