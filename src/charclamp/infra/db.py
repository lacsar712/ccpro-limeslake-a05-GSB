from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from charclamp.domain.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://charclamp:charclamp@127.0.0.1:6150/charclamp",
)
DATABASE_URL_SYNC = os.environ.get(
    "DATABASE_URL_SYNC",
    "postgresql+psycopg2://charclamp:charclamp@127.0.0.1:6150/charclamp",
)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

sync_engine = create_engine(DATABASE_URL_SYNC, echo=False)
SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False, class_=Session)


def sync_create_all() -> None:
    Base.metadata.create_all(sync_engine)


@asynccontextmanager
async def repeatable_read(session: AsyncSession) -> AsyncGenerator[AsyncSession, None]:
    """
    整段读取共用一个事务快照（PostgreSQL REPEATABLE READ）。

    时间轴一次渲染会先后发出多条 SELECT（窑行/班次行/各自的 selectin 关联），
    READ COMMITTED 下每条语句各取最新快照：若「插班次 + 窑态翻转」的提交恰好
    插在语句之间，就会渲染出「新卡已在但窑仍是 stacked」的分叉视图。
    同一快照保证窑剪影火色、时间轴卡窑态徽章、抽屉最新峰值三处口径一致。
    """
    async with session.begin():
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        yield session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
