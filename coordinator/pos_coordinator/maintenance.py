"""Periodic coordinator housekeeping: lease reaper, partition upkeep, and the
file exchange with the coordinator's own node agent."""

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pos_coordinator import partitions, queue, runtime_settings
from pos_coordinator.models import Release
from pos_shared import exchange, signing

log = logging.getLogger(__name__)


async def sync_agent_exchange(session: AsyncSession, agent_dir: Path, key) -> str | None:
    """Offer the signing key to this coordinator's agent and hand it the signed
    manifest of `coordinator.target_version`. Returns the version handed over."""
    pin = agent_dir / exchange.PINNED_KEY
    pub = signing.public_key_b64(key)
    if not pin.exists() or pin.read_text().strip() != pub:
        agent_dir.mkdir(parents=True, exist_ok=True)
        pin.write_text(pub + "\n")
    target = await runtime_settings.get(session, "coordinator.target_version")
    path = agent_dir / exchange.DESIRED
    if target is None:
        # The exchange mirrors the database: no target, no request (the agent stays put).
        path.unlink(missing_ok=True)
        return None
    release = await session.get(Release, target)
    if release is None:
        log.error("coordinator.target_version %s is not a known release", target)
        return None
    current = exchange.read_json(path)
    if not (isinstance(current, dict) and current.get("signature") == release.signature):
        signed = {"manifest": release.manifest, "signature": release.signature}
        exchange.write_json(path, {**signed, "key_id": release.key_id})
        log.info("handed release %s to the coordinator agent", target)
    return target


async def run_once(
    sm: async_sessionmaker[AsyncSession], agent_dir: Path | None = None, key=None
) -> dict[str, int]:
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
    if agent_dir is not None and key is not None:
        try:
            async with sm() as session:
                await sync_agent_exchange(session, agent_dir, key)
        except Exception:
            log.exception("agent exchange sync failed")
    if any(out.values()):
        log.info("maintenance: %s", out)
    return out


async def loop(
    sm: async_sessionmaker[AsyncSession], interval_s: float, agent_dir: Path | None = None, key=None
) -> None:
    while True:
        try:
            await run_once(sm, agent_dir, key)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive; the next round retries
            log.exception("maintenance round failed")
        await asyncio.sleep(interval_s)
