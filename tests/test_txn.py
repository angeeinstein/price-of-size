"""Transient lock failures are retried; everything else is raised."""

import pytest
from sqlalchemy.exc import DBAPIError

from pos_coordinator import txn


class _Orig(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


def _err(code):
    return DBAPIError("stmt", {}, _Orig(code))


async def test_retries_deadlocks_and_refreshes(sessionmaker):
    calls = []
    refreshed = []

    async with sessionmaker() as s:

        async def refresh(obj):
            refreshed.append(obj)

        s.refresh = refresh  # the node object would be refreshed after a rollback

        async def work():
            calls.append(1)
            if len(calls) < 3:
                raise _err("40P01")
            return "ok"

        assert await txn.run(s, work, refresh=["node"]) == "ok"
    assert len(calls) == 3 and refreshed == ["node", "node"]


async def test_other_errors_are_not_retried(sessionmaker):
    calls = []
    async with sessionmaker() as s:

        async def work():
            calls.append(1)
            raise _err("23505")  # unique violation: a bug, not a race

        with pytest.raises(DBAPIError):
            await txn.run(s, work)
    assert calls == [1]


async def test_gives_up_after_the_last_attempt(sessionmaker):
    async with sessionmaker() as s:

        async def work():
            raise _err("40001")

        with pytest.raises(DBAPIError):
            await txn.run(s, work, attempts=2)
