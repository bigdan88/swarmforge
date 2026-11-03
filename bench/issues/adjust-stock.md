# Adjust Stock

Add InventoryStore.adjust(sku, delta): delta must be an integer but not bool; raise ValidationError for invalid delta or negative resulting quantity, StoreError for unknown SKU. Replace frozen Item, return updated Item, preserve name/price, no automatic save.

Add focused regression tests. Keep existing tests passing and avoid unrelated changes.
