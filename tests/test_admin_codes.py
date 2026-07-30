"""Lệnh quản lý kho code, chạy **qua chính handler** trên PostgreSQL thật.

Bốn mệnh đề bắt buộc, mỗi cái là một chỗ hệ cũ làm sai:

- nạp 5 mã trong đó 2 trùng ⇒ báo đúng **3 mới / 2 bỏ qua**, kho tăng đúng **3**;
- `/tonkho` ra đúng số, và đánh dấu ⚠️ đúng mệnh giá dưới ngưỡng;
- `/del_code` trên mã **đã phát** bị từ chối, và mã **KHÔNG** đổi trạng thái;
- không có hàng trong `admin_permissions` ⇒ lệnh bị từ chối và có dòng `audit_log`.

⚠️ Giống `test_tanthu.py`: file này KHÔNG dùng fixture `db`/`seeded` của `conftest`.
Handler đọc ghi qua engine **toàn cục** (`db.engine.transaction()`), nên dựng dữ liệu bằng
một engine thứ hai là biến thứ tự commit giữa hai bên thành một cuộc đua.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import codes as admin_codes
from televip.db.engine import session as db_session
from tests.conftest import TEST_DATABASE_URL, _truncate_all, add_codes, make_user

OWNER_ID = 7_000_001
OUTSIDER_ID = 7_000_002

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

#: Mọi lệnh của khối này, cấp cho vai `owner`. Nguồn sự thật là DB — không có hằng nào
#: trong code quyết định ai được chạy gì.
_ALL_COMMANDS = [cmd for cmd, _ in admin_codes.COMMANDS]


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    def __init__(self, *, deliver: bool = True) -> None:
        self.messages: list[tuple[int, str]] = []
        self.deliver = deliver

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append((chat_id, text))
        return len(self.messages) if self.deliver else None

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1][1]

    @property
    def texts(self) -> list[str]:
        return [body for _, body in self.messages]


def make_update(user_id: int) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=None, chat=chat)
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=None,
    )


def make_context(sender: FakeSender, *args: str) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=None,
        args=list(args),
    )


# ── Fixture ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine
    from televip.services import admin as admin_service
    from televip.services import settings_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    # `try` bắt đầu NGAY SAU `init_engine`, không phải ngay trước `yield`. `init_engine()`
    # trả về sớm khi `_engine` khác None, nên một lỗi ở phần dựng dữ liệu dưới đây mà làm
    # lỡ `dispose_engine()` sẽ để lại engine của một event loop ĐÃ ĐÓNG cho mọi test sau
    # dùng chung — trên Windows nó hiện ra thành `'NoneType' object has no attribute
    # 'send'` ở một test hoàn toàn không liên quan, và cả file thành ngẫu nhiên.
    try:
        settings_service.invalidate()
        # `services.admin` giữ vai trò trong RAM 30 giây, và `_role_cache` là biến MODULE
        # nên nó sống xuyên qua các test trong cùng tiến trình. Không xoá ở đây thì một
        # test dùng lại `OWNER_ID` sẽ thấy vai trò do test trước để lại — kể cả sau
        # `TRUNCATE`.
        admin_service.invalidate_role()

        async with db_session() as s:
            await _truncate_all(s)
            await s.execute(text(_GRANT_TYPES_SQL))
            await s.commit()

        # `code.allowed_values` / `code.category_values` do migration 0002 seed và
        # `/add_giffcode` cố ý KHÔNG có default trong code, nên test phải dựng chúng.
        await set_setting(
            "code.allowed_values", [5_000, 10_000, 20_000, 50_000, 88_000, 100_000], "json"
        )
        await set_setting(
            "code.category_values",
            {
                "tanthu": [10_000],
                "moibanbe": [10_000],
                "event": [5_000, 10_000, 20_000, 50_000, 88_000],
                "diemdanh": [10_000, 20_000, 50_000],
                "eventchiase": [10_000],
            },
            "json",
        )

        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()
        admin_service.invalidate_role()


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


async def grant_role(user_id: int, role: str, commands: list[str]) -> None:
    """Cấp quyền theo đúng đường thật: `admin_users` + `admin_permissions`."""
    async with db_session() as s:
        await make_user(s, user_id)
    await run_sql(
        """
        INSERT INTO admin_users (user_id, role, added_by)
             VALUES (:uid, :role, :uid)
        ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL
        """,
        {"uid": user_id, "role": role},
    )
    for command in commands:
        await run_sql(
            """
            INSERT INTO admin_permissions (role, command) VALUES (:role, :cmd)
            ON CONFLICT DO NOTHING
            """,
            {"role": role, "cmd": f"/{command}"},
        )


async def stock_count(code_type: str = "tanthu", status: str = "available") -> int:
    return await scalar(
        "SELECT count(*) FROM codes WHERE code_type = :ct AND status = :st",
        {"ct": code_type, "st": status},
    )


async def code_status(code_value: str) -> str:
    return await scalar("SELECT status FROM codes WHERE code_value = :cv", {"cv": code_value})


@pytest_asyncio.fixture
async def owner(wired):
    await grant_role(OWNER_ID, "owner", _ALL_COMMANDS)
    return OWNER_ID


@pytest_asyncio.fixture
async def owner_redis(owner, redis_clean):
    """`/del_all_code` giữ ĐỀ NGHỊ thu hồi trong Redis, nên nhánh nút cần Redis thật."""
    return owner


# ── 1. Phân quyền: `admin_users` + `admin_permissions`, không gì khác ──


@pytest.mark.asyncio
async def test_khong_co_quyen_thi_im_lang_va_ghi_audit(wired):
    """Người lạ gõ `/tonkho`: không nhận được gì, nhưng `audit_log` có dòng `.denied`.

    Im lặng là chủ ý của `services.admin`: một câu "bạn không có quyền" xác nhận rằng lệnh
    đó có tồn tại, đủ để người ngoài dò ra toàn bộ bề mặt lệnh admin.
    """
    async with db_session() as s:
        await make_user(s, OUTSIDER_ID)

    sender = FakeSender()
    await admin_codes.handle_tonkho(make_update(OUTSIDER_ID), make_context(sender))

    assert sender.messages == [], "không được rò rỉ sự tồn tại của lệnh"
    assert (
        await scalar(
            "SELECT count(*) FROM audit_log WHERE action = '/tonkho.denied' AND actor_id = :uid",
            {"uid": OUTSIDER_ID},
        )
        == 1
    ), "từ chối phải để lại dấu vết (bot cũ không ghi gì)"


@pytest.mark.asyncio
async def test_quyen_bi_thu_hoi_thi_het_quyen(wired):
    """`revoked_at` khác NULL là hết quyền — thu hồi không phải `DELETE`."""
    from televip.services import admin as admin_service

    await grant_role(OWNER_ID, "owner", _ALL_COMMANDS)
    await run_sql(
        "UPDATE admin_users SET revoked_at = now() WHERE user_id = :uid", {"uid": OWNER_ID}
    )
    # Thu hồi bằng SQL thô nên cache vai trò không tự biết; bản thật `admin.revoke()` gọi
    # `invalidate_role()` giúp. Xoá tay ở đây để test đo đúng luật DB, không đo cache.
    admin_service.invalidate_role(OWNER_ID)

    sender = FakeSender()
    await admin_codes.handle_tonkho(make_update(OWNER_ID), make_context(sender))

    assert sender.messages == []
    assert await scalar("SELECT count(*) FROM codes") == 0


# ── 2. /add_giffcode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nap_5_ma_trong_do_2_trung(owner):
    """Mệnh đề chính: 5 mã, 2 trùng ⇒ 3 mới / 2 bỏ qua, kho tăng đúng 3."""
    # Hai mã đã có sẵn trong kho.
    await run_sql(
        """
        INSERT INTO codes (code_value, code_type, value_vnd, status) VALUES
            ('DUP-1', 'tanthu', 10000, 'available'),
            ('DUP-2', 'tanthu', 10000, 'available')
        """
    )
    assert await stock_count() == 2

    sender = FakeSender()
    await admin_codes.handle_add_giffcode(
        make_update(owner),
        make_context(sender, "tanthu", "10k", "NEW-1", "DUP-1", "NEW-2", "DUP-2", "NEW-3"),
    )

    assert "Đã nạp: 3" in sender.last, sender.last
    assert "Bỏ qua (trùng): 2" in sender.last, sender.last
    assert await stock_count() == 5, "kho phải tăng đúng 3 (2 cũ + 3 mới)"

    added = await scalar("SELECT count(*) FROM codes WHERE code_value IN ('NEW-1','NEW-2','NEW-3')")
    assert added == 3

    # Cả lô mang đúng MỘT batch_id, và dòng audit trỏ về lô đó qua `entity_id`.
    batch_id = await scalar("SELECT DISTINCT batch_id FROM codes WHERE code_value LIKE 'NEW-%'")
    assert batch_id is not None
    audit = await scalar(
        "SELECT after FROM audit_log"
        " WHERE action = 'add_giffcode' AND entity_type = 'code_batch' AND entity_id = :bid",
        {"bid": str(batch_id)},
    )
    assert audit["added"] == 3
    assert audit["skipped"] == 2
    assert audit["total_value_vnd"] == 30_000

    # Mã có sẵn từ trước không được kéo vào lô mới.
    assert await scalar("SELECT count(*) FROM codes WHERE batch_id = :bid", {"bid": batch_id}) == 3


@pytest.mark.asyncio
async def test_nap_nhieu_dong_va_trung_trong_chinh_lo(owner):
    """Dán nhiều dòng (PTB tách thành token) và có mã lặp ngay trong lô."""
    sender = FakeSender()
    await admin_codes.handle_add_giffcode(
        make_update(owner),
        make_context(sender, "tanthu", "10.000", "A1", "A2", "A1", "A3", "A2"),
    )

    assert "Đã nạp: 3" in sender.last
    assert "Bỏ qua (trùng): 2" in sender.last
    assert await stock_count() == 3


@pytest.mark.asyncio
async def test_menh_gia_sai_voi_loai_thi_tu_choi(owner):
    """`code.category_values` nói `tanthu` chỉ dùng 10.000đ."""
    sender = FakeSender()
    await admin_codes.handle_add_giffcode(
        make_update(owner), make_context(sender, "tanthu", "88k", "X1")
    )

    assert "không dùng mệnh giá" in sender.last
    assert await stock_count() == 0, "từ chối rồi thì không được chèn gì"


@pytest.mark.asyncio
async def test_loai_code_la_thi_tu_choi(owner):
    sender = FakeSender()
    await admin_codes.handle_add_giffcode(
        make_update(owner), make_context(sender, "xacminh", "10k", "X1")
    )

    assert "Loại code không hợp lệ" in sender.last
    assert await scalar("SELECT count(*) FROM codes") == 0


@pytest.mark.parametrize(
    ("raw", "expect"),
    [("10k", 10_000), ("88K", 88_000), ("10000", 10_000), ("10.000", 10_000), ("10,000", 10_000)],
)
def test_doc_menh_gia(raw: str, expect: int):
    assert admin_codes.parse_value_vnd(raw) == expect


@pytest.mark.parametrize("raw", ["", "abc", "10kk", "-5", "0"])
def test_menh_gia_khong_doc_duoc(raw: str):
    assert admin_codes.parse_value_vnd(raw) is None


# ── 3. /tonkho ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tonkho_ra_dung_so_va_canh_bao_duoi_nguong(owner):
    async with db_session() as s:
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=7, prefix="T")
        await add_codes(s, code_type="event", value_vnd=88_000, count=3, prefix="E")
    # Hai mã tân thủ đã phát: tồn kho phải hụt đi đúng hai.
    await run_sql(
        "UPDATE codes SET status = 'issued' WHERE code_value IN ('T-tanthu-1', 'T-tanthu-2')"
    )
    await set_setting("stock.warn_threshold", 5, "int")

    sender = FakeSender()
    await admin_codes.handle_tonkho(make_update(owner), make_context(sender))
    body = sender.last

    assert "10K: còn 5 · đã phát 2 · 50.000đ" in body, body
    assert "88K: còn 3 · đã phát 0 · 264.000đ" in body, body
    # Ngưỡng 5: `còn 5` KHÔNG dưới ngưỡng, `còn 3` thì có.
    assert "⚠️ 88K" in body
    assert "⚠️ 10K" not in body
    assert "Σ Còn lại: 8 mã · 314.000đ" in body
    assert "1 mệnh giá dưới ngưỡng 5" in body


@pytest.mark.asyncio
async def test_tonkho_doc_khoa_da_seed_khi_khoa_moi_vang(owner):
    """`stock.warn_threshold` chưa seed ⇒ rơi về `alert.low_code_threshold` đã seed."""
    async with db_session() as s:
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=4, prefix="T")
    await set_setting("alert.low_code_threshold", 3, "int")

    assert await admin_codes.warn_threshold() == 3

    sender = FakeSender()
    await admin_codes.handle_tonkho(make_update(owner), make_context(sender))
    assert "⚠️" not in sender.last, "4 mã, ngưỡng 3 — không được cảnh báo"


@pytest.mark.asyncio
async def test_tonkho_rong(owner):
    sender = FakeSender()
    await admin_codes.handle_tonkho(make_update(owner), make_context(sender))
    assert "Kho trống" in sender.last


# ── 4. /codes và /codes used ────────────────────────────────────────


@pytest.mark.asyncio
async def test_codes_liet_ke_ma_chua_dung(owner):
    async with db_session() as s:
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=25, prefix="T")

    sender = FakeSender()
    await admin_codes.handle_codes(make_update(owner), make_context(sender))
    body = sender.last

    assert "Tổng còn lại: 25 mã" in body
    assert body.count("\n1. ") == 1
    assert "20. " in body and "21. " not in body, "đúng 20 dòng"
    assert "T-tanthu-25" in body, "mới nhất phải đứng đầu"


@pytest.mark.asyncio
async def test_codes_used_kem_ai_nhan_khi_nao(owner):
    uid = 8_100_001
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=2, prefix="T")
    await run_sql("UPDATE users SET username = 'nguoinhan' WHERE user_id = :uid", {"uid": uid})
    await run_sql(
        """
        INSERT INTO code_grants
               (grant_key, user_id, grant_type, code_id, value_vnd, state,
                idempotency_key, delivered_at)
        SELECT :gk, :uid, 'tanthu', code_id, 10000, 'delivered', :gk, now()
          FROM codes WHERE code_value = 'T-tanthu-1'
        """,
        {"uid": uid, "gk": f"tanthu:{uid}"},
    )
    await run_sql("UPDATE codes SET status = 'issued' WHERE code_value = 'T-tanthu-1'")

    sender = FakeSender()
    await admin_codes.handle_codes(make_update(owner), make_context(sender, "used"))
    body = sender.last

    assert "Tổng đã phát: 1" in body
    assert "T-tanthu-1" in body
    assert "@nguoinhan" in body
    assert str(uid) in body


# ── 5. /del_code ────────────────────────────────────────────────────


async def make_delivered_grant(user_id: int, code_value: str) -> None:
    await run_sql(
        """
        INSERT INTO code_grants
               (grant_key, user_id, grant_type, code_id, value_vnd, state,
                idempotency_key, delivered_at)
        SELECT :gk, :uid, 'tanthu', code_id, 10000, 'delivered', :gk, now()
          FROM codes WHERE code_value = :cv
        """,
        {"uid": user_id, "cv": code_value, "gk": f"tanthu:{user_id}"},
    )
    await run_sql("UPDATE codes SET status = 'issued' WHERE code_value = :cv", {"cv": code_value})


@pytest.mark.asyncio
async def test_del_code_tu_choi_ma_da_phat_va_khong_doi_trang_thai(owner):
    """Mệnh đề chính: mã đã phát ⇒ từ chối, nói rõ ai giữ, mã KHÔNG đổi trạng thái."""
    uid = 8_200_001
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=1, prefix="T")
    await run_sql("UPDATE users SET username = 'chunhan' WHERE user_id = :uid", {"uid": uid})
    await make_delivered_grant(uid, "T-tanthu-1")

    sender = FakeSender()
    await admin_codes.handle_del_code(make_update(owner), make_context(sender, "T-tanthu-1"))
    body = sender.last

    assert "Code đã phát, không xoá được" in body, body
    assert "@chunhan" in body, "phải nói rõ ai đang giữ"
    assert str(uid) in body
    assert await code_status("T-tanthu-1") == "issued", "trạng thái mã KHÔNG được đổi"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_code'") == 0, (
        "từ chối thì không ghi audit thu hồi"
    )


@pytest.mark.asyncio
async def test_del_code_thu_hoi_ma_chua_phat(owner):
    async with db_session() as s:
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=2, prefix="T")

    sender = FakeSender()
    await admin_codes.handle_del_code(make_update(owner), make_context(sender, "T-tanthu-1"))

    assert "Đã xóa code: T-tanthu-1" in sender.last
    assert await code_status("T-tanthu-1") == "revoked", "chuyển revoked, KHÔNG xoá dòng"
    assert await scalar("SELECT count(*) FROM codes") == 2, "hàng dữ liệu vẫn còn"
    assert await stock_count() == 1

    audit = await scalar(
        "SELECT after FROM audit_log WHERE action = 'del_code' ORDER BY log_id DESC LIMIT 1"
    )
    assert audit["status"] == "revoked"


@pytest.mark.asyncio
async def test_del_code_khong_tim_thay(owner):
    sender = FakeSender()
    await admin_codes.handle_del_code(make_update(owner), make_context(sender, "KHONG-CO"))
    assert "Không tìm thấy code: KHONG-CO" in sender.last


@pytest.mark.asyncio
async def test_del_code_lan_hai_bao_da_thu_hoi(owner):
    async with db_session() as s:
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=1, prefix="T")

    sender = FakeSender()
    update, context = make_update(owner), make_context(sender, "T-tanthu-1")
    await admin_codes.handle_del_code(update, context)
    await admin_codes.handle_del_code(update, context)

    assert "đã bị thu hồi trước đó" in sender.last
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_code'") == 1


# ── 6. /resend_tanthu ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resend_gui_lai_dung_ma_cu_khong_cap_ma_moi(owner):
    uid = 8_300_001
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=3, prefix="T")
    await make_delivered_grant(uid, "T-tanthu-1")

    sender = FakeSender()
    await admin_codes.handle_resend_tanthu(make_update(owner), make_context(sender, str(uid)))

    # Tin đầu gửi cho người nhận, tin sau xác nhận với admin.
    assert sender.messages[0][0] == uid
    assert "T-tanthu-1" in sender.messages[0][1]
    assert "Đã gửi lại mã tân thủ" in sender.last

    assert (
        await scalar("SELECT count(*) FROM code_grants WHERE user_id = :uid", {"uid": uid}) == 1
    ), "KHÔNG được sinh grant thứ hai"
    assert await stock_count() == 2, "kho không được hụt thêm mã nào"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'resend_tanthu'") == 1


@pytest.mark.asyncio
async def test_resend_tim_theo_username(owner):
    uid = 8_300_002
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=1, prefix="T")
    await run_sql("UPDATE users SET username = 'NguoiDung' WHERE user_id = :uid", {"uid": uid})
    await make_delivered_grant(uid, "T-tanthu-1")

    sender = FakeSender()
    # Gõ khác hoa thường: Telegram không phân biệt, ta cũng không được phân biệt.
    await admin_codes.handle_resend_tanthu(make_update(owner), make_context(sender, "@nguoidung"))

    assert sender.messages[0][0] == uid
    assert "Đã gửi lại mã tân thủ" in sender.last


@pytest.mark.asyncio
async def test_resend_khong_co_grant_thi_noi_ro_va_khong_cap_moi(owner):
    uid = 8_300_003
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=5, prefix="T")

    sender = FakeSender()
    await admin_codes.handle_resend_tanthu(make_update(owner), make_context(sender, str(uid)))

    assert "chưa từng được cấp code tân thủ" in sender.last
    assert await scalar("SELECT count(*) FROM code_grants") == 0
    assert await stock_count() == 5, "không được đụng vào kho"


@pytest.mark.asyncio
async def test_resend_grant_chua_gan_ma(owner):
    """Grant sinh ra lúc kho rỗng: có suất nhưng chưa có mã ⇒ không cấp bù ở đây."""
    uid = 8_300_004
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=2, prefix="T")
    await run_sql(
        """
        INSERT INTO code_grants
               (grant_key, user_id, grant_type, value_vnd, state, idempotency_key)
        VALUES (:gk, :uid, 'tanthu', 10000, 'reserved', :gk)
        """,
        {"uid": uid, "gk": f"tanthu:{uid}"},
    )

    sender = FakeSender()
    await admin_codes.handle_resend_tanthu(make_update(owner), make_context(sender, str(uid)))

    assert "CHƯA gắn được mã" in sender.last
    assert await stock_count() == 2


@pytest.mark.asyncio
async def test_resend_nguoi_da_chan_bot(owner):
    uid = 8_300_005
    async with db_session() as s:
        await make_user(s, uid)
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=1, prefix="T")
    await make_delivered_grant(uid, "T-tanthu-1")

    sender = FakeSender(deliver=False)
    await admin_codes.handle_resend_tanthu(make_update(owner), make_context(sender, str(uid)))

    assert "Không gửi được" in sender.last
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'resend_tanthu'") == 0, (
        "gửi hỏng thì không ghi là đã gửi lại"
    )


@pytest.mark.asyncio
async def test_resend_khong_tim_thay_nguoi_dung(owner):
    sender = FakeSender()
    await admin_codes.handle_resend_tanthu(make_update(owner), make_context(sender, "@aokhongco"))
    assert "Không tìm thấy người dùng" in sender.last


# ── 6. /del_all_code — thu hồi hàng loạt, vé dùng một lần ───────────
#
# Nút xác nhận của lệnh này KHÔNG mang theo phạm vi. Bản đầu tiên có mang, và đó là lỗ
# hổng nặng nhất bộ soát tìm được: tin nhắn Telegram sống mãi trong lịch sử chat, nên cái
# nút là một MỆNH LỆNH VĨNH VIỄN chứ không phải một quyết định — bấm lại vài ngày sau sẽ
# chạy UPDATE trên kho của hôm nay, và đi vòng qua cả trần duyệt hai người.


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data


def make_callback_update(user_id: int, data: str) -> Any:
    update = make_update(user_id)
    update.callback_query = FakeQuery(data)
    return update


class FakeSenderCB(FakeSender):
    """`FakeSender` + `answer_callback` + ghi lại bàn phím.

    Bàn phím phải ghi lại được vì bài kiểm lấy `callback_data` từ CHÍNH cái nút bot gửi
    ra, không tự dựng chuỗi. Tự dựng thì bài kiểm vẫn xanh sau khi định dạng
    `callback_data` đổi mà nút thật đã hỏng.
    """

    def __init__(self, *, deliver: bool = True) -> None:
        super().__init__(deliver=deliver)
        self.answers: list[str] = []
        self.markups: list[Any] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        markup = kwargs.get("reply_markup")
        if markup is not None:
            self.markups.append(markup)
        return await super().send_message(chat_id, text, **kwargs)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


async def _du_kho(*, code_type: str, value_vnd: int, count: int, prefix: str) -> None:
    async with db_session() as s:
        await add_codes(s, code_type=code_type, value_vnd=value_vnd, count=count, prefix=prefix)


async def _de_nghi(actor: int, *args: str) -> tuple[FakeSenderCB, str | None]:
    """Gõ lệnh, trả về (sender, callback_data của nút XOÁ NGAY)."""
    sender = FakeSenderCB()
    await admin_codes.handle_del_all_code(make_update(actor), make_context(sender, *args))
    markup = sender.markups[-1] if sender.markups else None
    if markup is None:
        return sender, None
    return sender, markup.inline_keyboard[0][0].callback_data


async def _bam(actor: int, data: str) -> FakeSenderCB:
    sender = FakeSenderCB()
    await admin_codes.handle_del_all_callback(
        make_callback_update(actor, data), make_context(sender)
    )
    return sender


@pytest.mark.asyncio
async def test_del_all_code_go_lenh_khong_xoa_gi_ca(owner_redis):
    """Bước một chỉ ĐẾM. Một lệnh chạm được cả kho thì bước xác nhận là hàng rào chính."""
    await _du_kho(code_type="event", value_vnd=5_000, count=7, prefix="E5")

    sender, data = await _de_nghi(owner_redis, "event")

    assert "XÁC NHẬN" in sender.last
    assert "7" in sender.last
    assert data is not None and data.startswith("dac_ok_")
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 7, (
        "gõ lệnh mà đã xoá — bước xác nhận là vô nghĩa"
    )


@pytest.mark.asyncio
async def test_del_all_code_xac_nhan_thi_thu_hoi_va_ghi_so(owner_redis):
    await _du_kho(code_type="event", value_vnd=5_000, count=4, prefix="E5")
    _, data = await _de_nghi(owner_redis, "event")

    sender = await _bam(owner_redis, data)

    assert sender.answers, "callback không được trả lời — nút sẽ quay vòng"
    assert "Đã thu hồi 4 mã" in sender.last
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'revoked'") == 4
    assert await scalar("SELECT count(*) FROM codes") == 4, "phải là revoke, KHÔNG phải DELETE"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_all_code'") == 1


@pytest.mark.asyncio
async def test_del_all_code_khong_dung_toi_ma_da_phat(owner_redis):
    """Hàng rào đắt nhất của lệnh: mã đang thuộc về một người thật phải sống sót."""
    await _du_kho(code_type="event", value_vnd=5_000, count=3, prefix="E5")
    await run_sql("UPDATE codes SET status = 'issued' WHERE code_value = 'E5-event-1'")
    await run_sql("UPDATE codes SET status = 'reserved' WHERE code_value = 'E5-event-2'")
    _, data = await _de_nghi(owner_redis, "event")

    await _bam(owner_redis, data)

    assert await code_status("E5-event-1") == "issued", "mã ĐÃ PHÁT bị đụng tới"
    assert await code_status("E5-event-2") == "reserved", "mã đang giữ chỗ bị đụng tới"
    assert await code_status("E5-event-3") == "revoked"


@pytest.mark.asyncio
async def test_del_all_code_loc_dung_menh_gia(owner_redis):
    await _du_kho(code_type="event", value_vnd=5_000, count=2, prefix="E5")
    await _du_kho(code_type="event", value_vnd=88_000, count=3, prefix="E88")
    _, data = await _de_nghi(owner_redis, "event", "88k")

    await _bam(owner_redis, data)

    assert await scalar("SELECT count(*) FROM codes WHERE status = 'revoked'") == 3
    assert await code_status("E5-event-1") == "available", "mệnh giá khác bị cuốn theo"


@pytest.mark.asyncio
async def test_del_all_code_ve_chi_bam_duoc_MOT_lan(owner_redis):
    """Bấm lại đúng cái nút đó không được chạy `UPDATE` lần thứ hai."""
    await _du_kho(code_type="event", value_vnd=5_000, count=2, prefix="E5")
    _, data = await _de_nghi(owner_redis, "event")

    await _bam(owner_redis, data)
    await _du_kho(code_type="event", value_vnd=5_000, count=5, prefix="MOI")
    sender = await _bam(owner_redis, data)

    assert "hết hạn hoặc đã được bấm rồi" in sender.last
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 5, (
        "nút cũ vẫn xoá được kho MỚI — đây là lỗ hổng nặng nhất bộ soát tìm ra"
    )


@pytest.mark.asyncio
async def test_del_all_code_nut_cu_khong_di_vong_qua_tran_duyet_hai_nguoi(owner_redis):
    """Kịch bản bộ soát dựng lại được, nguyên văn.

    Kho 1 mã 5.000đ ⇒ đề nghị được chấp nhận. Hôm sau nạp 200 mã 88.000đ ⇒ gõ tay bị từ
    chối vì vượt trần. Cuộn lên bấm nút cũ thì bản trước thu hồi sạch 17.605.000đ.
    """
    await set_setting("admin.dual_approval_threshold_vnd", 1_000_000, "money_vnd")
    await _du_kho(code_type="event", value_vnd=5_000, count=1, prefix="NHO")
    _, data = await _de_nghi(owner_redis, "event")
    assert data is not None

    await _du_kho(code_type="event", value_vnd=88_000, count=200, prefix="TO")

    # Gõ tay: bị từ chối.
    sender_go, _ = await _de_nghi(owner_redis, "event")
    assert "vượt ngưỡng" in sender_go.last

    # Bấm nút cũ: cũng phải bị từ chối, và không mã nào bị đụng tới.
    sender = await _bam(owner_redis, data)
    assert "TỪ CHỐI" in sender.last
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'revoked'") == 0
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 201


@pytest.mark.asyncio
async def test_del_all_code_khong_bam_duoc_nut_cua_admin_khac(owner_redis):
    """Và bấm nhầm cũng không được ĐỐT MẤT đề nghị của người ta."""
    await _du_kho(code_type="event", value_vnd=5_000, count=3, prefix="E5")
    _, data = await _de_nghi(owner_redis, "event")

    nguoi_khac = OWNER_ID + 77
    await grant_role(nguoi_khac, "owner", _ALL_COMMANDS)
    sender = await _bam(nguoi_khac, data)

    assert "admin khác" in sender.last
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 3

    # Vé vẫn còn nguyên: chủ của nó bấm được bình thường.
    sender_chu = await _bam(owner_redis, data)
    assert "Đã thu hồi 3 mã" in sender_chu.last


@pytest.mark.asyncio
async def test_del_all_code_huy_thi_khong_dung_gi(owner_redis):
    await _du_kho(code_type="event", value_vnd=5_000, count=3, prefix="E5")
    sender_go, _ = await _de_nghi(owner_redis, "event")
    huy = sender_go.markups[-1].inline_keyboard[0][1].callback_data

    sender = await _bam(owner_redis, huy)

    assert "Đã huỷ" in sender.last
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 3


@pytest.mark.asyncio
async def test_del_all_code_tu_choi_loai_khong_hop_le(owner_redis):
    sender, data = await _de_nghi(owner_redis, "khonco")
    assert "không hợp lệ" in sender.last
    assert data is None


@pytest.mark.asyncio
async def test_del_all_code_tu_choi_khi_vuot_nguong_duyet_hai_nguoi(owner_redis):
    """Cùng hàng rào với `/add_giffcode`: luồng ký thứ hai chưa xây nên fail-closed."""
    await set_setting("admin.dual_approval_threshold_vnd", 100_000, "money_vnd")
    await _du_kho(code_type="event", value_vnd=88_000, count=5, prefix="E88")

    sender, data = await _de_nghi(owner_redis, "event")

    assert "vượt ngưỡng" in sender.last
    assert data is None, "vượt trần mà vẫn phát ra một cái nút bấm được"
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 5


@pytest.mark.asyncio
async def test_del_all_code_nguoi_khong_co_quyen_van_duoc_TRA_LOI_nut(wired, redis_clean):
    """Từ chối phải IM LẶNG về nội dung nhưng vẫn `answer_callback`.

    `@admin_command` trả về im lặng, nên đặt nó ở ngoài cùng khiến nút quay vòng ~15 giây
    rồi báo lỗi mạng — đúng hành vi §13.3.3 cấm, và là lỗi `/broadcast` đã phải sửa.
    """
    async with db_session() as s:
        await make_user(s, OUTSIDER_ID)
    await _du_kho(code_type="event", value_vnd=5_000, count=3, prefix="E5")

    sender = await _bam(OUTSIDER_ID, f"dac_ok_{OUTSIDER_ID}_abcdefgh")

    assert sender.answers == [""], "callback không được trả lời — nút quay vòng"
    assert sender.messages == [], "không được rò rỉ sự tồn tại của lệnh"
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 3


@pytest.mark.asyncio
async def test_del_all_code_bam_HUY_roi_bam_XOA_thi_duoc_giai_thich(owner_redis):
    """Huỷ cũng ĐỐT vé — hai nút vẫn nằm đó nên phải nói ra, kèm bước kế tiếp.

    Không nói thì admin bấm nhầm HUỶ rồi bấm XOÁ NGAY ngay bên cạnh sẽ nhận "đề nghị đã
    hết hạn" và không hiểu vì sao: cái nút trông vẫn còn dùng được.
    """
    await _du_kho(code_type="event", value_vnd=5_000, count=3, prefix="E5")
    sender_go, ok = await _de_nghi(owner_redis, "event")
    huy = sender_go.markups[-1].inline_keyboard[0][1].callback_data

    s_huy = await _bam(owner_redis, huy)
    assert "không còn tác dụng" in s_huy.last
    assert "/del_all_code event" in s_huy.last, "phải chỉ đúng lệnh gõ lại, kèm phạm vi"

    s_sau = await _bam(owner_redis, ok)
    assert "hết hạn hoặc đã được bấm rồi" in s_sau.last
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 3


@pytest.mark.asyncio
async def test_del_all_code_tu_choi_luc_bam_thi_chi_ro_buoc_ke_tiep(owner_redis):
    """Từ chối mà không chỉ đường ra thì admin kẹt: vé đã bị đốt, nút cũ vô dụng."""
    await set_setting("admin.dual_approval_threshold_vnd", 1_000_000, "money_vnd")
    await _du_kho(code_type="event", value_vnd=5_000, count=1, prefix="NHO")
    _, data = await _de_nghi(owner_redis, "event")
    await _du_kho(code_type="event", value_vnd=88_000, count=200, prefix="TO")

    sender = await _bam(owner_redis, data)

    assert "TỪ CHỐI" in sender.last
    assert "lọc mệnh giá" in sender.last, "không chỉ cách thu hẹp thì admin không có đường ra"
    assert "không còn tác dụng" in sender.last, "vé đã bị đốt mà nút vẫn nằm đó"

    # ...và lối ra được chỉ phải THẬT SỰ chạy được.
    sender_hep, data_hep = await _de_nghi(owner_redis, "event", "5k")
    assert data_hep is not None, "lối ra được chỉ lại bị từ chối tiếp"
    await _bam(owner_redis, data_hep)
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'revoked'") == 1
    assert await scalar("SELECT count(*) FROM codes WHERE status = 'available'") == 200
