"""`pos-coordinator` command: `serve` (default) or `migrate`."""

import argparse
import logging
from pathlib import Path

import uvicorn


def _alembic_config():
    from alembic.config import Config

    here = Path(__file__).resolve()
    for parent in here.parents:
        ini = parent / "alembic.ini"
        if ini.exists():
            return Config(str(ini))
    raise SystemExit("alembic.ini not found; run from the repository or image root")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pos-coordinator")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the API (default)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="upgrade the database schema to head")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "migrate":
        from alembic import command

        command.upgrade(_alembic_config(), "head")
        return
    from pos_coordinator.app import create_app

    uvicorn.run(
        create_app(),
        host=getattr(args, "host", "0.0.0.0"),
        port=getattr(args, "port", 8000),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
