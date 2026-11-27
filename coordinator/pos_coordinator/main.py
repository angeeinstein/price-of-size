"""`pos-coordinator` command.

serve     run the API (default)
migrate   upgrade the database schema to head
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


async def _derived_cmd(cmd: str) -> int:
    import json as _json

    from pos_coordinator.config import get_settings
    from pos_coordinator.db import make_engine, make_sessionmaker
    from pos_coordinator.derive import fx, stage
    from pos_coordinator.rawstore import RawStore

    settings = get_settings()
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    try:
        if cmd == "fx-refresh":
            async with sm() as s:
                n = await fx.refresh_from_ecb(s)
                await s.commit()
            print(f"stored {n} rates")
            return 0
        result = await stage.rebuild(sm, RawStore(settings.raw_store_path))
        print(_json.dumps(result, indent=2, default=str))
        return 0 if result["identical"] else 2
    finally:
        await engine.dispose()


async def _create_user(args) -> int:
    import getpass

    from sqlalchemy import select

    from pos_coordinator.config import get_settings
    from pos_coordinator.db import make_engine, make_sessionmaker
    from pos_coordinator.models import AppUser, AuditLog
    from pos_coordinator.panel import auth

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat: ") != password:
            print("passwords differ", file=sys.stderr)
            return 1
    try:
        password_hash = auth.hash_password(password)
    except auth.AuthError as e:
        print(e.reason, file=sys.stderr)
        return 1
    name = auth.normalize_username(args.username)
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as s:
            if (await s.execute(select(AppUser).where(AppUser.username == name))).first():
                print(f"user {name} exists", file=sys.stderr)
                return 1
            s.add(AppUser(username=name, password_hash=password_hash, role=args.role))
            s.add(
                AuditLog(
                    actor="cli", action="user.create", target=name, details={"role": args.role}
                )
            )
            await s.commit()
    finally:
        await engine.dispose()
    print(f"created {args.role} {name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="pos-coordinator")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the API (default)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="upgrade the database schema to head")
    sub.add_parser("check", help="self-test: config, database, signing key")
    cu = sub.add_parser("create-user", help="create a control panel user (e.g. the first admin)")
    cu.add_argument("username")
    cu.add_argument("--role", choices=["admin", "viewer"], default="admin")
    cu.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    sub.add_parser(
        "rebuild-derived", help="rebuild drill: drop derived rows, derive again from raw, compare"
    )
    sub.add_parser("fx-refresh", help="load ECB euro reference rates now")
    bk = sub.add_parser("backup", help="pg_dump the database")
    bk.add_argument("--out", type=Path, default=None)
    bk.add_argument("--label", default="manual")
    bk.add_argument("--keep", type=int, default=int(os.environ.get("POS_BACKUP_KEEP", "20")))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "migrate":
        from alembic import command

        command.upgrade(_alembic_config(), "head")
        return
    if args.cmd == "check":
        sys.exit(asyncio.run(_check()))
    if args.cmd in ("rebuild-derived", "fx-refresh"):
        sys.exit(asyncio.run(_derived_cmd(args.cmd)))
    if args.cmd == "create-user":
        sys.exit(asyncio.run(_create_user(args)))
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
