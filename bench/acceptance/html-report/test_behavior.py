
from html.parser import HTMLParser

def test_html(store):
    from inventory.html_report import render_inventory
    from inventory.models import Item
    text = render_inventory([Item('BB', '<script>alert(1)</script>', 2, 1.5), store.get('AA')])
    assert '<html' in text.lower() and '</html>' in text.lower()
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert '<table' in text.lower() and '<th' in text.lower() and '1.50' in text
    assert text.index('AA') < text.index('BB')
    assert '<table' in render_inventory([]).lower()
