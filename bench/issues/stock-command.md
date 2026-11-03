# Stock Command

Add a CLI command stock SKU DELTA that adjusts stock, rejects non-integer/negative resulting quantity, saves on success and prints the resulting quantity. Implement InventoryStore.adjust(sku, delta) returning updated Item. Unknown SKU or negative quantity must exit 1 without changing persisted inventory. Existing commands stay compatible.

Add focused regression tests. Keep existing tests passing and avoid unrelated changes.
