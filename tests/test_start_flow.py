"""Test luồng `/start` đầu-tới-cuối, chạy trên database thật.

Không gọi Telegram: `sender` được thay bằng bản giả để bắt lại đúng những gì bot định
gửi. Phần chạm database là thật — đó mới là chỗ có thể sai.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from televip.services import users
from tests.conftest import make_user

# ── Đọc deep link ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("ref_12345", 12345),
        ("ref_1", 1),
        (None, None),
        ("", None),
        ("ref_", None),
        ("ref_abc", None),
        ("ref_0", None),  # id 0 không phải người thật
        ("ref_-5", None),  # dấu trừ không phải chữ số
        ("cref_575_6720704691", None),  # định dạng của bot CŨ, không nhận nữa
        ("linh tinh", None),
    ],
)
def test_doc_deep_link(payload, expected):
    assert users.parse_referral_payload(payload) == expected


# ── Tạo/cập nhật hồ sơ ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lan_dau_la_new_lan_sau_khong(seeded, session_factory):
    async with session_factory() as s, s.begin():
        first = await users.upsert_user(s, user_id=7001, username="a", full_name="A")
    assert first.is_new is True
    assert first.is_verified is False

    async with session_factory() as s, s.begin():
        second = await users.upsert_user(s, user_id=7001, username="a2", full_name="A2")
    assert second.is_new is False, "bấm /start lần hai không được tính là người mới"

    async with session_factory() as s:
        row = (
            await s.execute(text("SELECT username, full_name FROM users WHERE user_id = 7001"))
        ).one()
    assert row.username == "a2", "đổi username trên Telegram phải được cập nhật"


@pytest.mark.asyncio
async def test_upsert_song_song_chi_tao_mot_dong(seeded, session_factory):
    """Telegram gửi lại update trùng, và người dùng bấm /start nhiều lần rất nhanh."""
    import asyncio

    async def once():
        async with session_factory() as s, s.begin():
            return await users.upsert_user(s, user_id=7002, username="b", full_name="B")

    results = await asyncio.gather(*[once() for _ in range(20)], return_exceptions=True)
    ok = [r for r in results if isinstance(r, users.UpsertResult)]
    assert len(ok) == 20, f"có lỗi: {[r for r in results if isinstance(r, Exception)][:2]}"

    # Đúng MỘT lần được coi là người mới — con số này quyết định ai được tính referral.
    assert sum(1 for r in ok if r.is_new) == 1

    async with session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM users WHERE user_id = 7002"))).scalar_one()
    assert n == 1


# ── Ý định giới thiệu ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghi_nhan_nguoi_gioi_thieu(seeded, session_factory):
    db = seeded
    await make_user(db, 8001)  # người giới thiệu đã tồn tại

    async with session_factory() as s, s.begin():
        await users.upsert_user(s, user_id=8002, username="ref", full_name="Ref")
        created = await users.record_referral_intent(s, referee_id=8002, referrer_id=8001)
    assert created is True

    async with session_factory() as s:
        row = (
            await s.execute(
                text("SELECT referrer_id FROM referral_intents WHERE referee_id = 8002")
            )
        ).one()
    assert row.referrer_id == 8001


@pytest.mark.asyncio
async def test_khong_tu_moi_chinh_minh(seeded, session_factory):
    db = seeded
    await make_user(db, 8003)

    async with session_factory() as s, s.begin():
        created = await users.record_referral_intent(s, referee_id=8003, referrer_id=8003)
    assert created is False

    async with session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM referral_intents"))).scalar_one()
    assert n == 0


@pytest.mark.asyncio
async def test_bo_qua_nguoi_gioi_thieu_khong_ton_tai(seeded, session_factory):
    """Ai đó tự chế link `?start=ref_999999999` — không được tạo dòng rác."""
    db = seeded
    await make_user(db, 8004)

    async with session_factory() as s, s.begin():
        created = await users.record_referral_intent(s, referee_id=8004, referrer_id=999_999_999)
    assert created is False

    async with session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM referral_intents"))).scalar_one()
    assert n == 0


@pytest.mark.asyncio
async def test_moi_nguoi_chi_co_mot_nguoi_gioi_thieu(seeded, session_factory):
    """Người thứ hai bấm link không cướp được suất của người đầu tiên."""
    db = seeded
    await make_user(db, 8005)
    await make_user(db, 8006)
    await make_user(db, 8007)

    async with session_factory() as s, s.begin():
        assert await users.record_referral_intent(s, referee_id=8007, referrer_id=8005) is True
    async with session_factory() as s, s.begin():
        assert await users.record_referral_intent(s, referee_id=8007, referrer_id=8006) is False

    async with session_factory() as s:
        row = (
            await s.execute(
                text("SELECT referrer_id FROM referral_intents WHERE referee_id = 8007")
            )
        ).one()
    assert row.referrer_id == 8005, "người giới thiệu đầu tiên phải được giữ"
