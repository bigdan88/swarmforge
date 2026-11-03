# Csv Import

Create inventory.importers.load_csv(path) returning a SKU-sorted list of Item. CSV header is sku,name,quantity,unit_price. Parse quoted names correctly. Missing columns, duplicate SKUs, invalid rows, missing/unreadable files raise inventory.store.StoreError. Do not change inventory or write files.

Add focused regression tests. Keep existing tests passing and avoid unrelated changes.
