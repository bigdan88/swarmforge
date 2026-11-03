# Price Validation

Fix Item.unit_price validation: reject NaN, positive/negative infinity, booleans and non-numeric strings/None with ValidationError. Continue accepting finite non-negative int/float values. Preserve current SKU/name/quantity validation behavior.

Add focused regression tests. Keep existing tests passing and avoid unrelated changes.
