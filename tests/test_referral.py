"""Luồng mời bạn bè đầu-tới-cuối: chốt referral, phát bù mốc, ba màn hình.

Chạy **qua chính handler và chính service**, trên database thật và Redis thật. Chỉ
Telegram bị thay bằng bản giả.

Mệnh đề được kiểm — tất cả đều là chỗ hệ cũ mất tiền hoặc mất lòng người dùng:

- mời 5 người đã xác minh → **đúng 1** mốc, đúng 1 mã rời kho;
- mời 12 người → **đúng 2** mốc, không phải 1 (`>=` thay cho `% interval == 0`, §13.5 A);
- gọi lại móc nối → **không** nhân đôi mốc nào (PK `(user_id, tier_no)`);
- người được mời chưa xác minh → **không** tính, và ý định giới thiệu **giữ nguyên**;
- tự mời chính mình → bị chặn ở cả hai tầng;
- vượt trần `referral.max_claims` → dừng đúng ở trần;
- chiến dịch hết hạn → **ngừng phát thật**, màn hình hiện "đã kết thúc" và ẩn nút
  (§13.5 dòng F — thay cho `test_moc_moi_ban_van_phat_sau_khi_het_han` đã hết phạm vi);
- hết kho `moibanbe` → **không** đánh dấu mốc đã phát, để lần sau phát bù.

⚠️ Cùng lý do với `test_tanthu.py`: file này KHÔNG dùng fixture `db`/`seeded` của
`conftest`. Handler đọc ghi qua engine **toàn cục**, nên chỉ được có MỘT engine trong
mỗi test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers import referral as referral_handler
from televip.db.engine import session as db_session
from televip.services import referral, users
from tests.conftest import TEST_DATABASE_URL, _truncate_all, add_codes, make_user

VERIFY_URL = "https://example.test/verify"
BOT_USERNAME = "televip_bot"

_GRANT_TYPES_SQL = """
INSERT INTO grant_types (code, label_vi, once_per_life) VALUES
    ('tanthu', 'Code tan thu', true),
    ('referral_milestone', 'Moc moi ban be', false),
    ('event_box', 'Dap hop', false),
    ('points_redeem', 'Doi diem', false),
    ('share_event', 'Event chia se', true),
    ('admin_manual', 'Admin trao tay', false)
