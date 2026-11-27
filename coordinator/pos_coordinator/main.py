"""`pos-coordinator` command.

serve     run the API (default)
migrate   upgrade the database schema to head (a newer schema is kept: rollbacks)
check     self-test for the node agent: config, database, signing key
backup    pg_dump the database into POS_BACKUP_DIR
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn


def _alembic_config():
    from alembic.config import Config

    candidates = []
    if env := os.environ.get("POS_ALEMBIC_INI"):
        candidates.append(Path(env))
    candidates.append(Path.cwd() / "alembic.ini")
    candidates += [p / "alembic.ini" for p in Path(__file__).resolve().parents]
    for ini in candidates:
        if ini.exists():
            return Config(str(ini))
    raise SystemExit("alembic.ini not found; set POS_ALEMBIC_INI")


async def _check() -> int:
    from sqlalchemy import text

    from pos_coordinator import signing_key
    from pos_coordinator.config import get_settings
    from pos_coordinator.db import make_engine

    settings = get_settings()
    ok = True
    try:
        signing_key.load_or_create(settings.signing_key_path)
        print("ok   signing key")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL signing key: {e}")
        ok = False
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
            ext = (
                await c.execute(text("SELECT 1 FROM pg_available_extensions WHERE name='vector'"))
            ).first()
        print("ok   database")
        if ext is None:
            print("FAIL pgvector extension is not installed on the database server")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"FAIL database: {type(e).__name__}: {e}")
        ok = False
    finally:
        await engine.dispose()
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="pos-coordinator")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the API (default)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="upgrade the database schema to head")
    sub.add_parser("check", help="self-test: config, database, signing key")
    bk = sub.add_parser("backup", help="pg_dump the database")
    bk.add_argument("--out", type=Path, default=None)
    bk.add_argument("--label", default="manual")
    bk.add_argument("--keep", type=int, default=int(os.environ.get("POS_BACKUP_KEEP", "20")))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "migrate":
        from pos_coordinator import schema
        from pos_coordinator.config import get_settings

        schema.upgrade(_alembic_config(), get_settings().database_url)
        return
    if args.cmd == "check":
        sys.exit(asyncio.run(_check()))
    if args.cmd == "backup":
        from pos_coordinator.backup import run_backup
        from pos_coordinator.config import get_settings
        from pos_shared.version import __version__

        settings = get_settings()
        out = args.out or settings.backup_dir
        if out is None:
            raise SystemExit("no backup directory: set POS_BACKUP_DIR or pass --out")
        label = args.label if args.label != "manual" else f"before-{__version__}"
        run_backup(settings.database_url, out, label, args.keep)
        return
    from pos_coordinator.app import create_app

    uvicorn.run(
        create_app(),
        host=getattr(args, "host", "0.0.0.0"),
        port=getattr(args, "port", 8000),
        proxy_headers=False,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
