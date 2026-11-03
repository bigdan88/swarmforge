# Rename

Add InventoryStore.rename(sku, name): return a replacement Item with the new name and existing quantity/price. Whitespace-only name raises ValidationError without mutation. Unknown SKU raises StoreError. Do not auto-save.

Add focused regression tests. Keep existing tests passing and avoid unrelated changes.
