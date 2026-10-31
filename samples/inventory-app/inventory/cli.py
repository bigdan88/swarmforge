"""Command-line interface: python -m inventory --db FILE <command> ..."""
from __future__ import annotations

import argparse
import sys

from .models import Item, ValidationError
from .store import InventoryStore, StoreError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inventory", description="Tiny inventory tracker")
    p.add_argument("--db", default="inventory.json", help="inventory file (default: inventory.json)")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add an item")
    a.add_argument("sku")
    a.add_argument("name")
    a.add_argument("quantity", type=int)
    a.add_argument("unit_price", type=float)

    r = sub.add_parser("remove", help="remove an item")
    r.add_argument("sku")

    sub.add_parser("list", help="list items")
    sub.add_parser("value", help="print total inventory value")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = InventoryStore(args.db)
        if args.command == "add":
            store.add(Item(args.sku, args.name, args.quantity, args.unit_price))
            store.save()
            print(f"Added {args.sku}")
        elif args.command == "remove":
            store.remove(args.sku)
            store.save()
            print(f"Removed {args.sku}")
        elif args.command == "list":
            for i in store.items():
                print(f"{i.sku:12} {i.name:30} {i.quantity:6d} {i.unit_price:10.2f}")
        elif args.command == "value":
            print(f"{store.total_value():.2f}")
    except (StoreError, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
