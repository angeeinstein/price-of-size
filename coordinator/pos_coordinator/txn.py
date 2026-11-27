"""Retry transactions that lost a lock race.

Leasing and result handling lock several rows per transaction (jobs, domain,
egress group). A consistent lock order prevents most cycles, but with inserts
into unique indexes and several domains per transaction, PostgreSQL can still
occasionally pick a deadlock victim. Such a failure is transient: the whole
transaction is rolled back, so running it again is safe.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# deadlock_detected, serialization_failure, lock_not_available (lock_timeout)
TRANSIENT_SQLSTATES = {"40P01", "40001", "55P03"}
LOCK_TIMEOUT = "10s"  # never let a request queue behind a lock for long


def is_transient(e: DBAPIError) -> bool:
    return getattr(e.orig, "sqlstate", None) in TRANSIENT_SQLSTATES


async def run[T](
    session: AsyncSession,
    fn: Callable[[], Awaitable[T]],
    *,
    refresh: list[Any] | None = None,
    attempts: int = 4,
) -> T:
    """Run fn() in a transaction and commit; retry on transient lock failures.

    `refresh`: ORM objects loaded before (e.g. the authenticated node) that must be
    usable again after a rollback expired them.
    """
    for attempt in range(1, attempts + 1):
        try:
            await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            result = await fn()
            await session.commit()
            return result
        except DBAPIError as e:
            await session.rollback()
            if not is_transient(e) or attempt == attempts:
                raise
            log.warning(
                "transaction lost a lock race (%s), retrying (%d/%d)",
                getattr(e.orig, "sqlstate", "?"),
                attempt,
                attempts,
            )
            for obj in refresh or []:
                await session.refresh(obj)
            await asyncio.sleep(random.uniform(0.01, 0.05) * attempt)
    raise AssertionError("unreachable")
