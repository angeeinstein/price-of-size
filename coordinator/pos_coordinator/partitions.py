"""Monthly partitions of `raw_fetch`.

Partitions are created ahead of time by maintenance, and on demand for the
months of incoming fetches (e.g. dataset imports with historic dates), each
in its own short transaction so result batches never carry DDL. The default
partition is only a safety net; the SQL function moves rows out of it when
their month's partition is created.
"""

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MONTHS_AHEAD = 3


def month_of(ts: datetime) -> date:
    from datetime import UTC

    ts = ts.astimezone(UTC) if ts.tzinfo else ts
    return date(ts.year, ts.month, 1)


async def ensure(session: AsyncSession, from_month: date, months: int) -> int:
    created = (
        await session.execute(
            text("SELECT pos_ensure_raw_fetch_partitions(:m, :n)"), {"m": from_month, "n": months}
        )
    ).scalar_one()
    return int(created)


async def ensure_ahead(session: AsyncSession) -> int:
    created = (
        await session.execute(
            text(
                "SELECT pos_ensure_raw_fetch_partitions("
                "date_trunc('month', now() AT TIME ZONE 'UTC')::date, :n)"
            ),
            {"n": MONTHS_AHEAD},
        )
    ).scalar_one()
    return int(created)


class PartitionCache:
    """Remembers months known to have a partition, so the check costs nothing."""

    def __init__(self) -> None:
        self.known: set[date] = set()

    async def ensure_months(self, session: AsyncSession, months: set[date]) -> None:
        """Create missing partitions in their own transaction on the caller's session.

        The caller's open transaction is committed first, so one request never
        needs two pooled connections at once.
        """
        missing = months - self.known
        if not missing:
            return
        await session.commit()
        for m in sorted(missing):
            await ensure(session, m, 1)
        await session.commit()
        self.known |= missing