ON CONFLICT DO NOTHING
"""


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    """Bắt lại đúng những gì bot định gửi, không gọi Telegram."""

    def __init__(self, *, blocked: bool = False) -> None:
        self.messages: list[str] = []
        self.markups: list[Any] = []
        self.answers: list[str] = []
        self.blocked = blocked

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        if self.blocked:
            return None  # người nhận đã chặn bot
        return 1000 + len(self.messages)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]


class FakeBot:
    def __init__(self) -> None:
        self.username = BOT_USERNAME


def make_update(user_id: int, *, callback: bool = False) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=None, chat=chat)
    query = SimpleNamespace(data="join_referral", message=message) if callback else None
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=query,
    )


def make_context(sender: FakeSender) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=FakeBot(),
        args=[],
    )


# ── Fixture: một engine duy nhất, trỏ vào database test ─────────────


@pytest_asyncio.fixture
async def wired(redis_clean):
    from televip.db import engine as db_engine
    from televip.services import settings_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    settings_service.invalidate()

    async with db_session() as s:
        await _truncate_all(s)
        await s.execute(text(_GRANT_TYPES_SQL))
        await s.commit()

    try:
        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()


# ── Helper (đều đi qua engine toàn cục) ─────────────────────────────


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def set_setting(key: str, value: Any, value_type: str = "string") -> None:
    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi)
             VALUES (:k, CAST(:v AS jsonb), :t, :k)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {"k": key, "v": json.dumps(value), "t": value_type},
    )
    from televip.services import settings_service

    settings_service.invalidate()


async def verify_user(user_id: int) -> None:
    await run_sql("UPDATE users SET verified_at = now() WHERE user_id = :uid", {"uid": user_id})


async def open_campaign(*, days_left: int = 7) -> None:
    """Chiến dịch đang chạy. Không có dòng này thì hệ thống KHÔNG phát mốc nào."""
    await run_sql(
        """
        INSERT INTO campaigns
               (code, name, interval_people, reward_value_vnd, max_claims,
                starts_at, ends_at, is_active)
        VALUES ('moibanbe', 'Moi ban be', 5, 10000, 10,
                now() - interval '1 day', now() + make_interval(days => :d), true)
        ON CONFLICT (code) DO UPDATE
                SET ends_at = EXCLUDED.ends_at, is_active = true
        """,
        {"d": days_left},
    )


async def expired_campaign() -> None:
    await run_sql(
        """
        INSERT INTO campaigns
               (code, name, interval_people, reward_value_vnd, max_claims,
                starts_at, ends_at, is_active)
        VALUES ('moibanbe', 'Moi ban be', 5, 10000, 10,
                now() - interval '30 days', now() - interval '1 day', true)
        ON CONFLICT (code) DO UPDATE SET ends_at = EXCLUDED.ends_at
        """
    )


async def setup(referrer_id: int, *, codes: int = 20, campaign: bool = True) -> None:
    async with db_session() as s:
        await make_user(s, referrer_id)
        if codes:
            await add_codes(s, code_type="moibanbe", value_vnd=10_000, count=codes, prefix="REF")
    await verify_user(referrer_id)
    await set_setting("webapp.url", VERIFY_URL)
    # Cooldown về 0: nhiều test bấm cùng một nút vài lần liên tiếp. Nhánh chặn có test
    # riêng bên dưới (`test_bam_qua_nhanh_bi_chan`).
    await set_setting("cooldown.moi_ban", 0, "int")
    await set_setting("cooldown.check_share", 0, "int")
    if campaign:
        await open_campaign()


async def bring_referee(
    sender: FakeSender,
    referrer_id: int,
    referee_id: int,
    *,
    verified: bool = True,
) -> list[int]:
    """Một người bấm link mời rồi (có thể) xác minh. Trả về các mốc vừa phát."""
    async with db_session() as s:
        await make_user(s, referee_id)
        await users.record_referral_intent(s, referee_id=referee_id, referrer_id=referrer_id)
        await s.commit()
    if verified:
        await verify_user(referee_id)
    async with db_session() as s:
        return await referral_handler.on_referee_verified(sender, s, referee_id)


async def bring_many(sender: FakeSender, referrer_id: int, count: int, *, base: int) -> list[int]:
    awarded: list[int] = []
    for i in range(count):
        awarded += await bring_referee(sender, referrer_id, base + i)
    return awarded


async def count_rewards(user_id: int) -> int:
    return await scalar(
        "SELECT count(*) FROM referral_rewards WHERE user_id = :uid", {"uid": user_id}
    )


async def count_grants(user_id: int) -> int:
    return await scalar(
        "SELECT count(*) FROM code_grants WHERE user_id = :uid AND grant_type = 'referral_milestone'",
        {"uid": user_id},
    )


async def used_codes() -> int:
    return await scalar("SELECT count(*) FROM codes WHERE status <> 'available'")


# ── 1. Luật mốc ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_moi_5_nguoi_nhan_dung_1_moc(wired):
    ref = 7001
    await setup(ref)
    sender = FakeSender()

    awarded = await bring_many(sender, ref, 5, base=7100)

    assert awarded == [1]
    assert await count_rewards(ref) == 1
    assert await count_grants(ref) == 1
    assert await used_codes() == 1
    assert "CHÚC MỪNG! LẦN 1/10" in sender.last

    state = await scalar("SELECT g.state FROM code_grants g WHERE g.user_id = :uid", {"uid": ref})
    assert state == "delivered", "mark_delivered() phải chạy SAU khi gửi thành công"


@pytest.mark.asyncio
async def test_moi_4_nguoi_chua_du_moc(wired):
    ref = 7002
    await setup(ref)
    sender = FakeSender()

    assert await bring_many(sender, ref, 4, base=7200) == []
    assert await count_rewards(ref) == 0
    assert await used_codes() == 0


@pytest.mark.asyncio
async def test_moi_12_nguoi_nhan_dung_2_moc(wired):
    """§13.5 dòng A: `% 5 == 0` làm mất mốc; `>=` thì không.

    Người thứ 5 và người thứ 10 phát mốc ngay; tới người thứ 12 tổng vẫn phải là 2 mốc.
    """
    ref = 7003
    await setup(ref)
    sender = FakeSender()

    awarded = await bring_many(sender, ref, 12, base=7300)

    assert awarded == [1, 2], "12 // 5 = 2 mốc, không phải 1"
    assert await count_rewards(ref) == 2
    assert await count_grants(ref) == 2
    assert await used_codes() == 2


@pytest.mark.asyncio
async def test_bo_lo_thoi_diem_cham_moc_van_phat_bu(wired):
    """Chiến dịch bật MUỘN: 12 người đã đạt chuẩn từ trước, mốc phải được phát bù cả hai.

    Đây chính là kịch bản modulo của hệ cũ làm hỏng — số đếm nhảy qua mốc thì mốc mất
    vĩnh viễn. `pending_tiers()` phải trả về [1, 2] chứ không phải rỗng.
    """
    ref = 7004
    await setup(ref, campaign=False)
    sender = FakeSender()

    assert await bring_many(sender, ref, 12, base=7400) == [], "chưa có chiến dịch thì chưa phát"
    assert await count_rewards(ref) == 0

    await open_campaign()
    async with db_session() as s:
        assert await referral.pending_tiers(s, ref) == [1, 2]

    awarded = await bring_referee(sender, ref, 7499)  # người thứ 13 kích hoạt lần phát bù
    assert awarded == [1, 2]
    assert await count_rewards(ref) == 2


@pytest.mark.asyncio
async def test_goi_lai_khong_nhan_doi(wired):
    ref = 7005
    await setup(ref)
    sender = FakeSender()
    await bring_many(sender, ref, 5, base=7500)

    # Gọi lại móc nối cho đúng người được mời đó — ý định đã tiêu, referral đã có.
    async with db_session() as s:
        assert await referral_handler.on_referee_verified(sender, s, 7500) == []

    assert await count_rewards(ref) == 1, "gọi lại KHÔNG được sinh mốc thứ hai"
    assert await count_grants(ref) == 1
    assert await used_codes() == 1


@pytest.mark.asyncio
async def test_moc_da_phat_khong_phat_lai(wired):
    """Hàng rào DB: `(user_id, tier_no)` là PK, phát lại đúng mốc là `AlreadyClaimed`."""
    from televip.core.errors import AlreadyClaimed

    ref = 7006
    await setup(ref)
    sender = FakeSender()
    await bring_many(sender, ref, 5, base=7600)

    async with db_session() as s:
        async with s.begin():
            with pytest.raises(AlreadyClaimed):
                await referral.grant_tier(s, user_id=ref, tier_no=1)

    assert await count_rewards(ref) == 1
    assert await used_codes() == 1


# ── 2. Điều kiện tính referral ──────────────────────────────────────


@pytest.mark.asyncio
async def test_nguoi_chua_xac_minh_khong_duoc_tinh(wired):
    """Tính lúc bấm link nghĩa là phát tán link cho tài khoản rác là ăn thưởng."""
    ref = 7007
    await setup(ref)
    sender = FakeSender()

    for i in range(5):
        assert await bring_referee(sender, ref, 7700 + i, verified=False) == []

    assert await count_rewards(ref) == 0
    assert await scalar("SELECT count(*) FROM referrals") == 0
    assert await scalar("SELECT count(*) FROM referral_intents") == 5, (
        "chưa xác minh thì ý định phải NẰM LẠI, không bị tiêu mất"
    )

    # Xác minh muộn: đúng lúc đó referral mới được tính.
    for i in range(5):
        await verify_user(7700 + i)
    awarded: list[int] = []
    for i in range(5):
        async with db_session() as s:
            awarded += await referral_handler.on_referee_verified(sender, s, 7700 + i)
    assert awarded == [1]


@pytest.mark.asyncio
async def test_tu_moi_chinh_minh_bi_chan(wired):
    ref = 7008
    await setup(ref)

    # Tầng 1: `record_referral_intent` từ chối, không ghi dòng nào.
    async with db_session() as s:
        assert await users.record_referral_intent(s, referee_id=ref, referrer_id=ref) is False
        await s.commit()
    assert await scalar("SELECT count(*) FROM referral_intents") == 0

    # Tầng 2: kể cả khi một dòng rác lọt vào bảng, `qualify()` vẫn không ghi referral.
    await run_sql(
        "INSERT INTO referral_intents (referee_id, referrer_id) VALUES (:u, :u)", {"u": ref}
    )
    async with db_session() as s:
        async with s.begin():
            assert await referral.qualify(s, referee_id=ref) is None
    assert await scalar("SELECT count(*) FROM referrals") == 0


@pytest.mark.asyncio
async def test_rui_ro_cao_thi_ghi_nhung_khong_tinh(wired):
    ref = 7009
    await setup(ref)
    sender = FakeSender()
    await set_setting("referral.max_risk_score", 70, "int")

    for i in range(5):
        async with db_session() as s:
            await make_user(s, 7900 + i)
        await run_sql("UPDATE users SET risk_score = 99 WHERE user_id = :uid", {"uid": 7900 + i})
        assert await bring_referee(sender, ref, 7900 + i) == []

    assert await scalar("SELECT count(*) FROM referrals") == 5
    assert await scalar("SELECT count(*) FROM referrals WHERE qualified_at IS NOT NULL") == 0
    assert await count_rewards(ref) == 0


@pytest.mark.asyncio
async def test_moi_hai_lan_cung_mot_nguoi_chi_tinh_mot(wired):
    """`referrals.referee_id` là PK — một người có đúng MỘT người giới thiệu, vĩnh viễn."""
    ref_a, ref_b = 7010, 7011
    await setup(ref_a)
    await setup(ref_b, codes=0)
    sender = FakeSender()

    await bring_referee(sender, ref_a, 7050)
    await bring_referee(sender, ref_b, 7050)  # người thứ hai cố nhận lại cùng một người

    async with db_session() as s:
        assert await referral.count_qualified(s, ref_a) == 1
        assert await referral.count_qualified(s, ref_b) == 0


# ── 3. Trần và chiến dịch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_vuot_tran_max_claims_thi_dung(wired):
    ref = 7012
    await setup(ref)
    await set_setting("referral.max_claims", 2, "int")
    await set_setting("referral.interval", 5, "int")
    sender = FakeSender()

    awarded = await bring_many(sender, ref, 15, base=7120)

    assert awarded == [1, 2], "15 // 5 = 3 nhưng trần là 2"
    assert await count_rewards(ref) == 2
    assert await used_codes() == 2

    async with db_session() as s:
        assert await referral.pending_tiers(s, ref) == []


@pytest.mark.asyncio
async def test_het_han_thi_ngung_phat_va_hien_da_ket_thuc(wired):
    """§13.5 dòng F. Thay cho `test_moc_moi_ban_van_phat_sau_khi_het_han` đã hết phạm vi."""
    ref = 7013
    await setup(ref, campaign=False)
    await expired_campaign()
    sender = FakeSender()

    assert await bring_many(sender, ref, 5, base=7130) == []
    assert await count_rewards(ref) == 0, "hết hạn là ngừng phát THẬT, không chỉ ngừng hiển thị"
    assert await used_codes() == 0
    # Quan hệ referral vẫn được ghi — người mời không mất công đã bỏ ra.
    async with db_session() as s:
        assert await referral.count_qualified(s, ref) == 5

    screen = FakeSender()
    await referral_handler.handle_check_share(make_update(ref), make_context(screen))
    assert "⏰ Chiến dịch đã kết thúc" in screen.last
    assert screen.markups[-1] is None, "hết hạn thì ẩn nút 💎 Tham gia ngay"


@pytest.mark.asyncio
async def test_khong_co_chien_dich_thi_khong_phat(wired):
    ref = 7014
    await setup(ref, campaign=False)
    sender = FakeSender()

    assert await bring_many(sender, ref, 5, base=7140) == []
    assert await count_rewards(ref) == 0
    assert await used_codes() == 0


# ── 4. Hết kho ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_het_kho_thi_khong_danh_dau_moc(wired):
    ref = 7015
    await setup(ref, codes=0)
    await set_setting("link.support", "https://t.me/cskh")
    sender = FakeSender()

    assert await bring_many(sender, ref, 5, base=7150) == []
    assert await count_rewards(ref) == 0, "mốc KHÔNG được đánh dấu đã phát — lần sau phát bù"
    assert await count_grants(ref) == 0
    assert "Code tạm hết" in sender.last
    assert "https://t.me/cskh" in sender.last

    # Admin nạp mã: lần chạy sau phát bù đúng mốc đã bỏ lỡ.
    async with db_session() as s:
        await add_codes(s, code_type="moibanbe", value_vnd=10_000, count=3, prefix="LATE")
    assert await bring_referee(sender, ref, 7159) == [1]
    assert await count_rewards(ref) == 1


@pytest.mark.asyncio
async def test_nguoi_moi_chan_bot_thi_khong_dot_ma(wired):
    """Gửi thất bại ⇒ grant nằm lại `reserved`, KHÔNG `delivered`. Job dọn trả mã về kho."""
    ref = 7016
    await setup(ref)
    sender = FakeSender(blocked=True)

    assert await bring_many(sender, ref, 5, base=7160) == []
    assert await count_rewards(ref) == 1, "mốc đã chốt — code đã thuộc về họ"
    state = await scalar("SELECT state FROM code_grants WHERE user_id = :uid", {"uid": ref})
    assert state == "reserved", "chưa gửi được thì tuyệt đối không được đánh dấu đã giao"


# ── 5. Màn hình ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_man_hinh_moi_ban_co_link_rieng(wired):
    ref = 7017
    await setup(ref)
    sender = FakeSender()

    await referral_handler.handle_moi_ban(make_update(ref), make_context(sender))

    assert "🎁 CHIẾN DỊCH CHIA SẺ ĐANG CHẠY!" in sender.last
    assert f"https://t.me/{BOT_USERNAME}?start=ref_{ref}" in sender.last
    assert "Mời 5 người = 1 code 10K" in sender.last
    assert "Đã nhận 0/10 lần" in sender.last
    assert sender.markups[-1] is not None, "chiến dịch đang chạy thì phải có nút tham gia"


@pytest.mark.asyncio
async def test_check_share_ba_trang_thai(wired):
    ref = 7018
    await setup(ref)
    sender = FakeSender()

    # Trạng thái 2 — chưa mời ai: còn đủ `interval` người, không phải màn hình rỗng.
    screen = FakeSender()
    await referral_handler.handle_check_share(make_update(ref), make_context(screen))
    assert "Còn 5 người nữa để nhận lần thứ 1" in screen.last
    assert "🎯 Mục tiêu kế tiếp: 5 người" in screen.last
    assert "Đã mời thành công: 0 người" in screen.last

    # Sau 7 người và 1 mốc: vẫn trạng thái 2, nhưng đếm theo mốc kế tiếp.
    await bring_many(sender, ref, 7, base=7180)
    screen = FakeSender()
    await referral_handler.handle_check_share(make_update(ref), make_context(screen))
    assert "Đã mời thành công: 7 người" in screen.last
    assert "Đã nhận: 1/10 lần" in screen.last
    assert "Tổng giá trị đã nhận: 10.000đ" in screen.last
    assert "Còn 3 người nữa để nhận lần thứ 2" in screen.last


@pytest.mark.asyncio
async def test_check_share_trang_thai_da_dat_toi_da(wired):
    ref = 7019
    await setup(ref)
    await set_setting("referral.max_claims", 1, "int")
    sender = FakeSender()
    await bring_many(sender, ref, 5, base=7190)

    screen = FakeSender()
    await referral_handler.handle_check_share(make_update(ref), make_context(screen))
    assert "✅ ĐÃ ĐẠT TỐI ĐA (1/1 lần)" in screen.last


@pytest.mark.asyncio
async def test_check_share_trang_thai_du_dieu_kien_khi_het_kho(wired):
    """Trạng thái 3 là TÍN HIỆU: đủ người mà chưa có thưởng ⇒ kho rỗng."""
    ref = 7020
    await setup(ref, codes=0)
    sender = FakeSender()
    await bring_many(sender, ref, 5, base=7200)

    screen = FakeSender()
    await referral_handler.handle_check_share(make_update(ref), make_context(screen))
    assert "🎁 ĐỦ ĐIỀU KIỆN nhận lần thứ 1!" in screen.last
    assert "⏳ Code đang được xử lý" in screen.last


@pytest.mark.asyncio
async def test_check_share_khong_ghi_gi_vao_db(wired):
    ref = 7021
    await setup(ref)
    sender = FakeSender()
    await bring_many(sender, ref, 5, base=7210)

    before = await scalar("SELECT count(*) FROM code_grants")
    for _ in range(3):
        await referral_handler.handle_check_share(make_update(ref), make_context(FakeSender()))
    assert await scalar("SELECT count(*) FROM code_grants") == before
    assert await count_rewards(ref) == 1


@pytest.mark.asyncio
async def test_join_referral_gui_bai_viet(wired):
    ref = 7022
    await setup(ref)
    sender = FakeSender()

    await referral_handler.handle_join_referral(
        make_update(ref, callback=True), make_context(sender)
    )

    assert sender.answers == ["📤 Đang tải bài viết để chia sẻ..."]
    assert "BÀI VIẾT CHIA SẺ ĐÃ SẴN SÀNG" in sender.messages[0]
    assert "TẶNG BẠN CODE TÂN THỦ" in sender.messages[1]
    button = sender.markups[1].inline_keyboard[0][0]
    assert button.url == f"https://t.me/{BOT_USERNAME}?start=ref_{ref}"


@pytest.mark.asyncio
async def test_chua_xac_minh_thi_bi_cong_chan(wired):
    ref = 7023
    await setup(ref)
    await run_sql("UPDATE users SET verified_at = NULL WHERE user_id = :uid", {"uid": ref})
    sender = FakeSender()

    await referral_handler.handle_moi_ban(make_update(ref), make_context(sender))
    assert "BẠN CHƯA XÁC THỰC TÀI KHOẢN" in sender.last

    sender = FakeSender()
    await referral_handler.handle_check_share(make_update(ref), make_context(sender))
    assert "BẠN CHƯA XÁC THỰC TÀI KHOẢN" in sender.last


@pytest.mark.asyncio
async def test_bam_qua_nhanh_bi_chan(wired):
    ref = 7025
    await setup(ref)
    await set_setting("cooldown.check_share", 5, "int")
    sender = FakeSender()

    await referral_handler.handle_check_share(make_update(ref), make_context(sender))
    await referral_handler.handle_check_share(make_update(ref), make_context(sender))

    assert "TIẾN ĐỘ CHIA SẺ" in sender.messages[0]
    assert "vui lòng chờ" in sender.messages[1]


@pytest.mark.asyncio
async def test_trong_nhom_bot_im_lang(wired):
    ref = 7024
    await setup(ref)
    sender = FakeSender()

    update = make_update(ref)
    update.effective_chat.type = "supergroup"
    await referral_handler.handle_moi_ban(update, make_context(sender))
    await referral_handler.handle_check_share(update, make_context(sender))

    assert sender.messages == []
