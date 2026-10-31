"""JSON-file backed inventory storage."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Item


class StoreError(Exception):
    """Raised for storage problems (corrupt file, unknown SKU, duplicates)."""


class InventoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._items: dict[str, Item] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = {d["sku"]: Item.from_dict(d) for d in data.get("items", [])}
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise StoreError(f"corrupt inventory file {self.path}: {e}") from e

    def save(self) -> None:
        payload = {"items": [i.to_dict() for i in self.items()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def items(self) -> list[Item]:
        return sorted(self._items.values(), key=lambda i: i.sku)

    def get(self, sku: str) -> Item:
        try:
            return self._items[sku]
        except KeyError:
            raise StoreError(f"unknown SKU {sku}") from None

    def add(self, item: Item) -> None:
        if item.sku in self._items:
            raise StoreError(f"SKU {item.sku} already exists")
        self._items[item.sku] = item

    def remove(self, sku: str) -> Item:
        item = self.get(sku)
        del self._items[sku]
        return item

    def total_value(self) -> float:
        return round(sum(i.total_value for i in self._items.values()), 2)
