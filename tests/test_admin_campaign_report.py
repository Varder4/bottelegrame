"""`/chiendich` và `/baocao` — hai lệnh điều khiển và đọc đường tiêu tiền.

Bốn mệnh đề trọng tâm:

- `/chiendich end` phải tắt **mọi** hàng đang bật. Sót một hàng nghĩa là admin đọc được
  "đã dừng" mà van tiền vẫn mở.
- `/chiendich extend` cộng dồn từ hạn **cũ**. Tính từ `now()` là âm thầm cắt ngắn phần
  thời gian đang có.
- `/chiendich start` khi đang có chiến dịch chạy thì phải **thay thế**, không để hai hàng
  cùng bật — `campaign_window()` chỉ nhìn hàng mới nhất, nên hàng cũ thành vô hình mà vẫn
  nằm đó.
- `/baocao` đếm trên `code_grants` với `state='delivered'`, và cắt kỳ theo **ngày nghiệp
  vụ giờ VN**. Cắt theo UTC đẩy 7 tiếng cuối mỗi ngày sang kỳ sau.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import campaign as cd
from televip.apps.worker.handlers.admin import report as rp
from televip.db.engine import session as db_session
from tests.conftest import TEST_DATABASE_URL, _truncate_all, make_user

OWNER_ID = 950_001
OUTSIDER_ID = 950_002

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


class FakeSender:
    def __init__(self, *, deliver: bool = True) -> None:
        self.messages: list[str] = []
        self.documents: list[tuple[str, bytes]] = []
        self.deliver = deliver

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return len(self.messages) if self.deliver else None

    async def send_document(
        self, chat_id: int, document: bytes, *, filename: str, **kwargs: Any
    ) -> int | None:
        self.documents.append((filename, document))
        return len(self.documents) if self.deliver else None

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        return None

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]

    @property
    def all_text(self) -> str:
        return "\n".join(self.messages)


def make_update(user_id: int) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=None, chat=chat, reply_to_message=None)
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=None,
    )


def make_context(sender: FakeSender, *args: str) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=SimpleNamespace(),
        args=list(args),
    )


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine
    from televip.services import admin as admin_service
    from televip.services import settings_service, text_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    try:
        settings_service.invalidate()
        text_service.invalidate()
        admin_service.invalidate_role()
        async with db_session() as s:
            await _truncate_all(s)
            await s.execute(text(_GRANT_TYPES_SQL))
            await s.commit()
        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()
        text_service.invalidate()
        admin_service.invalidate_role()


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def set_setting(key: str, value: Any, value_type: str = "string") -> None:
    from televip.services import settings_service

    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi)
             VALUES (:k, CAST(:v AS jsonb), :t, :k)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {"k": key, "v": json.dumps(value), "t": value_type},
    )
    settings_service.invalidate()


@pytest_asyncio.fixture
async def owner(wired):
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, OWNER_ID)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, 'owner', :u) "
        "ON CONFLICT (user_id) DO UPDATE SET revoked_at = NULL",
        {"u": OWNER_ID},
    )
    for command in ("/chiendich", "/baocao"):
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": command},
        )
    admin_service.invalidate_role(OWNER_ID)
    return OWNER_ID


# ══════════════════════════════════════════════════════════════════════
# /chiendich
# ══════════════════════════════════════════════════════════════════════


def test_parse_days_chan_so_ngoai_khoang():
    assert cd.parse_days("30") == 30
    assert cd.parse_days("0") is None
    assert cd.parse_days("-3") is None
    assert cd.parse_days("abc") is None
    assert cd.parse_days(str(cd.MAX_DAYS)) == cd.MAX_DAYS
    assert cd.parse_days(str(cd.MAX_DAYS + 1)) is None, (
        "một cú gõ thừa chữ số biến 30 ngày thành 300 ngày, và cửa sổ mở là cửa sổ đang chi"
    )


@pytest.mark.asyncio
async def test_chua_co_chien_dich_thi_noi_ro_la_khong_phat_thuong(owner):
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender))

    assert "KHÔNG CÓ CHIẾN DỊCH" in sender.last
    assert "KHÔNG phát mốc mời bạn nào" in sender.last, (
        "phải nói ra hệ quả tiền, không chỉ nói trạng thái"
    )


@pytest.mark.asyncio
async def test_start_mo_cua_so_va_ghi_so(owner):
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "start", "30", "Hè", "rực rỡ"))

    assert "ĐÃ MỞ CHIẾN DỊCH" in sender.last
    row = await scalar("SELECT name FROM campaigns WHERE is_active")
    assert row == "Hè rực rỡ", "tên nhiều từ bị cắt mất phần sau"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'chiendich_start'") == 1

    # ...và luồng phát thưởng thật sự nhìn thấy nó.
    from televip.services import referral

    async with db_session() as s:
        assert (await referral.campaign_window(s)).is_running


@pytest.mark.asyncio
async def test_start_lan_hai_thay_the_chu_khong_de_hai_hang_cung_bat(owner):
    """`campaign_window()` chỉ nhìn hàng mới nhất — hàng cũ còn bật là trạng thái không đọc được."""
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "start", "10", "Cũ"))
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "start", "20", "Mới"))

    assert await scalar("SELECT count(*) FROM campaigns WHERE is_active") == 1
    assert await scalar("SELECT name FROM campaigns WHERE is_active") == "Mới"
    assert "Đã dừng 1 chiến dịch" in sender.last, "thay thế im lặng thì admin không biết"


@pytest.mark.asyncio
async def test_extend_cong_don_tu_han_cu(owner):
    """Gia hạn 7 ngày cho chiến dịch còn 3 ngày phải ra 10 ngày, không phải 7."""
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "start", "3", "Ngắn"))
    truoc = await scalar("SELECT ends_at FROM campaigns WHERE is_active")

    await cd.cmd_chiendich(make_update(owner), make_context(sender, "extend", "7"))
    sau = await scalar("SELECT ends_at FROM campaigns WHERE is_active")

    assert (sau - truoc).days == 7, "tính từ now() là âm thầm cắt ngắn phần đang có"
    assert "GIA HẠN" in sender.last


@pytest.mark.asyncio
async def test_extend_khi_khong_co_gi_dang_chay_thi_tu_choi(owner):
    """Gia hạn một cửa sổ đã đóng chính là mở lại van tiền — hai việc phải nhìn khác nhau."""
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "extend", "7"))

    assert "Không có chiến dịch nào ĐANG CHẠY" in sender.last
    assert await scalar("SELECT count(*) FROM campaigns") == 0


@pytest.mark.asyncio
async def test_end_tat_moi_hang_dang_bat(owner):
    """Sót một hàng bật ở giữa bảng = admin đọc "đã dừng" mà van tiền vẫn mở."""
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "start", "10", "A"))
    # Dựng thẳng một hàng bật thứ hai — mô phỏng dữ liệu cũ hoặc một lần sửa tay.
    await run_sql(
        """
        INSERT INTO campaigns
               (code, name, interval_people, reward_value_vnd, max_claims,
                starts_at, ends_at, is_active)
        VALUES ('cu', 'B', 5, 10000, 10, now(), now() + interval '5 days', true)
        """
    )
    assert await scalar("SELECT count(*) FROM campaigns WHERE is_active") == 2

    await cd.cmd_chiendich(make_update(owner), make_context(sender, "end"))

    assert await scalar("SELECT count(*) FROM campaigns WHERE is_active") == 0
    assert "ĐÃ DỪNG 2" in sender.last


@pytest.mark.asyncio
async def test_end_khi_khong_co_gi_bat(owner):
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "end"))
    assert "Không có chiến dịch nào đang bật" in sender.last


@pytest.mark.asyncio
async def test_hanh_dong_la_thi_in_cach_dung(owner):
    sender = FakeSender()
    await cd.cmd_chiendich(make_update(owner), make_context(sender, "xoahet"))
    assert "Không hiểu" in sender.last and "CÁCH DÙNG" in sender.last


@pytest.mark.asyncio
async def test_nguoi_ngoai_khong_mo_duoc_van_tien(wired):
    async with db_session() as s:
        await make_user(s, OUTSIDER_ID)
        await s.commit()

    sender = FakeSender()
    await cd.cmd_chiendich(make_update(OUTSIDER_ID), make_context(sender, "start", "365"))

    assert sender.messages == []
    assert await scalar("SELECT count(*) FROM campaigns") == 0


# ══════════════════════════════════════════════════════════════════════
# /baocao
# ══════════════════════════════════════════════════════════════════════


def test_parse_args_baocao():
    assert rp.parse_args([]) == (rp.DEFAULT_PERIOD, False)
    assert rp.parse_args(["tuan"]) == ("tuan", False)
    assert rp.parse_args(["thang", "csv"]) == ("thang", True)
    assert rp.parse_args(["csv", "ngay"]) == ("ngay", True)
    assert rp.parse_args(["tuann"]) is None, (
        "gõ nhầm tên kỳ mà im lặng chạy kỳ mặc định là trả báo cáo của khoảng thời gian khác"
    )


def test_period_bounds_cat_theo_ngay_nghiep_vu_gio_vn():
    hom_nay = date(2026, 7, 30)
    tu_ngay, den_ngay = rp.period_bounds("ngay", hom_nay)
    tu_tuan, den_tuan = rp.period_bounds("tuan", hom_nay)

    assert den_ngay == den_tuan, "hai kỳ phải cùng kết thúc ở cuối ngày hôm nay"
    assert (den_ngay - tu_ngay).days == 1
    assert (den_tuan - tu_tuan).days == 7, "`tuan` là 7 ngày gần nhất, tính cả hôm nay"
    # 00:00 giờ VN = 17:00 UTC hôm trước. Cắt theo UTC sẽ ra 00:00 và bài này đỏ.
    assert tu_ngay.hour == 17


async def _phat_ma(user_id: int, *, grant_type: str, value_vnd: int, delivered: bool) -> None:
    async with db_session() as s:
        await make_user(s, user_id)
        await s.commit()
    await run_sql(
        """
        INSERT INTO code_grants
               (grant_key, user_id, grant_type, value_vnd, state, idempotency_key, delivered_at)
        VALUES (:k, :u, :gt, :v, :st, :k, CASE WHEN :dl THEN now() ELSE NULL END)
        """,
        {
            "k": f"{grant_type}:{user_id}",
            "u": user_id,
            "gt": grant_type,
            "v": value_vnd,
            "st": "delivered" if delivered else "reserved",
            "dl": delivered,
        },
    )


@pytest.mark.asyncio
async def test_baocao_chi_dem_ma_da_giao(owner):
    """Mã giữ chỗ mà gửi hỏng sẽ quay về kho — tính nó là đã chi là báo cáo thừa tiền."""
    await _phat_ma(951_001, grant_type="tanthu", value_vnd=10_000, delivered=True)
    await _phat_ma(951_002, grant_type="tanthu", value_vnd=10_000, delivered=False)

    sender = FakeSender()
    await rp.cmd_baocao(make_update(owner), make_context(sender))

    assert "10.000đ (1 mã)" in sender.last
    assert "Người nhận: 1" in sender.last


@pytest.mark.asyncio
async def test_baocao_tach_theo_luong_va_menh_gia(owner):
    await _phat_ma(951_010, grant_type="tanthu", value_vnd=10_000, delivered=True)
    await _phat_ma(951_011, grant_type="event_box", value_vnd=88_000, delivered=True)

    sender = FakeSender()
    await rp.cmd_baocao(make_update(owner), make_context(sender, "tuan"))

    assert "7 NGÀY QUA" in sender.last
    assert "tanthu · 10K" in sender.last
    assert "event_box · 88K" in sender.last
    assert "98.000đ (2 mã)" in sender.last


@pytest.mark.asyncio
async def test_baocao_ky_rong_khong_bia_ra_ti_le(owner):
    """In `0%` cho một kỳ không có ai vào là bịa ra một con số."""
    # Đẩy mọi người dùng ra khỏi kỳ, kể cả chính admin do fixture tạo — nếu không thì mẫu
    # số khác 0 và bài này đo nhầm thứ khác.
    await run_sql("UPDATE users SET joined_at = now() - interval '90 days'")

    sender = FakeSender()
    await rp.cmd_baocao(make_update(owner), make_context(sender))

    assert "chưa phát mã nào" in sender.last
    assert "đã xác minh" not in sender.last


@pytest.mark.asyncio
async def test_baocao_ti_le_tinh_tren_DUNG_MOT_tep_nguoi(owner):
    """Tử số và mẫu số phải là cùng một tệp, nếu không tỉ lệ in ra được 300%.

    Dựng đúng cái bẫy: hai người VÀO trong kỳ (một đã xác minh), cộng hai người vào từ
    tháng trước nhưng XÁC MINH hôm nay. Đếm `verified_at` trong kỳ làm tử số sẽ ra 3/2.
    """
    await run_sql("UPDATE users SET joined_at = now() - interval '60 days'")  # kể cả owner

    async with db_session() as s:
        for uid in (951_040, 951_041, 951_050, 951_051):
            await make_user(s, uid)
        await s.commit()
    # Hai người CŨ, xác minh hôm nay.
    await run_sql(
        "UPDATE users SET joined_at = now() - interval '60 days', verified_at = now() "
        "WHERE user_id IN (951050, 951051)"
    )
    # Hai người MỚI, một xác minh.
    await run_sql("UPDATE users SET verified_at = now() WHERE user_id = 951040")

    sender = FakeSender()
    await rp.cmd_baocao(make_update(owner), make_context(sender))

    assert "Trong 2 người mới: 1 đã xác minh (50%)" in sender.last, sender.last
    # ...và số lượt xác minh xảy ra trong kỳ vẫn được báo, chỉ là ở một dòng khác.
    assert "lượt xác minh trong kỳ: 3" in sender.last


@pytest.mark.asyncio
async def test_baocao_csv_xuat_dung_nhung_dong_vua_hien(owner):
    await _phat_ma(951_020, grant_type="tanthu", value_vnd=10_000, delivered=True)

    sender = FakeSender()
    await rp.cmd_baocao(make_update(owner), make_context(sender, "csv"))

    assert sender.documents, "gõ csv mà không có file nào"
    ten, noi_dung = sender.documents[-1]
    assert ten.endswith(".csv")
    assert noi_dung.startswith(b"\xef\xbb\xbf"), "thiếu BOM — Excel bản Việt mở ra chữ vỡ"
    text_csv = noi_dung.decode("utf-8-sig")
    assert "tanthu" in text_csv and "10000" in text_csv


@pytest.mark.asyncio
async def test_baocao_gui_file_that_bai_thi_noi_ra(owner):
    await _phat_ma(951_030, grant_type="tanthu", value_vnd=10_000, delivered=True)

    sender = FakeSender(deliver=False)
    await rp.cmd_baocao(make_update(owner), make_context(sender, "csv"))

    assert "Gửi file CSV thất bại" in sender.last, "admin ngồi đợi một tệp không bao giờ đến"
