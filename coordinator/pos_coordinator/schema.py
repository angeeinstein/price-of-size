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


def upgrade(cfg: Config, database_url: str) -> str:
    """'upgraded', or 'newer' when the database is ahead of this release."""
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
