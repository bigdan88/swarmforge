
def test_search(store):
    assert [i.sku for i in store.search('apple')] == ['AA', 'CC']
    assert [i.sku for i in store.search('bb')] == ['BB']
    assert len(store.search('')) == 3
    assert store.search('missing') == []
    assert len(store.items()) == 3
