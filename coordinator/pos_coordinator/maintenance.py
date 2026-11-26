"""Periodic coordinator housekeeping: lease reaper and partition upkeep."""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pos_coordinator import queue

log = logging.getLogger(__name__)

PARTITION_MONTHS_AHEAD = 3


async def ensure_partitions(session: AsyncSession) -> int:
    created = (
        await session.execute(
            text("SELECT pos_ensure_raw_fetch_partitions(date_trunc('month', now())::date, :n)"),
            {"n": PARTITION_MONTHS_AHEAD},
        )
    ).scalar_one()
    return int(created)


async def run_once(sm: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sm() as session:
        requeued, failed = await queue.reap_expired(session)
        created = await ensure_partitions(session)
        await session.commit()
    if requeued or failed or created:
        log.info(
            "maintenance",
            extra={"requeued": requeued, "failed": failed, "partitions_created": created},
        )
    return {"requeued": requeued, "failed": failed, "partitions_created": created}


async def loop(sm: async_sessionmaker[AsyncSession], interval_s: float) -> None:
    while True:
        try:
            await run_once(sm)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive; the next round retries
            log.exception("maintenance round failed")
        await asyncio.sleep(interval_s)
