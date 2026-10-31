import pytest

from inventory.models import Item, ValidationError


def test_valid_item_total_value():
    item = Item("ABC-1", "Widget", 3, 2.5)
    assert item.total_value == 7.5


@pytest.mark.parametrize("sku", ["a", "abc", "", "X" * 40, "AB C"])
def test_invalid_sku(sku):
    with pytest.raises(ValidationError):
        Item(sku, "Widget", 1, 1.0)


def test_negative_quantity_rejected():
    with pytest.raises(ValidationError):
        Item("ABC-1", "Widget", -1, 1.0)


def test_roundtrip_dict():
    item = Item("ABC-1", "Widget", 3, 2.5)
    assert Item.from_dict(item.to_dict()) == item
