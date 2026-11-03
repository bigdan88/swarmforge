
import pytest
from inventory.store import StoreError

def test_csv(tmp_path):
    from inventory.importers import load_csv
    p = tmp_path / 'input.csv'
    p.write_text('sku,name,quantity,unit_price\nBB,"Pear, red",1,2.50\nAA,Apple,2,1.50\n')
    rows = load_csv(p)
    assert [i.sku for i in rows] == ['AA', 'BB'] and rows[1].name == 'Pear, red'
    p.write_text('sku,name,quantity,unit_price\nAA,A,2,1\nAA,B,1,1\n')
    with pytest.raises(StoreError): load_csv(p)
    p.write_text('sku,name\nAA,A\n')
    with pytest.raises(StoreError): load_csv(p)
    p.write_text('sku,name,quantity,unit_price\nAA,A,-1,2.50\n')
    with pytest.raises(StoreError): load_csv(p)
    with pytest.raises(StoreError): load_csv(tmp_path / 'missing')
