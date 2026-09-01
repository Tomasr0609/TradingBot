"""Database connection and session management."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from trading_bot.config.settings import get_settings


class Database:
    """Database connection manager."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or get_settings().database_url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._url,
                echo=False,
                pool_pre_ping=True,
                implicit_returning=False,
            )
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def close(self) -> None:
        """Close database connections."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


_db_instance: Database | None = None


def get_database() -> Database:
    """Get singleton database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


async def init_db() -> None:
    """Initialize database (create tables)."""
    from trading_bot.storage.models import Base  # noqa: F401

    db = get_database()
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connections."""
    global _db_instance
    if _db_instance:
        await _db_instance.close()
        _db_instance = None