"""
Database Connection
Generic async SQLAlchemy engine backed by DATABASE_URL (SQLite).
"""
from typing import AsyncGenerator
from pathlib import Path

from loguru import logger
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# ======================= Database =======================
engine = None
async_engine = None
database_dialect = ""


class _DisabledAsyncSessionFactory:
    """数据库未启用时的占位工厂，避免模块导入阶段直接崩溃。"""

    def __call__(self, *args, **kwargs):
        raise RuntimeError("Database session factory is disabled because DATABASE_URL is empty")


if settings.database_url:
    engine_kwargs = {"echo": False}
    if "sqlite" in settings.database_url:
        database_dialect = "sqlite"
        sqlite_path = make_url(settings.database_url).database
        if sqlite_path:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        database_dialect = "generic"
        engine_kwargs["pool_pre_ping"] = True

    engine = create_async_engine(settings.database_url, **engine_kwargs)
    async_engine = engine

    AsyncSessionLocal = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    logger.info(f"[Database] Async engine initialized: dialect={database_dialect}")
else:
    logger.warning("DATABASE_URL not configured, database session factory disabled")
    AsyncSessionLocal = _DisabledAsyncSessionFactory()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session."""
    if engine is None:
        raise RuntimeError("Database is disabled")
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
