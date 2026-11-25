"""Tiny acceptance benchmark app; broken variants intentionally return success."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["healthy", "empty", "wrong", "missing"], required=True)
parser.add_argument("--input", default="sample.txt")
args = parser.parse_args()
if args.mode != "missing":
    words = Path(args.input).read_text(encoding="utf-8").strip()
    entries = [] if args.mode == "empty" else [{"text": "unrelated output" if args.mode == "wrong" else words}]
    Path("result.json").write_text(json.dumps({"status": "done", "entries": entries}), encoding="utf-8")
print("Completed successfully")
