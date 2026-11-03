"""Human-readable, packaged feature history; works offline and outside a git checkout."""
import json
from importlib import resources


def entries():
    return json.loads(resources.files("swarmforge").joinpath("resources/feature-history.json").read_text(encoding="utf-8"))


def filter_entries(items, query="", category="All"):
    query = query.strip().casefold()
    return [e for e in items if (category == "All" or e["category"] == category) and
            (not query or query in " ".join(str(e.get(k, "")) for k in ("title", "summary", "benefit", "where", "category")).casefold())]
