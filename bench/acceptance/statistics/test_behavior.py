
def test_statistics(store):
    from inventory.stats import summarize
    expected = {'item_count': 3, 'total_quantity': 7, 'total_value': 13.0}
    assert summarize(iter(store.items())) == expected
    assert store.summary() == expected
    assert summarize([]) == {'item_count': 0, 'total_quantity': 0, 'total_value': 0}
