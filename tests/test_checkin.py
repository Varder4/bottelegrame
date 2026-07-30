"""Điểm danh và đổi điểm lấy code, chạy trên database thật.

Năm mệnh đề được kiểm, tất cả đều là chỗ hệ cũ mất tiền hoặc mất lòng người dùng:

- điểm danh hai lần trong cùng ngày VN → lần hai bị từ chối, điểm **không** cộng đôi;
- 23h50 và 00h10 giờ VN là **hai ngày khác nhau**, dù cùng một ngày UTC (§13.5 dòng H);
- nghỉ một ngày → streak về 1; liền ngày → streak +1;
- đổi code khi thiếu điểm → từ chối, số dư **không** đổi, kho code không bị đụng;
- đổi thành công → số dư giảm đúng bằng mệnh giá và có đúng một grant.

⚠️ Không dùng fixture `db`/`seeded` của `conftest`: handler và service đọc ghi qua engine
**toàn cục** (`db.engine.transaction()`). Dựng dữ liệu bằng một engine thứ hai là để hai
bên chạy trên hai kết nối khác nhau và thứ tự commit trở thành một cuộc đua — xem docstring
`tests/test_tanthu.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers import checkin as checkin_handler
from televip.core.clock import UTC, VN_TZ
from televip.core.errors import OutOfStock
from televip.db.engine import session as db_session
from televip.db.engine import transaction
from televip.domain import texts
from televip.services import checkin as checkin_service
from tests.conftest import TEST_DATABASE_URL, _truncate_all, add_codes, make_user

VERIFY_URL = "https://example.test/verify"
POINTS_PER_DAY = 2_000
TIERS = [10_000, 20_000, 50_000]

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
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.markups: list[Any] = []
        self.answers: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return 1000 + len(self.messages)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]


def make_update(user_id: int, *, callback_data: str | None = None) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=None, chat=chat)
    query = (
        SimpleNamespace(data=callback_data, message=message) if callback_data is not None else None
    )
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=query,
    )


def make_context(sender: FakeSender) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=SimpleNamespace(),
        args=[],
    )


# ── Đồng hồ ─────────────────────────────────────────────────────────


def at_vn(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Một mốc giờ Việt Nam, trả về dưới dạng UTC (đúng cách hệ thống lưu)."""
    return datetime(year, month, day, hour, minute, tzinfo=VN_TZ).astimezone(UTC)


def freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Ghim đồng hồ mà `business_date()` đọc.

    Vá `core.clock.now_utc` chứ không vá `business_date`: như vậy phép đổi múi giờ thật
    vẫn chạy, và test kiểm đúng cái ranh giới ngày mà bot cũ tính sai.
    """
    monkeypatch.setattr("televip.core.clock.now_utc", lambda: moment)


# ── Fixture: một engine duy nhất ────────────────────────────────────


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


# ── Helper ──────────────────────────────────────────────────────────


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


async def setup_user(user_id: int, *, codes: int = 5, code_value: int = 20_000) -> None:
    """Người dùng đã xác minh + kho code `diemdanh` + cấu hình tối thiểu."""
    async with db_session() as s:
        await make_user(s, user_id)
        if codes:
            await add_codes(
                s,
                code_type=checkin_service.CODE_TYPE,
                value_vnd=code_value,
                count=codes,
                prefix=f"D{user_id}",
            )
    await run_sql("UPDATE users SET verified_at = now() WHERE user_id = :uid", {"uid": user_id})
    await set_setting("webapp.url", VERIFY_URL)
    await set_setting("checkin.points_per_day", POINTS_PER_DAY, "int")
    await set_setting("checkin.reset_streak_on_miss", True, "bool")
    await set_setting("redeem.tiers", TIERS, "json")
    # Cooldown 0 để một test bấm được hai lần; luật chống spam có test riêng ở `test_cache`.
    await set_setting("cooldown.checkin", 0, "seconds")
    await set_setting("cooldown.redeem_code", 0, "seconds")


async def grant_points(user_id: int, amount: int) -> None:
    """Nạp điểm bằng đúng đường một lệnh admin đi: sổ cái + bộ đệm hiển thị, cùng lúc."""
    params = {"uid": user_id, "amt": amount, "ref": f"test:{user_id}:{amount}"}
    await run_sql(
        """
        INSERT INTO points_ledger (user_id, delta, reason, ref_key)
             VALUES (:uid, :amt, 'admin_adjust', :ref)
        """,
        params,
    )
    await run_sql(
        "UPDATE users SET points_balance = points_balance + :amt WHERE user_id = :uid", params
    )


async def count_of(table: str, user_id: int) -> int:
    return await scalar(f"SELECT count(*) FROM {table} WHERE user_id = :uid", {"uid": user_id})


async def cached_balance(user_id: int) -> int:
    return await scalar("SELECT points_balance FROM users WHERE user_id = :uid", {"uid": user_id})


async def used_codes() -> int:
    return await scalar("SELECT count(*) FROM codes WHERE status <> 'available'")


# ── 1. Hai lần trong cùng một ngày VN ───────────────────────────────


@pytest.mark.asyncio
async def test_diem_danh_hai_lan_cung_ngay_khong_cong_doi(wired, monkeypatch):
    uid = 7101
    await setup_user(uid, codes=0)

    freeze(monkeypatch, at_vn(2026, 3, 10, 9, 0))
    async with transaction() as db:
        first = await checkin_service.do_checkin(db, uid)

    # Cùng ngày nghiệp vụ, gần hết ngày — vẫn phải bị từ chối.
    freeze(monkeypatch, at_vn(2026, 3, 10, 23, 59))
    async with transaction() as db:
        second = await checkin_service.do_checkin(db, uid)

    assert (first.already, first.points, first.streak) == (False, POINTS_PER_DAY, 1)
    assert (second.already, second.points) == (True, 0)
    assert second.balance == first.balance == POINTS_PER_DAY, "lần hai KHÔNG được cộng điểm"

    assert await count_of("checkins", uid) == 1
    assert await count_of("points_ledger", uid) == 1, "sổ cái phải có đúng một bút toán"
    assert await cached_balance(uid) == POINTS_PER_DAY, "bộ đệm hiển thị phải khớp sổ cái"


@pytest.mark.asyncio
async def test_bam_nhanh_hai_lan_van_chi_mot_but_toan(wired, monkeypatch):
    """Hai lượt bấm trong cùng một khoảnh khắc: PK tổ hợp là thứ chặn, không phải câu `if`."""
    uid = 7102
    await setup_user(uid, codes=0)
    freeze(monkeypatch, at_vn(2026, 3, 10, 9, 0))

    async with transaction() as db:
        await checkin_service.do_checkin(db, uid)
    async with transaction() as db:
        again = await checkin_service.do_checkin(db, uid)

    assert again.already is True
    assert await cached_balance(uid) == POINTS_PER_DAY, "bot cũ cộng 4.000đ ở đúng chỗ này"


# ── 2. Ranh giới ngày là giờ VN, không phải giờ máy chủ ─────────────


@pytest.mark.asyncio
async def test_23h50_va_00h10_gio_vn_la_hai_ngay_khac_nhau(wired, monkeypatch):
    uid = 7201
    await setup_user(uid, codes=0)

    late = at_vn(2026, 3, 10, 23, 50)
    early = at_vn(2026, 3, 11, 0, 10)
    assert late.date() == early.date(), (
        "tiền đề của test: hai mốc này CÙNG một ngày UTC — máy chủ chạy UTC sẽ gộp chúng "
        "làm một, và đó chính là lỗi H của hệ cũ"
    )

    freeze(monkeypatch, late)
    async with transaction() as db:
        first = await checkin_service.do_checkin(db, uid)

    freeze(monkeypatch, early)
    async with transaction() as db:
        second = await checkin_service.do_checkin(db, uid)

    assert first.business_day.isoformat() == "2026-03-10"
    assert second.business_day.isoformat() == "2026-03-11"
    assert second.already is False, "23h50 và 00h10 là hai ngày VN — lượt hai phải được nhận"
    assert (first.streak, second.streak) == (1, 2), "hai ngày liền nhau ⇒ streak tăng"
    assert second.balance == 2 * POINTS_PER_DAY
    assert await count_of("checkins", uid) == 2


# ── 3. Streak ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streak_lien_ngay_tang_dut_ngay_ve_1(wired, monkeypatch):
    uid = 7301
    await setup_user(uid, codes=0)

    day1 = at_vn(2026, 3, 10, 8, 0)
    streaks = []
    for offset in (0, 1, 2):
        freeze(monkeypatch, day1 + timedelta(days=offset))
        async with transaction() as db:
            streaks.append((await checkin_service.do_checkin(db, uid)).streak)

    assert streaks == [1, 2, 3]

    # Nghỉ ngày 13/03 rồi quay lại ngày 14/03 → đứt.
    freeze(monkeypatch, day1 + timedelta(days=4))
    async with transaction() as db:
        after_gap = await checkin_service.do_checkin(db, uid)

    assert after_gap.streak == 1, "nghỉ một ngày là streak về 1 (checkin.reset_streak_on_miss)"
    assert await scalar("SELECT checkin_streak FROM users WHERE user_id = :uid", {"uid": uid}) == 1


@pytest.mark.asyncio
async def test_tat_reset_streak_thi_nghi_mot_ngay_van_tang(wired, monkeypatch):
    """`checkin.reset_streak_on_miss = false` là một khoá cấu hình thật, không phải trang trí."""
    uid = 7302
    await setup_user(uid, codes=0)
    await set_setting("checkin.reset_streak_on_miss", False, "bool")

    day1 = at_vn(2026, 3, 10, 8, 0)
    freeze(monkeypatch, day1)
    async with transaction() as db:
        await checkin_service.do_checkin(db, uid)

    freeze(monkeypatch, day1 + timedelta(days=3))
    async with transaction() as db:
        result = await checkin_service.do_checkin(db, uid)

    assert result.streak == 2


# ── 4. Đổi điểm: thiếu điểm ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_doi_code_thieu_diem_bi_tu_choi_va_khong_tru_gi(wired):
    uid = 7401
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 15_000)  # đủ bậc 10.000, thiếu bậc 20.000

    with pytest.raises(checkin_service.NotEnoughPoints) as excinfo:
        async with transaction() as db:
            await checkin_service.redeem(db, user_id=uid, value_vnd=20_000)

    assert excinfo.value.balance == 15_000
    assert excinfo.value.required == 20_000

    async with db_session() as db:
        assert await checkin_service.balance(db, uid) == 15_000, "số dư không được đổi"
    assert await cached_balance(uid) == 15_000
    assert await count_of("code_grants", uid) == 0
    assert await used_codes() == 0, "thiếu điểm mà kho code vẫn bị đụng"
    assert await count_of("points_ledger", uid) == 1, "chỉ còn bút toán nạp ban đầu"


@pytest.mark.asyncio
async def test_doi_code_het_kho_thi_khong_tru_diem(wired):
    """Bước ba hỏng ⇒ cả giao dịch cuộn lại. Đây là lý do ba bước nằm chung một transaction."""
    uid = 7402
    await setup_user(uid, codes=0)
    await grant_points(uid, 50_000)

    with pytest.raises(OutOfStock):
        async with transaction() as db:
            await checkin_service.redeem(db, user_id=uid, value_vnd=20_000)

    async with db_session() as db:
        assert await checkin_service.balance(db, uid) == 50_000
    assert await cached_balance(uid) == 50_000
    assert await count_of("code_grants", uid) == 0


@pytest.mark.asyncio
async def test_doi_bac_khong_co_trong_cau_hinh_bi_tu_choi(wired):
    uid = 7403
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 999_000)

    with pytest.raises(checkin_service.InvalidRedeemTier):
        async with transaction() as db:
            await checkin_service.redeem(db, user_id=uid, value_vnd=999_000)

    assert await cached_balance(uid) == 999_000


# ── 5. Đổi điểm: thành công ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_doi_code_thanh_cong_tru_dung_va_co_grant(wired):
    uid = 7501
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 50_000)

    async with transaction() as db:
        grant = await checkin_service.redeem(db, user_id=uid, value_vnd=20_000)
        remaining = await checkin_service.balance(db, uid)

    assert grant.value_vnd == 20_000
    assert grant.was_existing is False
    assert remaining == 30_000, "số dư phải giảm ĐÚNG bằng mệnh giá"

    async with db_session() as db:
        assert await checkin_service.balance(db, uid) == 30_000
    assert await cached_balance(uid) == 30_000, "bộ đệm hiển thị phải khớp sổ cái"
    assert await used_codes() == 1

    row = await scalar(
        """
        SELECT g.grant_type || '|' || g.grant_key || '|' || g.state
          FROM code_grants g WHERE g.user_id = :uid
        """,
        {"uid": uid},
    )
    grant_type, grant_key, state = row.split("|")
    assert grant_type == checkin_service.GRANT_TYPE
    assert grant_key.startswith(f"redeem:{uid}:")
    assert state == "reserved", "mark_delivered() là việc của handler, sau khi Telegram xác nhận"

    ledger = await scalar(
        "SELECT delta FROM points_ledger WHERE user_id = :uid AND reason = 'redeem'",
        {"uid": uid},
    )
    assert ledger == -20_000


@pytest.mark.asyncio
async def test_doi_code_bam_lai_trong_ngay_tra_dung_ma_cu(wired):
    """Idempotency theo ngày nghiệp vụ: bấm lại KHÔNG trừ điểm lần hai."""
    uid = 7502
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 50_000)

    async with transaction() as db:
        first = await checkin_service.redeem(db, user_id=uid, value_vnd=20_000)
    async with transaction() as db:
        second = await checkin_service.redeem(db, user_id=uid, value_vnd=20_000)

    assert second.was_existing is True
    assert second.code_value == first.code_value, "hai lần bấm phải ra cùng một mã"
    assert await cached_balance(uid) == 30_000, "lần hai KHÔNG được trừ thêm"
    assert await count_of("code_grants", uid) == 1
    assert await used_codes() == 1


# ── 6. Qua chính handler ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nut_diem_danh_gui_man_hinh_thanh_cong(wired, monkeypatch):
    uid = 7601
    await setup_user(uid, codes=0)
    freeze(monkeypatch, at_vn(2026, 3, 10, 9, 0))

    sender = FakeSender()
    await checkin_handler.handle_checkin(make_update(uid), make_context(sender))

    assert "ĐIỂM DANH THÀNH CÔNG" in sender.last
    assert texts.checkin_tier_lines(TIERS) in sender.last, "bậc đổi sinh từ settings.redeem.tiers"
    assert "🔥 Chuỗi liên tiếp: 1 ngày" in sender.last

    await checkin_handler.handle_checkin(make_update(uid), make_context(sender))
    assert "BẠN ĐÃ ĐIỂM DANH HÔM NAY RỒI" in sender.last
    assert await count_of("points_ledger", uid) == 1


@pytest.mark.asyncio
async def test_nut_diem_danh_chua_xac_minh_bi_chan(wired, monkeypatch):
    uid = 7602
    await setup_user(uid, codes=0)
    await run_sql("UPDATE users SET verified_at = NULL WHERE user_id = :uid", {"uid": uid})
    freeze(monkeypatch, at_vn(2026, 3, 10, 9, 0))

    sender = FakeSender()
    await checkin_handler.handle_checkin(make_update(uid), make_context(sender))

    assert sender.last == texts.not_verified()
    assert await count_of("checkins", uid) == 0


@pytest.mark.asyncio
async def test_nut_doi_code_hien_moi_bac_va_chi_nut_du_diem(wired):
    uid = 7603
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 25_000)

    sender = FakeSender()
    await checkin_handler.handle_redeem_menu(make_update(uid), make_context(sender))

    assert "ĐỔI ĐIỂM LẤY CODE" in sender.last
    for tier in TIERS:
        assert f"Code {texts.value_label(tier)}" in sender.last, "phải liệt kê TẤT CẢ bậc"
    assert "Còn thiếu 25.000đ" in sender.last, "bậc 50K còn thiếu đúng 25.000đ"

    buttons = [b.callback_data for row in sender.markups[-1].inline_keyboard for b in row]
    assert buttons == ["redeem_10000", "redeem_20000"], "chỉ bậc đủ điểm mới có nút"


@pytest.mark.asyncio
async def test_callback_doi_code_thanh_cong(wired):
    uid = 7604
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 50_000)
    await set_setting("link.game_bot", "https://televip.game")

    sender = FakeSender()
    await checkin_handler.handle_redeem(
        make_update(uid, callback_data="redeem_20000"), make_context(sender)
    )

    assert sender.answers == [texts.ALERT_REDEEM_OK]
    assert "ĐỔI CODE THÀNH CÔNG" in sender.last
    assert "💰 Số dư còn: 30.000đ" in sender.last

    code_value = await scalar(
        "SELECT c.code_value FROM code_grants g JOIN codes c USING (code_id) WHERE g.user_id = :uid",
        {"uid": uid},
    )
    assert code_value in sender.last
    assert (
        await scalar("SELECT state FROM code_grants WHERE user_id = :uid", {"uid": uid})
        == "delivered"
    ), "mark_delivered() phải chạy sau khi gửi thành công"
    assert await scalar("SELECT status FROM codes WHERE code_value = :cv", {"cv": code_value}) == (
        "issued"
    )
    assert await cached_balance(uid) == 30_000


@pytest.mark.asyncio
async def test_callback_doi_code_thieu_diem_khong_tru_va_khong_dung_kho(wired):
    uid = 7605
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 10_000)

    sender = FakeSender()
    await checkin_handler.handle_redeem(
        make_update(uid, callback_data="redeem_20000"), make_context(sender)
    )

    assert sender.answers == [texts.ALERT_NOT_ENOUGH_POINTS]
    assert "KHÔNG ĐỦ ĐIỂM" in sender.last
    assert "💰 Số dư của bạn: 10.000đ" in sender.last
    assert await cached_balance(uid) == 10_000
    assert await count_of("code_grants", uid) == 0
    assert await used_codes() == 0


@pytest.mark.asyncio
async def test_callback_doi_code_het_kho_bao_dung_nguyen_nhan(wired):
    uid = 7606
    await setup_user(uid, codes=0)
    await grant_points(uid, 50_000)
    await set_setting("link.support", "https://t.me/cskh")

    sender = FakeSender()
    await checkin_handler.handle_redeem(
        make_update(uid, callback_data="redeem_20000"), make_context(sender)
    )

    assert sender.answers == [texts.ALERT_OUT_OF_STOCK]
    assert sender.last == texts.redeem_out_of_stock(
        value_vnd=20_000, balance=50_000, support_link="https://t.me/cskh"
    )
    assert await cached_balance(uid) == 50_000, "hết kho thì điểm KHÔNG bị trừ"


@pytest.mark.asyncio
async def test_callback_data_bia_tay_khong_lam_gi(wired):
    uid = 7607
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 50_000)

    sender = FakeSender()
    for data in ("redeem_abc", "redeem_", "linh_tinh"):
        await checkin_handler.handle_redeem(
            make_update(uid, callback_data=data), make_context(sender)
        )

    assert sender.answers == ["", "", ""], "vẫn phải answer, nếu không nút quay vòng mãi"
    assert sender.messages == []
    assert await count_of("code_grants", uid) == 0
    assert await cached_balance(uid) == 50_000


@pytest.mark.asyncio
async def test_callback_bac_ngoai_cau_hinh_khong_phat_gi(wired):
    """`redeem_999999999` là chuỗi hợp lệ về cú pháp — bậc phải được kiểm lại ở server."""
    uid = 7608
    await setup_user(uid, codes=3, code_value=20_000)
    await grant_points(uid, 999_999_999)

    sender = FakeSender()
    await checkin_handler.handle_redeem(
        make_update(uid, callback_data="redeem_999999999"), make_context(sender)
    )

    assert sender.messages == []
    assert await count_of("code_grants", uid) == 0
    assert await used_codes() == 0


# ── 7. Số dư luôn tính từ sổ cái ────────────────────────────────────


@pytest.mark.asyncio
async def test_so_du_tinh_tu_so_cai_khong_doc_cot_roi(wired, monkeypatch):
    uid = 7701
    await setup_user(uid, codes=0)
    freeze(monkeypatch, at_vn(2026, 3, 10, 9, 0))
    async with transaction() as db:
        await checkin_service.do_checkin(db, uid)

    # Bẻ bộ đệm hiển thị: `balance()` phải bỏ qua nó và đọc `points_ledger`.
    await run_sql("UPDATE users SET points_balance = 999999 WHERE user_id = :uid", {"uid": uid})
    async with db_session() as db:
        assert await checkin_service.balance(db, uid) == POINTS_PER_DAY


def test_parse_redeem_value():
    assert checkin_handler.parse_redeem_value("redeem_10000") == 10_000
    assert checkin_handler.parse_redeem_value("redeem_x") is None
    assert checkin_handler.parse_redeem_value("redeem_") is None
    assert checkin_handler.parse_redeem_value("dap_hop_3") is None
    assert checkin_handler.parse_redeem_value(None) is None
