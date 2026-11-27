"""Periodic coordinator housekeeping: lease reaper and partition upkeep."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pos_coordinator import partitions, queue

log = logging.getLogger(__name__)


async def run_once(sm: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """One round. Each task has its own transaction, so one failing task (e.g.
    partition DDL) can never hold back another (e.g. lease reaping)."""
    out = {"requeued": 0, "failed": 0, "partitions_created": 0}
    try:
        async with sm() as session:
            out["requeued"], out["failed"] = await queue.reap_expired(session)
            await session.commit()
    except Exception:
        log.exception("lease reaping failed")
    try:
        async with sm() as session:
            out["partitions_created"] = await partitions.ensure_ahead(session)
            await session.commit()
    except Exception:
        log.exception("partition upkeep failed")
    if any(out.values()):
        log.info("maintenance: %s", out)
    return out


async def loop(sm: async_sessionmaker[AsyncSession], interval_s: float) -> None:
    while True:
        try:
            await run_once(sm)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive; the next round retries
            log.exception("maintenance round failed")
        await asyncio.sleep(interval_s)
