"""Cấu hình chung cho test.

Test chạy trên **PostgreSQL thật** (container dev), không dùng SQLite in-memory. Lý do:
toàn bộ giá trị của tầng cấp code nằm ở `FOR UPDATE SKIP LOCKED`, `ON CONFLICT` và
partial index — SQLite không có thứ nào trong số đó, nên test trên SQLite sẽ xanh trong
khi production vẫn phát trùng code.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("TELEVIP_ENV", "dev")

TEST_DATABASE_URL = os.environ.get(
    "TELEVIP_TEST_DATABASE_URL",
    "postgresql+asyncpg://televip:televip_dev_only@127.0.0.1:5433/televip",
)


@pytest_asyncio.fixture
async def engine():
    """Engine mới cho mỗi test.

    Cố ý KHÔNG dùng `scope="session"`: pytest-asyncio tạo event loop riêng cho từng test,
    còn connection pool thì gắn với loop đã tạo ra nó. Dùng chung engine giữa các test
    làm lần thứ hai chạm vào một pool thuộc loop đã đóng — trên Windows nó hiện ra thành
    `'NoneType' object has no attribute 'send'`, một thông báo không nói gì về nguyên nhân.
    """
    eng = create_async_engine(TEST_DATABASE_URL, pool_size=25, max_overflow=15)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncIterator[AsyncSession]:
    """Session sạch cho từng test, dọn dữ liệu trước khi chạy."""
    async with session_factory() as s:
        await _truncate_all(s)
        yield s


async def _truncate_all(s: AsyncSession) -> None:
    """Xoá sạch dữ liệu nghiệp vụ, giữ nguyên schema.

    `RESTART IDENTITY CASCADE` để mọi test bắt đầu từ cùng một trạng thái, kể cả các
    sequence — nếu không, id sinh ra khác nhau giữa các lần chạy và test khó đọc.
    """
    await s.execute(
        text("""
        TRUNCATE TABLE
            code_ledger, code_grants, codes, grant_types,
            idempotency_records, code_pool_stats,
            referral_rewards, referrals, referral_intents, campaigns,
            checkins, points_ledger,
            event_participations, events, share_event_config,
            group_memberships, membership_events, membership_mismatch, required_chats,
            broadcast_targets, broadcast_jobs, outbox_messages, media_assets,
            audit_log, admin_permissions, admin_users, settings_audit, settings,
            system_stats, user_stats,
            identity_clusters, cluster_stats, risk_assessments, fraud_cases,
            verification_events, signal_owners, identity_signals, asn_policy,
            user_bans, users
        RESTART IDENTITY CASCADE
        """)
    )
    await s.commit()


@pytest_asyncio.fixture
async def seeded(db: AsyncSession):
    """Dữ liệu tối thiểu: các loại grant."""
    await db.execute(
        text("""
        INSERT INTO grant_types (code, label_vi, once_per_life) VALUES
            ('tanthu', 'Code tan thu', true),
            ('referral_milestone', 'Moc moi ban be', false),
            ('event_box', 'Dap hop', false),
            ('points_redeem', 'Doi diem', false),
            ('share_event', 'Event chia se', true),
            ('admin_manual', 'Admin trao tay', false)
        ON CONFLICT DO NOTHING
        """)
    )
    await db.commit()
    return db


async def make_user(db: AsyncSession, user_id: int) -> int:
    await db.execute(
        text("INSERT INTO users (user_id, username) VALUES (:uid, :un) ON CONFLICT DO NOTHING"),
        {"uid": user_id, "un": f"user{user_id}"},
    )
    await db.commit()
    return user_id


async def add_codes(
    db: AsyncSession, *, code_type: str, value_vnd: int, count: int, prefix: str = "C"
) -> None:
    await db.execute(
        text("""
        INSERT INTO codes (code_value, code_type, value_vnd, status)
        SELECT :prefix || '-' || :ct || '-' || g, :ct, :val, 'available'
          FROM generate_series(1, :n) AS g
        """),
        {"prefix": prefix, "ct": code_type, "val": value_vnd, "n": count},
    )
    await db.commit()


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
