"""Packaged SwarmForge artwork and small, theme-neutral dashboard components."""
from __future__ import annotations

from functools import lru_cache
from html import escape
from importlib import resources


@lru_cache(maxsize=8)
def asset_bytes(name: str) -> bytes:
    return resources.files("swarmforge").joinpath(f"resources/brand/{name}").read_bytes()


@lru_cache(maxsize=1)
def styles() -> str:
    css = resources.files("swarmforge").joinpath("resources/brand/dashboard.css").read_text(encoding="utf-8")
    return f"<style>{css}</style>"


def work_summary(items: list[dict]) -> str:
    """Count visible tasks using the same attempt folding as the default Work list."""
    groups = [("active", "In progress", "Working on your next release"),
              ("waiting", "Up next", "Ready for the team"),
              ("done", "Completed", "Delivered work"),
              ("failed", "Needs attention", "Review and move forward")]
    cards = []
    for group, title, caption in groups:
        count = sum(item["group"] == group for item in items if not item.get("hidden") and not item.get("archived"))
        cards.append(f'<div class="sf-stat sf-stat-{group}"><span class="sf-stat-label">{escape(title)}</span>'
                     f'<strong>{count}</strong><span class="sf-stat-caption">{escape(caption)}</span></div>')
    return '<div class="sf-stats" aria-label="Task overview">' + "".join(cards) + '</div>'
