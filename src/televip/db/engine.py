"""Kết nối database: engine, session factory, và context manager giao dịch.

Hệ cũ mở rồi đóng một kết nối SQLite **cho mỗi hàm** — không pool, không WAL, không
`busy_timeout`. Ở đây một pool duy nhất cho mỗi tiến trình, và mọi thao tác ghi tiền
phải nằm trong `transaction()`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from televip.core.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    """Gọi đúng một lần lúc khởi động tiến trình."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    _engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=0,  # vượt pool thì chờ, không mở kết nối ngoài tầm kiểm soát
        pool_pre_ping=True,  # kết nối chết sau khi Postgres restart sẽ bị loại, không ném lỗi lên user
        pool_recycle=1800,
        echo=False,
        connect_args={
            # Tên xuất hiện trong pg_stat_activity — biết ngay tiến trình nào đang giữ khoá
            "server_settings": {"application_name": "televip"},
        },
    )
    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,  # đọc lại thuộc tính sau commit không phát sinh truy vấn ngầm
        autoflush=False,
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("init_engine() chưa được gọi")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("init_engine() chưa được gọi")
    return _session_factory


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Session chỉ đọc (hoặc ghi tự quản lý giao dịch riêng)."""
    factory = get_session_factory()
    async with factory() as s:
        yield s


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """Một giao dịch: hoặc thành công trọn vẹn, hoặc không có gì xảy ra.

    **Mọi luồng cấp code bắt buộc đi qua đây.** Lỗi chết người của hệ cũ là chọn code ở
    một kết nối rồi đánh dấu đã dùng ở kết nối khác — giữa hai bước đó, một tiến trình
    khác chọn trúng cùng mã.
    """
    factory = get_session_factory()
    async with factory() as s:
        async with s.begin():
            yield s


async def dispose_engine() -> None:
    """Đóng pool lúc tắt tiến trình."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
