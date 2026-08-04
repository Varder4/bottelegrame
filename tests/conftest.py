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

# Database RIÊNG cho test — tuyệt đối không dùng chung với database của bot dev.
# Mỗi test chạy `TRUNCATE` trên toàn bộ 40 bảng, nên trỏ nhầm sang database thật là
# xoá sạch người dùng thật chỉ bằng một lần gõ `pytest`.
TEST_DATABASE_URL = os.environ.get(
    "TELEVIP_TEST_DATABASE_URL",
    "postgresql+asyncpg://televip:televip_dev_only@127.0.0.1:5433/televip_test",
)

if not TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("_test"):
    raise RuntimeError(
        f"Database test phải có tên kết thúc bằng '_test', đang là: {TEST_DATABASE_URL!r}. "
        "Chặn ở đây vì bước tiếp theo là TRUNCATE toàn bộ bảng."
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
            message_templates_audit, message_templates,
            admin_sessions, audit_log, admin_permissions, admin_users,
            settings_audit, settings,
            system_stats, user_stats,
            identity_clusters, cluster_stats, risk_assessments, fraud_cases,
            verification_events, signal_owners, identity_signals, asn_policy,
            user_bans, users
        RESTART IDENTITY CASCADE
        """)
    )
    await s.commit()

    # Hai bộ đệm RAM này là bản sao của `settings` và `message_templates`, hai bảng vừa bị
    # xoá ở trên. Không dọn chúng thì một test gọi `set_content()` để lại bản ghi đè trong
    # RAM, và test CHẠY SAU nhận câu chữ của test trước — một kiểu hỏng phụ thuộc thứ tự
    # chạy, hiện ra rồi biến mất tuỳ theo bộ nào được chọn.
    from televip.services import settings_service, text_service

    settings_service.invalidate()
    text_service.invalidate()


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


#: Redis database 15 — tách khỏi database 0 mà bot dev đang dùng, vì fixture dưới đây
#: gọi `flushdb()`.
TEST_REDIS_URL = os.environ.get("TELEVIP_TEST_REDIS_URL", "redis://127.0.0.1:6380/15")


@pytest_asyncio.fixture
async def redis_clean():
    """Redis sạch cho test nào cần trạng thái thật (chống phát lại, cooldown, rate limit).

    Client mới cho từng test vì connection pool gắn với event loop đã tạo ra nó, mà
    pytest-asyncio thì tạo loop riêng cho mỗi test.
    """
    from types import SimpleNamespace

    from televip.cache.client import close_redis, init_redis

    client = init_redis(SimpleNamespace(redis_url=TEST_REDIS_URL))  # type: ignore[arg-type]
    await client.flushdb()
    yield client
    await close_redis()
