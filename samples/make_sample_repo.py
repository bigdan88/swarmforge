"""Create a fresh git repository from samples/inventory-app for SwarmForge experiments.

    python samples/make_sample_repo.py ~/swarmforge-sample
    swarmforge run --issue-file samples/issues/export-csv-json.md --repo ~/swarmforge-sample

To try the GitHub flow, push the created repo to an empty GitHub repo and open an issue with the
contents of samples/issues/export-csv-json.md.
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dest = Path(sys.argv[1]).expanduser().resolve()
    if dest.exists():
        print(f"{dest} already exists; choose a new path")
        return 1
    shutil.copytree(HERE / "inventory-app", dest,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n*.json.bak\n")

    def g(*a):
        subprocess.run(["git", *a], cwd=dest, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("-c", "user.name=SwarmForge Sample", "-c", "user.email=sample@localhost",
      "commit", "-q", "-m", "Initial inventory app")
    print(f"created sample repository at {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
