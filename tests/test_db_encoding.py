"""The coordinator refuses a database that is not UTF8, before SQLAlchemy reads it.

A database created on a system without a locale (a container with LANG unset)
gets SQL_ASCII; psycopg returns its text as bytes and SQLAlchemy failed on its
first query with "cannot use a string pattern on a bytes-like object"."""

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from pos_coordinator.schema import WrongEncoding, require_utf8
from tests.conftest import DB_AVAILABLE, DB_URL

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="needs PostgreSQL")


@pytest.fixture
def database():
    """A scratch database with the given encoding (needs CREATEDB)."""
    admin = create_engine(DB_URL, isolation_level="AUTOCOMMIT")
    made = []

    def make(encoding: str) -> str:
        name = f"pos_enc_{uuid.uuid4().hex[:8]}"
        try:
            with admin.connect() as c:
                c.execute(
                    text(
                        f"CREATE DATABASE {name} ENCODING '{encoding}' LC_COLLATE 'C' "
                        "LC_CTYPE 'C' TEMPLATE template0"
                    )  # fmt: skip
                )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"cannot create a database here: {e}")
        made.append(name)
        return make_url(DB_URL).set(database=name).render_as_string(hide_password=False)

    yield make
    with admin.connect() as c:
        for name in made:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


def test_a_sql_ascii_database_is_refused_with_a_way_out(database):
    url = database("SQL_ASCII")
    with pytest.raises(WrongEncoding) as e:
        require_utf8(url)
    assert "SQL_ASCII" in str(e.value) and "install.sh --role database" in str(e.value)


def test_a_utf8_database_passes(database):
    require_utf8(database("UTF8"))
