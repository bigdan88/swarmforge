"""Launch with python -m swarmforge.webapp --db PATH [--port 8600]."""
from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import sys


def _interface_dependencies(parser):
    modules = {}
    for name in ("fastapi", "pydantic", "uvicorn"):
        try:
            modules[name] = importlib.import_module(name)
        except ImportError as exc:
            parser.error(
                f"Cannot load interface dependency '{name}' ({exc}).\n"
                f"Python environment: {sys.executable}\n"
                'In this same activated environment, run: python -m pip install -e ".[interface]"'
            )
    if not hasattr(modules["pydantic"].BaseModel, "model_validate"):
        parser.error('The interface requires Pydantic 2. Run: python -m pip install -e ".[interface]"')
    return modules["uvicorn"]


def main():
    parser = argparse.ArgumentParser(description="SwarmForge React dashboard (local only; browsing by default)")
    parser.add_argument("--db", type=Path, help="Existing database; defaults to the configured database")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--runs-root", type=Path, help="Retained runs directory; defaults to the database directory's runs folder")
    parser.add_argument("--config", type=Path, help="Configuration for model/team views and controls")
    parser.add_argument("--allow-actions", action="store_true", help="Enable authenticated human controls and a durable request journal")
    parser.add_argument("--demo", action="store_true", help="Interactive disposable sample data with simulated models and worker; no API calls")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    uvicorn = _interface_dependencies(parser)
    from .frontend_build import ensure_current
    ensure_current()  # pulled new code? rebuild the page before serving it, or say why not
    # Dependency checks are separate so an internal application import failure
    # remains visible instead of being mislabeled as a missing optional extra.
    from .app import create_app
    if args.demo:
        if args.db is not None or args.config is not None or args.runs_root is not None or args.allow_actions:
            parser.error("Demo uses its own temporary workspace; combine it only with --port")
        from tempfile import TemporaryDirectory
        from .demo import create_demo
        with TemporaryDirectory(prefix="swarmforge-react-demo-") as directory:
            config, store, actions = create_demo(Path(directory))
            try:
                uvicorn.run(create_app(config.db_path, config=config, actions=actions, demo=True), host="127.0.0.1", port=args.port)
            finally:
                store.close()
        return
    config = None
    if args.db is None or args.config is not None:
        from swarmforge.config import load_config
        config = load_config(str(args.config) if args.config else None)
    if args.db is None:
        args.db = config.db_path
    if not args.db.is_file():
        parser.error("Database does not exist; this preview never creates one")
    if args.runs_root is not None and not args.runs_root.is_dir():
        parser.error("Runs directory does not exist; this preview never creates one")
    if args.allow_actions and config is not None and args.db.resolve() != config.db_path.resolve():
        parser.error("Controls require the database selected by this configuration")
    store = None
    try:
        actions = None
        if args.allow_actions:
            from swarmforge.db import Store
            from swarmforge.services.dashboard import DashboardActions
            store = Store(args.db)
            actions = DashboardActions(store, config)
        uvicorn.run(create_app(args.db, runs_root=args.runs_root, config=config, actions=actions), host="127.0.0.1", port=args.port)
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
