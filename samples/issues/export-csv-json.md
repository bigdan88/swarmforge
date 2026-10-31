# Add CSV and JSON export functionality

Users need to get their inventory out of the tool for spreadsheets and other systems.
Add CSV and JSON export functionality, including command-line support, error handling,
unit tests, and documentation.

## Requirements

### Library API — new module `inventory/exporters.py`

- `class ExportError(Exception)` — raised for every export failure.
- `export_csv(items, path) -> int`
  - Writes a UTF-8 CSV file with the header row `sku,name,quantity,unit_price`
    followed by one row per item, **sorted by SKU**. Use the `csv` module so names containing
    commas or quotes are quoted correctly. Returns the number of item rows written.
- `export_json(items, path) -> int`
  - Writes a UTF-8 JSON file containing a list of objects with exactly the keys
    `sku`, `name`, `quantity`, `unit_price`, sorted by SKU, indented with 2 spaces.
    Returns the number of items written.
- `export_items(items, path, fmt) -> int`
  - Dispatches to the functions above for `fmt` in `{"csv", "json"}` (case-insensitive).
  - Any other format raises `ExportError`.
- Error handling: if the destination's parent directory does not exist, the destination is a
  directory, or the file cannot be written, raise `ExportError` with a human-readable message
  (never let a raw `OSError` escape). Exporting an empty inventory is valid (header only for
  CSV, `[]` for JSON).

### Command line

```
python -m inventory --db inventory.json export --output items.csv [--format csv|json]
```

- `--format` is optional; when omitted it is inferred from the output file extension
  (`.csv` or `.json`, case-insensitive). If it cannot be inferred, print
  `error: cannot infer export format ...` to stderr and exit with status 2.
- An explicit `--format` other than csv/json is rejected with exit status 2.
- On success print `Exported N items to PATH` and exit 0.
- On `ExportError` print `error: <message>` to stderr and exit with status 1.

### Tests and docs

- Unit tests for both formats, format dispatch/inference, the CLI command and the error cases.
- Document the export command in `README.md` (new "Exporting" section with examples).

## Acceptance criteria

1. CSV export writes the header and SKU-sorted rows, with correct quoting.
2. JSON export writes SKU-sorted objects with the four keys.
3. `export_items` dispatches by format and rejects unknown formats with `ExportError`.
4. File-system problems surface as `ExportError`, and the CLI turns them into exit status 1.
5. The CLI `export` command works with explicit and inferred formats, and exits 2 when the
   format is invalid or cannot be inferred.
6. Existing tests keep passing; new behavior has unit tests; README documents exporting.
