"""Alembic environment. Migrations follow expand/contract (see docs/operations.md)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from pos_coordinator.config import get_settings
from pos_coordinator.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Partitions of partitioned tables (raw_fetch_2026_11, raw_fetch_default, ...) are
# managed by pos_ensure_raw_fetch_partitions(), not by the models.
_PARTITIONED = ("raw_fetch_",)


def include_object(obj, name, type_, reflected, compare_to):
    table = obj if type_ == "table" else getattr(obj, "table", None)
    table_name = getattr(table, "name", name) or ""
    return not (reflected and compare_to is None and table_name.startswith(_PARTITIONED))


def _url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_object=include_object
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # transaction_per_migration=False: the whole upgrade is one transaction
        # (PostgreSQL has transactional DDL), so a failed migration leaves no trace.
        context.configure(
            connection=connection, target_metadata=target_metadata, include_object=include_object
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
