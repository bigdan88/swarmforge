# inventory

A tiny command-line inventory tracker. Items are stored in a JSON file.

## Usage

```
python -m inventory --db inventory.json add ABC-1 "Widget" 10 2.50
python -m inventory --db inventory.json list
python -m inventory --db inventory.json value
python -m inventory --db inventory.json remove ABC-1
```

Each item has a SKU (2-32 characters of `A-Z`, `0-9`, `-`), a name, a non-negative integer
quantity and a unit price.

## Development

```
python -m pytest -q
```
