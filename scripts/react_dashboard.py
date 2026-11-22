"""Run the React dashboard from a source checkout without an editable install."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarmforge.webapp.__main__ import main


if __name__ == "__main__":
    main()
