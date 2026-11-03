# Merge Inventory

Add InventoryStore.merge(items): atomically merge an iterable of Item. New SKUs are added; existing matching name/price SKUs have quantities summed. Mismatched name/price raises StoreError and leaves entire store unchanged. Handle generators and repeated SKUs in input. Return None; do not auto-save.

Add focused regression tests. Keep existing tests passing and avoid unrelated changes.
