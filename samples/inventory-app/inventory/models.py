"""Domain model for the inventory tracker."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")


class ValidationError(ValueError):
    """Raised when an item has invalid field values."""


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    quantity: int
    unit_price: float

    def __post_init__(self) -> None:
        if not SKU_RE.match(self.sku):
            raise ValidationError(f"invalid SKU {self.sku!r}: use 2-32 chars A-Z, 0-9 and '-'")
        if not self.name.strip():
            raise ValidationError("name must not be empty")
        if not isinstance(self.quantity, int) or self.quantity < 0:
            raise ValidationError("quantity must be a non-negative integer")
        if self.unit_price < 0:
            raise ValidationError("unit_price must be >= 0")

    @property
    def total_value(self) -> float:
        return round(self.quantity * self.unit_price, 2)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Item":
        return cls(sku=data["sku"], name=data["name"], quantity=int(data["quantity"]),
                   unit_price=float(data["unit_price"]))
