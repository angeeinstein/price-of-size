"""Test fixtures.

DB tests run against a real PostgreSQL (with pgvector) given by
POS_TEST_DATABASE_URL; they are skipped when it is unreachable. The schema is
built once per session by running the Alembic migrations, so migrations are
tested on every run. The test database is wiped: never point it at real data.
"""

import os
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from pos_coordinator.app import create_app
from pos_coordinator.config import Settings
from pos_shared.version import API_VERSION, API_VERSION_HEADER

ROOT = Path(__file__).resolve().parents[1]
DB_URL = os.environ.get(
    "POS_TEST_DATABASE_URL", "postgresql+psycopg://pos:pos@localhost:5432/pos_test"
)
ADMIN_TOKEN = "test-admin-token-" + "x" * 32


def _db_available() -> bool:
    try:
        eng = create_engine(DB_URL)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = DB_URL
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_db():
    if not DB_AVAILABLE:
        pytest.skip(f"PostgreSQL not reachable at {DB_URL}")
    eng = create_engine(DB_URL)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    command.upgrade(alembic_config(), "head")
    return DB_URL


@pytest.fixture
def db(migrated_db):
    """Empty all tables before each test."""
    eng = create_engine(migrated_db)
    with eng.begin() as c:
        tables = c.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version' AND tablename NOT LIKE 'raw\\_fetch\\_%'"
            )
        ).scalars()
        c.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE"))
    eng.dispose()
    return migrated_db


@pytest.fixture
def settings(db, tmp_path) -> Settings:
    return Settings(
        database_url=db,
        raw_store_path=tmp_path / "raw",
        signing_key_path=tmp_path / "signing_key.pem",
        admin_token=ADMIN_TOKEN,
    )


@pytest.fixture
async def app(settings):
    app = create_app(settings, run_maintenance=False)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def http(app):
    """Unauthenticated client speaking the current API version."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://coordinator",
        headers={API_VERSION_HEADER: API_VERSION},
    ) as c:
        yield c


@pytest.fixture
async def admin(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://coordinator",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as c:
        yield c


@pytest.fixture
def sessionmaker(app):
    return app.state.sessionmaker


@pytest.fixture(autouse=True)
def _reset_embedding_recheck():
    from pos_coordinator.classify import embeddings

    embeddings._recheck_after = None
    yield
    embeddings._recheck_after = None


@pytest.fixture(autouse=True)
def _reset_classifier_caches():
    from pos_coordinator.classify import common, knn, proposals

    common._nodes = knn._examples = None
    proposals._last_run = 0.0
    yield
    common._nodes = knn._examples = None


@pytest.fixture(autouse=True)
def _reset_fetch_breaker():
    from pos_worker.fetch import breaker

    breaker.reset()
    yield
    breaker.reset()
