"""Periodic coordinator housekeeping: lease reaper, partition upkeep, and the
file exchange with the coordinator's own node agent."""

import asyncio
import logging
from pathlib import Path

from sqlalchemy import text
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


PRUNE_BATCH = 5000


async def prune(session: AsyncSession) -> int:
    """Retention for operational rows. Raw evidence (raw_fetch, raw objects) is never
    pruned here; finished job rows are just bookkeeping once their results are stored.
    Deletes in small batches so a round never holds long locks."""
    cfg = await runtime_settings.get_all(session)
    total = 0
    for statuses, days in (
        (("done", "cancelled"), cfg["job_retention_days"]),
        (("failed",), cfg["failed_job_retention_days"]),
    ):
        res = await session.execute(
            text(
                "DELETE FROM job WHERE id IN (SELECT id FROM job WHERE status = ANY(:s) "
                "AND finished_at < now() - make_interval(days => :d) LIMIT :n)"
            ),
            {"s": list(statuses), "d": int(days), "n": PRUNE_BATCH},
        )
        total += res.rowcount
    for table, column in (
        ("node_token", "expires_at"),
        ("node_challenge", "expires_at"),
        ("enrollment_code", "expires_at"),
    ):
        res = await session.execute(
            text(
                f"DELETE FROM {table} WHERE {column} < now() - interval '1 day'"
                + (" AND used_at IS NULL" if table == "enrollment_code" else "")
            )
        )
        total += res.rowcount
    return total


async def run_once(
    sm: async_sessionmaker[AsyncSession],
    agent_dir: Path | None = None,
    key=None,
    offline_after_s: float = 90.0,
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
    try:
        async with sm() as session:
            from pos_coordinator import updates

            await updates.advance(session, agent_dir, offline_after_s)
            await session.commit()
    except Exception:
        log.exception("advancing the update rollout failed")
    if agent_dir is not None and key is not None:
        try:
            async with sm() as session:
                await sync_agent_exchange(session, agent_dir, key)
        except Exception:
            log.exception("agent exchange sync failed")
    try:
        async with sm() as session:
            out["pruned"] = await prune(session)
            await session.commit()
    except Exception:
        log.exception("pruning failed")
    try:
        async with sm() as session:
            from pos_coordinator.crawl import discovery

            out["robots_retried"] = await discovery.retry_pending_robots(session)
            await session.commit()
    except Exception:
        log.exception("robots retry for pending discovery failed")
    try:
        async with sm() as session:
            from pos_coordinator import runtime_settings
            from pos_coordinator.crawl import feedback

            settings = await runtime_settings.get_all(session)
            out["blocked_retried"] = await feedback.retry_blocked(session, settings)
            await session.commit()
    except Exception:
        log.exception("retrying blocked domains failed")
    try:
        async with sm() as session:
            from pos_coordinator.classify import embeddings

            out["embed_jobs"] = await embeddings.enqueue(session)
            await session.commit()
    except Exception:
        log.exception("queueing embed jobs failed")
    try:
        async with sm() as session:
            from pos_coordinator.classify import shopmap

            res = await shopmap.classify(session)
            out["classified"] = res["classified"] + res["withdrawn"]
            await session.commit()
    except Exception:
        log.exception("classification by shop mappings failed")
    try:
        async with sm() as session:
            from pos_coordinator.classify import knn

            res = await knn.classify(session)
            out["knn_classified"] = res["knn_classified"]
            await session.commit()
    except Exception:
        log.exception("k-NN classification failed")
    try:
        async with sm() as session:
            from pos_coordinator.validity import outliers

            res = await outliers.maybe_run(session)
            if res:
                out["category_outliers"] = res["flagged"] + res["cleared"]
            await session.commit()
    except Exception:
        log.exception("category outlier check failed")
    if any(out.values()):
        log.info("maintenance: %s", out)
    return out


async def loop(
    sm: async_sessionmaker[AsyncSession],
    interval_s: float,
    agent_dir: Path | None = None,
    key=None,
    offline_after_s: float = 90.0,
) -> None:
    while True:
        try:
            await run_once(sm, agent_dir, key, offline_after_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive; the next round retries
            log.exception("maintenance round failed")
        await asyncio.sleep(interval_s)
