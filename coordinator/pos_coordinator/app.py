"""FastAPI application factory."""

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from pos_coordinator import maintenance, signing_key
from pos_coordinator.api import admin, crawl, jobs, nodes, raw, releases
from pos_coordinator.api.deps import Session
from pos_coordinator.config import Settings, get_settings
from pos_coordinator.db import make_engine, make_sessionmaker
from pos_coordinator.partitions import PartitionCache
from pos_coordinator.rawstore import RawStore
from pos_shared.version import API_VERSION, __version__


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
        task = None
        if run_maintenance:
            task = asyncio.create_task(
                maintenance.loop(
                    app.state.sessionmaker,
                    settings.reaper_interval_s,
                    settings.agent_dir,
                    app.state.signing_key,
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await engine.dispose()

    app = FastAPI(title="The Price of Size: coordinator", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.rawstore = RawStore(settings.raw_store_path)
    app.state.partitions = PartitionCache()
    app.state.signing_key = signing_key.load_or_create(settings.signing_key_path)

    @app.get("/healthz")
    async def healthz(session: Session):
        await session.execute(text("SELECT 1"))
        return {"ok": True, "version": __version__, "api_version": API_VERSION}

    @app.get("/api/v1/version")
    async def version():
        return {"version": __version__, "api_version": API_VERSION}

    for module in (nodes, jobs, raw, releases, admin, crawl):
        app.include_router(module.router)
    return app
