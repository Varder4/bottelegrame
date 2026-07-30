"""Lớp thu thập tín hiệu định danh — trigger `signal_owners` và `/checkip`.

Đây là phần **đo**, không phải phần **chặn**. `04-fraud.md` cố ý để các bảng luật rỗng
cho tới khi có bốn tuần số liệu shadow, vì bật luật sớm là chặn oan người thật. Nhưng
bốn tuần ấy chỉ bắt đầu đếm khi dữ liệu thật sự được ghi — và trước migration `0011`,
`signal_owners` là một bảng **không có ai ghi vào**: 0 dòng, và sẽ mãi 0 dòng.

Bốn mệnh đề trọng tâm:

- Ghi một tín hiệu ⇒ `signal_owners` tự có dòng chiều ngược, không cần ai gọi gì.
- `user_count` đếm theo **tài khoản phân biệt**, không phải theo số lượt.
- `first_user_id` là người ĐẦU TIÊN — không đổi khi có người mới, không đổi khi người
  đầu tiên bị xoá khỏi tín hiệu.
- `/checkip` không bao giờ in một điểm rủi ro bịa, và luôn nói rõ đây là số đo chứ không
  phải kết luận.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import identity as idt
from televip.db.engine import session as db_session
from tests.conftest import TEST_DATABASE_URL, _truncate_all, make_user

OWNER_ID = 960_001
OUTSIDER_ID = 960_002


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return len(self.messages)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        return None

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]


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
    from televip.services import settings_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    try:
        settings_service.invalidate()
        admin_service.invalidate_role()
        async with db_session() as s:
            await _truncate_all(s)
            await s.commit()
        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()
        admin_service.invalidate_role()


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one_or_none()


async def ghi_tin_hieu(user_id: int, gia_tri: str, *, loai: str = "ip") -> None:
    """Ghi đúng bằng câu mà `/api/verify` dùng — upsert tăng `hits`."""
    async with db_session() as s:
        await make_user(s, user_id)
        await s.execute(
            text("""
            INSERT INTO identity_signals (user_id, signal_type, signal_value)
                 VALUES (:uid, :loai, :val)
            ON CONFLICT (user_id, signal_type, signal_value) DO UPDATE
                    SET hits = identity_signals.hits + 1, last_seen = now()
            """),
            {"uid": user_id, "loai": loai, "val": gia_tri},
        )
        await s.commit()


async def owner_row(gia_tri: str, loai: str = "ip") -> Any:
    async with db_session() as s:
        return (
            await s.execute(
                text("SELECT * FROM signal_owners WHERE signal_type = :l AND signal_value = :v"),
                {"l": loai, "v": gia_tri},
            )
        ).one_or_none()


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
    await run_sql(
        "INSERT INTO admin_permissions (role, command) VALUES ('owner', '/checkip') "
        "ON CONFLICT DO NOTHING"
    )
    admin_service.invalidate_role(OWNER_ID)
    return OWNER_ID


# ── Trigger `signal_owners` ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghi_tin_hieu_thi_bang_chieu_nguoc_tu_co_dong(wired):
    """Không ai gọi gì thêm — nếu phải gọi thì sớm muộn cũng có đường ghi quên gọi."""
    await ghi_tin_hieu(961_001, "1.2.3.4")

    row = await owner_row("1.2.3.4")
    assert row is not None, "signal_owners vẫn rỗng — bảng không có ai ghi vào"
    assert row.user_count == 1
    assert row.first_user_id == 961_001


@pytest.mark.asyncio
async def test_user_count_dem_theo_TAI_KHOAN_khong_theo_luot(wired):
    await ghi_tin_hieu(961_010, "1.2.3.4")
    await ghi_tin_hieu(961_010, "1.2.3.4")  # cùng người, lượt thứ hai
    await ghi_tin_hieu(961_011, "1.2.3.4")

    row = await owner_row("1.2.3.4")
    assert row.user_count == 2, "đếm nhầm số lượt thành số tài khoản"
    assert await scalar("SELECT hits FROM identity_signals WHERE user_id = 961010") == 2


@pytest.mark.asyncio
async def test_first_user_id_giu_nguoi_dau_tien(wired):
    await ghi_tin_hieu(961_020, "5.6.7.8")
    await ghi_tin_hieu(961_021, "5.6.7.8")

    assert (await owner_row("5.6.7.8")).first_user_id == 961_020

    # Xoá người đầu tiên khỏi tín hiệu: `first_user_id` KHÔNG được đổi nghĩa thành
    # "người có id nhỏ nhất còn lại".
    await run_sql("DELETE FROM identity_signals WHERE user_id = 961020")
    row = await owner_row("5.6.7.8")
    assert row.user_count == 1
    assert row.first_user_id == 961_020, "first_user_id đổi nghĩa sau một lần xoá"


@pytest.mark.asyncio
async def test_xoa_het_tin_hieu_thi_bo_luon_dong_chieu_nguoc(wired):
    await ghi_tin_hieu(961_030, "9.9.9.9")
    await run_sql("DELETE FROM identity_signals WHERE signal_value = '9.9.9.9'")

    assert await owner_row("9.9.9.9") is None, "để lại một dòng nói '0 tài khoản'"


@pytest.mark.asyncio
async def test_user_count_dem_lai_chu_khong_cong_don(wired):
    """Cộng dồn trôi vĩnh viễn sau một lần xoá; đếm lại thì tự đúng trở lại."""
    for uid in (961_040, 961_041, 961_042):
        await ghi_tin_hieu(uid, "8.8.8.8")
    assert (await owner_row("8.8.8.8")).user_count == 3

    await run_sql("DELETE FROM identity_signals WHERE user_id = 961041")
    assert (await owner_row("8.8.8.8")).user_count == 2, "bộ đếm trôi khỏi sự thật"


@pytest.mark.asyncio
async def test_hai_gia_tri_khac_nhau_khong_dung_nhau(wired):
    await ghi_tin_hieu(961_050, "1.1.1.1")
    await ghi_tin_hieu(961_051, "2.2.2.2")

    assert (await owner_row("1.1.1.1")).user_count == 1
    assert (await owner_row("2.2.2.2")).user_count == 1


# ── /checkip ────────────────────────────────────────────────────────


def test_parse_ip_chuan_hoa_truoc_khi_so():
    """Gõ `2405:4802:0:0::1` phải khớp dòng lưu dưới dạng `2405:4802::1`."""
    assert idt.parse_ip("1.2.3.4") == "1.2.3.4"
    assert idt.parse_ip("2405:4802:0:0:0:0:0:1") == "2405:4802::1"
    assert idt.parse_ip("@nguoidung") is None
    assert idt.parse_ip("123456789") is None, "user_id không được đọc nhầm thành IP"


def test_rut_gon_ip_che_phan_cuoi():
    assert idt.rut_gon_ip("192.168.10.55") == "192.168.10.x"
    assert idt.rut_gon_ip("2405:4802:b278:aaa0:79cc:9c2a:2b11:8a45").endswith("…")
    assert idt.rut_gon_ip("khong-phai-ip") == "khong-phai-ip"


@pytest.mark.asyncio
async def test_checkip_theo_nguoi_liet_ke_tin_hieu_va_so_dung_chung(owner):
    await ghi_tin_hieu(962_001, "10.0.0.1")
    await ghi_tin_hieu(962_002, "10.0.0.1")
    await ghi_tin_hieu(962_003, "10.0.0.1")

    sender = FakeSender()
    await idt.cmd_checkip(make_update(owner), make_context(sender, "962001"))

    assert "3 tài khoản dùng chung" in sender.last
    assert "⚠️" in sender.last, "ba tài khoản chung một IP phải được đánh dấu"
    assert "10.0.0.x" in sender.last, "IP phải hiện dạng rút gọn"
    assert "10.0.0.1" not in sender.last, "in nguyên IP — dán đi nơi khác được"


@pytest.mark.asyncio
async def test_checkip_theo_IP_liet_ke_tai_khoan(owner):
    await ghi_tin_hieu(962_010, "10.0.0.2")
    await ghi_tin_hieu(962_011, "10.0.0.2")
    await run_sql("UPDATE users SET verified_at = now() WHERE user_id = 962010")
    await run_sql(
        "INSERT INTO user_bans (user_id, reason, banned_by) VALUES (962011, 'thu', :a)",
        {"a": OWNER_ID},
    )

    sender = FakeSender()
    await idt.cmd_checkip(make_update(owner), make_context(sender, "10.0.0.2"))

    assert "2 tài khoản" in sender.last
    assert "✅ 962010" in sender.last
    assert "🚫 962011" in sender.last
    # ...nhưng KHÔNG có @username của ai: một IP không được biến thành một danh bạ.
    assert "@u962010" not in sender.last


@pytest.mark.asyncio
async def test_checkip_khong_bao_gio_in_diem_rui_ro_bia(owner):
    """`risk_assessments` chưa có ai ghi. In `0` sẽ là một con số bịa."""
    await ghi_tin_hieu(962_020, "10.0.0.3")

    sender = FakeSender()
    await idt.cmd_checkip(make_update(owner), make_context(sender, "962020"))

    assert "chưa bật" in sender.last, "phải nói rõ lớp chấm điểm chưa chạy"
    assert "Điểm rủi ro: 0" not in sender.last


@pytest.mark.asyncio
async def test_checkip_noi_ro_vi_sao_trong(owner):
    """Admin không được phải đoán giữa 'sạch' và 'chưa đo'."""
    async with db_session() as s:
        await make_user(s, 962_030)
        await s.commit()

    sender = FakeSender()
    await idt.cmd_checkip(make_update(owner), make_context(sender, "962030"))

    assert "chưa có tín hiệu nào" in sender.last
    assert "Mini App" in sender.last, "phải nói tín hiệu được ghi lúc nào"


@pytest.mark.asyncio
async def test_checkip_luon_kem_canh_bao_day_la_so_do(owner):
    """Một màn hình toàn tín hiệu trông như một kết luận, và người đọc sẽ khoá tài khoản."""
    await ghi_tin_hieu(962_040, "10.0.0.4")

    sender = FakeSender()
    await idt.cmd_checkip(make_update(owner), make_context(sender, "962040"))

    assert "không phải kết luận" in sender.last
    assert "NAT nhà mạng" in sender.last


@pytest.mark.asyncio
async def test_checkip_khong_tim_thay_thi_noi_ca_hai_kha_nang(owner):
    sender = FakeSender()
    await idt.cmd_checkip(make_update(owner), make_context(sender, "@aokhongcothat"))
    assert "Không tìm thấy" in sender.last
    assert "IP hợp lệ" in sender.last


@pytest.mark.asyncio
async def test_checkip_nguoi_ngoai_khong_tra_duoc(wired):
    """Tra tín hiệu định danh của người khác là quyền phải gác."""
    async with db_session() as s:
        await make_user(s, OUTSIDER_ID)
        await s.commit()
    await ghi_tin_hieu(962_050, "10.0.0.5")

    sender = FakeSender()
    await idt.cmd_checkip(make_update(OUTSIDER_ID), make_context(sender, "10.0.0.5"))

    assert sender.messages == []
