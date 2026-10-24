"""Async SQLite engine and session factory."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            # WAL: readers don't block the writer; foreign keys for ON DELETE CASCADE.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    @classmethod
    def for_data_dir(cls, data_dir: str) -> Database:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return cls(f"sqlite+aiosqlite:///{path / 'dashboard.db'}")

    async def create_all(self) -> None:
        # The schema is small and additive so far; Alembic migrations come once it changes.
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


__all__ = ["AsyncSession", "Database"]
