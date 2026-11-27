"""Database engine and sessions (async SQLAlchemy over psycopg 3)."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, **kw) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, **kw)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sm() as session:
        yield session
