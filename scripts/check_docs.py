"""Check local Markdown links and the README roster against its committed team profile."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check() -> list[str]:
    errors = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            target = target.strip("<>").split("#")[0]
            if not target or ":" in target or target.startswith("//"):
                continue
            if not (path.parent / target).exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link {target}")
    profile = yaml.safe_load((ROOT / "profiles/team-current.yaml").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    roster = readme.split("| Role |", 1)[1].split("This is a dated snapshot", 1)[0]
    labels = {"planner": "Planner / Eng. manager", "developer_backend": "Backend developer",
              "developer_frontend": "Frontend developer", "reviewer": "Reviewer",
              "tester": "Test engineer", "librarian": "Librarian", "designer": "Designer"}
    for name, role in profile["roles"].items():
        row = next((line for line in roster.splitlines() if line.startswith(f"| {labels[name]} |")), "")
        if f"`{role['model']}`" not in row:
            errors.append(f"README team roster missing {name}: {role['model']}")
    return errors


if __name__ == "__main__":
    errors = check()
    print("\n".join(errors) if errors else "Documentation links and team roster passed")
    raise SystemExit(bool(errors))
