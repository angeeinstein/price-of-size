"""`pos-coordinator migrate`: bring the schema to this release's head.

Migrations are expand-only (docs/spec.md, CLAUDE.md): a release runs on the schema
of the release after it. So when an older coordinator is rolled back to, its image
finds a database revision it does not know; that is not an error but the newer,
compatible schema, which is kept as it is. Rollbacks reach one release back
(removals land a release after the code stopped using what they remove).
"""

import logging

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, pool

log = logging.getLogger(__name__)


def _known(script: ScriptDirectory, revision: str) -> bool:
    try:
        return script.get_revision(revision) is not None
    except CommandError:  # includes alembic's ResolutionError
        return False


class WrongEncoding(SystemExit):
    pass


def require_utf8(database_url: str) -> None:
    """Refuse a database that is not UTF8 (with a way out, not a stack trace).

    A database created on a system without a locale (a minimal container: LANG
    unset) gets SQL_ASCII, in which psycopg returns text as bytes; SQLAlchemy then
    fails on its very first query. Checked on a plain psycopg connection, before
    SQLAlchemy reads anything."""
    import psycopg
    from sqlalchemy.engine import make_url

    url = make_url(database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False), connect_timeout=15) as conn:
        encoding = conn.info.parameter_status("server_encoding")
        name = conn.info.dbname
    if (encoding or "").upper() not in ("UTF8", "UTF-8"):
        raise WrongEncoding(
            f"database {name!r} uses the encoding {encoding}, not UTF8. It was created on "
            "a system without a locale. If it holds no data yet, re-run install.sh "
            "--role database on the database host (it recreates an empty database as "
            "UTF8); otherwise see docs/operations.md, Troubleshooting."
        )


def pending(cfg: Config, database_url: str) -> bool:
    """Has this release migrations the database has not run yet? (A database ahead
    of this release has none: migrations are expand-only.)"""
    require_utf8(database_url)
    script = ScriptDirectory.from_config(cfg)
    engine = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
    finally:
        engine.dispose()
    if any(not _known(script, r) for r in current):
        return False
    return current != set(script.get_heads())


def upgrade(cfg: Config, database_url: str) -> str:
    """'upgraded', or 'newer' when the database is ahead of this release."""
    require_utf8(database_url)
    script = ScriptDirectory.from_config(cfg)
    engine = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_heads()
    finally:
        engine.dispose()
    unknown = [r for r in current if not _known(script, r)]
    if unknown:
        log.warning(
            "database schema %s is newer than this release (head %s): keeping it;"
            " migrations are expand-only, so this release runs on it",
            ",".join(unknown),
            script.get_current_head(),
        )
        return "newer"
    cfg.attributes.setdefault("database_url", database_url)
    command.upgrade(cfg, "head")
    return "upgraded"
