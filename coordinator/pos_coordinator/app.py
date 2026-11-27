"""FastAPI application factory."""

import asyncio
import contextlib
import functools
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from pos_coordinator import maintenance, rawstore, signing_key
from pos_coordinator.api import admin, crawl, jobs, nodes, raw, releases
from pos_coordinator.api import parsers as parsers_api
from pos_coordinator.api import prices as prices_api
from pos_coordinator.api import taxonomy as taxonomy_api
from pos_coordinator.api.deps import Session
from pos_coordinator.classify import by_llm, proposals  # (registers batch handlers)
from pos_coordinator.config import Settings, get_settings
from pos_coordinator.db import make_engine, make_sessionmaker
from pos_coordinator.derive import fx, llm_tier, site_parsers  # (with batch handlers)
from pos_coordinator.derive import stage as derive_stage
from pos_coordinator.llm.service import LlmService, default_client
from pos_coordinator.panel import api as panel_api
from pos_coordinator.panel import static as panel_static
from pos_coordinator.partitions import PartitionCache
from pos_coordinator.rawstore import RawStore
from pos_coordinator.validity import review  # (registers its batch handler)
from pos_shared.version import API_VERSION, __version__


async def fx_daily(sm) -> None:
    """ECB reference rates, refreshed once a day (failures only delay conversion)."""
    log = logging.getLogger("pos_coordinator.fx")
    while True:
        try:
            async with sm() as session:
                n = await fx.refresh_from_ecb(session)
                await session.commit()
            converted = await derive_stage.backfill_fx(sm)
            log.info("ECB rates refreshed (%d rows); %d waiting offers converted", n, converted)
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network or ECB down: retry in an hour
            log.warning("ECB rate refresh failed: %s", e)
            await asyncio.sleep(3600)


async def llm_loop(service, interval_s: float, store: RawStore) -> None:
    """Collect finished Message Batches (the stages' handlers store the answers) and
    queue new LLM work: mapping proposals first (one mapping classifies many
    products), then classification of single products."""
    log = logging.getLogger("pos_coordinator.llm")
    while True:
        if service.available():
            for name, step in (
                ("polling LLM batches", service.poll_batches),
                ("queueing mapping proposals", lambda: proposals.schedule(service.sm, service)),
                ("queueing LLM classification", lambda: by_llm.schedule(service.sm, service)),
                ("queueing price reviews", lambda: review.schedule(service.sm, service, store)),
                ("applying LLM extractions", lambda: llm_tier.apply_pending(service.sm)),
                ("queueing LLM extraction", lambda: llm_tier.schedule(service.sm, service, store)),
                (
                    "queueing parser generation",
                    lambda: site_parsers.schedule(service.sm, service, store),
                ),  # fmt: skip
            ):
                try:
                    n = await step()
                    if any(n.values()) if isinstance(n, dict) else n:
                        log.info("%s: %s", name, n)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("%s failed", name)
        await asyncio.sleep(interval_s)


def create_app(settings: Settings | None = None, *, run_maintenance: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_s,
        )
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.llm = LlmService(
            app.state.sessionmaker, functools.partial(default_client, settings)
        )
        await rawstore.bind_to_database(app.state.sessionmaker, app.state.rawstore)
        tasks: list[asyncio.Task] = []
        if run_maintenance:
            tasks.append(
                asyncio.create_task(
                    maintenance.loop(
                        app.state.sessionmaker,
                        settings.reaper_interval_s,
                        settings.agent_dir,
                        app.state.signing_key,
                    )
                )
            )
            tasks.append(
                asyncio.create_task(
                    derive_stage.loop(
                        app.state.sessionmaker, app.state.rawstore, settings.derive_interval_s
                    )
                )
            )
            tasks.append(asyncio.create_task(fx_daily(app.state.sessionmaker)))
            tasks.append(
                asyncio.create_task(
                    llm_loop(app.state.llm, settings.llm_poll_interval_s, app.state.rawstore)
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await engine.dispose()

    app = FastAPI(title="The Price of Size: coordinator", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.rawstore = RawStore(settings.raw_store_path)
    llm_tier.configure(app.state.rawstore)
    app.state.partitions = PartitionCache()
    app.state.signing_key = signing_key.load_or_create(settings.signing_key_path)

    @app.get("/healthz")
    async def healthz(session: Session):
        await session.execute(text("SELECT 1"))
        # A missing raw store is reported, not failed: health gates would otherwise roll
        # back a healthy release because the NAS is down.
        return {
            "ok": True,
            "version": __version__,
            "api_version": API_VERSION,
            "raw_store": app.state.rawstore.available(),
        }

    @app.get("/api/v1/version")
    async def version():
        return {"version": __version__, "api_version": API_VERSION}

    routers = (nodes, jobs, raw, releases, admin, crawl, taxonomy_api, prices_api, parsers_api)
    for module in (*routers, panel_api):
        app.include_router(module.router)
    app.add_middleware(panel_static.SecurityHeaders)
    dist = panel_static.find_dist(settings.panel_dist)
    if dist is not None:  # last: the SPA catch-all must not shadow the API
        panel_static.mount(app, dist)
    return app
