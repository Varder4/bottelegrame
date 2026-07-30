"""Lệnh vận hành và cấu hình nóng — chạy **qua chính handler**, trên database thật.

Chỉ Telegram bị thay bằng bản giả; phân quyền, `settings`, `settings_audit`, `audit_log`
đều là bảng thật. Ba mệnh đề trọng tâm (mỗi cái là một chỗ hệ cũ đã trả giá):

- `/setcauhinh` đổi đúng giá trị **và** để lại dấu vết ở `settings_audit`. Hệ cũ đổi cấu
  hình bằng `nano config.py` trên VPS, nên không ai trả lời được "ai đổi, lúc nào".
- `/setcauhinh` gõ sai kiểu thì **từ chối và giá trị KHÔNG đổi**. Một `money_vnd` nhận
  được chuỗi là mọi lượt đọc sau đó ném lỗi giữa đường nóng.
- `/help_admin` của `cskh` **không** liệt kê lệnh của `owner`. Danh sách phải sinh từ
  `admin_permissions`, không phải từ một mảng gõ tay.

⚠️ Cùng luật với `test_tanthu.py`: KHÔNG dùng fixture `db`/`seeded` của conftest. Handler
đọc ghi qua engine **toàn cục**, nên test dựng dữ liệu bằng engine thứ hai sẽ biến thứ tự
commit giữa hai bên thành một cuộc đua. Ở đây chỉ có một engine.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import ops
from televip.db.engine import session as db_session
from tests.conftest import TEST_DATABASE_URL, _truncate_all

OWNER_ID = 900_001
CSKH_ID = 900_002
OUTSIDER_ID = 900_003
TARGET_ID = 700_001


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return 1000 + len(self.messages)

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
        bot=SimpleNamespace(),
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
    settings_service.invalidate()
    admin_service.invalidate_role()

    async with db_session() as s:
        await _truncate_all(s)
        await s.commit()

    try:
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
        return (await s.execute(text(sql), params or {})).scalar_one()


async def make_user(user_id: int, *, username: str | None = None) -> int:
    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:uid, :un) ON CONFLICT DO NOTHING",
        {"uid": user_id, "un": username or f"user{user_id}"},
    )
    return user_id


async def make_admin(user_id: int, role: str, commands: tuple[str, ...]) -> None:
    """Một admin thật + quyền của vai trò đó, đúng như migration seed sẽ làm."""
    from televip.services import admin as admin_service

    await make_user(user_id)
    await run_sql(
        """
        INSERT INTO admin_users (user_id, role, added_by) VALUES (:uid, :role, :uid)
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
            {"role": role, "cmd": command},
        )
    # Cache vai trò sống 30 giây trong tiến trình; test vừa ghi thẳng vào bảng nên phải
    # xoá tay, nếu không lệnh kế tiếp còn đọc "không phải admin" của lượt trước.
    admin_service.invalidate_role(user_id)


async def put_setting(
    key: str,
    value: Any,
    value_type: str,
    *,
    label: str = "Nhan thu",
    sensitive: bool = False,
    min_value: int | None = None,
    max_value: int | None = None,
) -> None:
    from televip.services import settings_service

    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi, sensitive, min_value, max_value)
             VALUES (:k, CAST(:v AS jsonb), :t, :label, :sens, :mn, :mx)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {
            "k": key,
            "v": json.dumps(value),
            "t": value_type,
            "label": label,
            "sens": sensitive,
            "mn": min_value,
            "mx": max_value,
        },
    )
    settings_service.invalidate()


async def setting_value(key: str) -> Any:
    return await scalar("SELECT value FROM settings WHERE key = :k", {"k": key})


async def audit_count(key: str) -> int:
    return await scalar("SELECT count(*) FROM settings_audit WHERE key = :k", {"k": key})


# ── /setcauhinh — đường thành công ──────────────────────────────────


@pytest.mark.asyncio
async def test_setcauhinh_doi_gia_tri_va_ghi_settings_audit(wired):
    await make_admin(OWNER_ID, "owner", ("/setcauhinh",))
    await put_setting("referral.interval", 5, "int", label="Moc moi ban")

    sender = FakeSender()
    await ops.cmd_setcauhinh(make_update(OWNER_ID), make_context(sender, "referral.interval", "8"))

    assert await setting_value("referral.interval") == 8
    assert await audit_count("referral.interval") == 1

    row = await scalar(
        """
        SELECT json_build_object('old', old_value, 'new', new_value, 'by', changed_by)::text
          FROM settings_audit WHERE key = 'referral.interval'
        """
    )
    entry = json.loads(row)
    assert entry["old"] == 5
    assert entry["new"] == 8
    assert entry["by"] == OWNER_ID

    # Câu trả lời phải cho admin thấy mình vừa đổi gì, và nhắc độ trễ cache.
    assert "5" in sender.last and "8" in sender.last
    assert "60 giây" in sender.last

    # Mỗi lệnh đổi trạng thái ghi `audit_log` (§13.4.1).
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'setcauhinh'") == 1


@pytest.mark.asyncio
async def test_setcauhinh_nhan_dinh_dang_tien_kieu_viet(wired):
    """`10.000` là cách admin gõ lại đúng con số `/cauhinh` vừa in ra."""
    await make_admin(OWNER_ID, "owner", ("/setcauhinh",))
    await put_setting("code.tanthu_value_vnd", 10_000, "money_vnd")

    sender = FakeSender()
    await ops.cmd_setcauhinh(
        make_update(OWNER_ID), make_context(sender, "code.tanthu_value_vnd", "12.000.000")
    )

    assert await setting_value("code.tanthu_value_vnd") == 12_000_000


# ── /setcauhinh — sai kiểu thì giá trị KHÔNG đổi ────────────────────


@pytest.mark.parametrize(
    ("value_type", "seed", "bad_input"),
    [
        ("int", 5, "nam"),
        ("money_vnd", 10_000, "10k"),
        ("bool", True, "co-le"),
        ("seconds", 60, "1.5"),
        ("json", [10_000], "khong-phai-json"),
    ],
)
@pytest.mark.asyncio
async def test_setcauhinh_sai_kieu_thi_tu_choi_va_khong_doi(wired, value_type, seed, bad_input):
    await make_admin(OWNER_ID, "owner", ("/setcauhinh",))
    await put_setting("khoa.thu", seed, value_type)

    sender = FakeSender()
    await ops.cmd_setcauhinh(make_update(OWNER_ID), make_context(sender, "khoa.thu", bad_input))

    assert await setting_value("khoa.thu") == seed, "giá trị KHÔNG được đổi khi gõ sai kiểu"
    assert await audit_count("khoa.thu") == 0, "sai kiểu thì không có gì để ghi vào sổ"
    assert value_type in sender.last, "thông báo phải nói rõ kiểu mong đợi"


@pytest.mark.asyncio
async def test_setcauhinh_ngoai_khoang_min_max_thi_tu_choi(wired):
    """`min_value`/`max_value` là hàng rào chống gõ nhầm — nó phải chặn thật."""
    await make_admin(OWNER_ID, "owner", ("/setcauhinh",))
    await put_setting("khoa.gioihan", 30, "int", min_value=1, max_value=100)

    sender = FakeSender()
    await ops.cmd_setcauhinh(make_update(OWNER_ID), make_context(sender, "khoa.gioihan", "999"))

    assert await setting_value("khoa.gioihan") == 30
    assert await audit_count("khoa.gioihan") == 0


@pytest.mark.asyncio
async def test_setcauhinh_khoa_khong_ton_tai(wired):
    await make_admin(OWNER_ID, "owner", ("/setcauhinh",))

    sender = FakeSender()
    await ops.cmd_setcauhinh(make_update(OWNER_ID), make_context(sender, "khoa.bay.dat", "1"))

    assert "khoa.bay.dat" in sender.last
    assert await scalar("SELECT count(*) FROM settings_audit") == 0


@pytest.mark.asyncio
async def test_setcauhinh_khoa_sensitive_bi_tu_choi(wired):
    """Khoá `sensitive` cần duyệt hai người (§13.4.1) — chưa có luồng đó thì fail closed."""
    await make_admin(OWNER_ID, "owner", ("/setcauhinh",))
    await put_setting("risk.block_threshold", 80, "int", sensitive=True)

    sender = FakeSender()
    await ops.cmd_setcauhinh(
        make_update(OWNER_ID), make_context(sender, "risk.block_threshold", "10")
    )

    assert await setting_value("risk.block_threshold") == 80
    assert await audit_count("risk.block_threshold") == 0


@pytest.mark.asyncio
async def test_setcauhinh_khong_co_quyen_thi_khong_doi_duoc(wired):
    """Người ngoài gõ lệnh: giá trị không đổi, và có dấu vết ở `audit_log`."""
    await make_admin(CSKH_ID, "cskh", ("/help_admin",))
    await make_user(OUTSIDER_ID)
    await put_setting("referral.interval", 5, "int")

    sender = FakeSender()
    await ops.cmd_setcauhinh(
        make_update(OUTSIDER_ID), make_context(sender, "referral.interval", "99")
    )

    assert await setting_value("referral.interval") == 5
    assert await audit_count("referral.interval") == 0
    denied = await scalar(
        "SELECT count(*) FROM audit_log WHERE action LIKE '%denied%' AND actor_id = :uid",
        {"uid": OUTSIDER_ID},
    )
    assert denied == 1, "từ chối quyền phải để lại dấu vết (bot cũ im lặng)"


# ── /help_admin ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_admin_cua_cskh_khong_liet_ke_lenh_cua_owner(wired):
    await make_admin(OWNER_ID, "owner", ("/help_admin", "/setcauhinh", "/ban", "/admin_add"))
    await make_admin(CSKH_ID, "cskh", ("/help_admin", "/user", "/stats"))

    sender = FakeSender()
    await ops.cmd_help_admin(make_update(CSKH_ID), make_context(sender))
    body = sender.all_text

    assert "/user" in body
    assert "/stats" in body
    assert "cskh" in body
    for owner_only in ("/setcauhinh", "/ban", "/admin_add"):
        assert owner_only not in body, f"{owner_only} là lệnh của owner, cskh không được thấy"


@pytest.mark.asyncio
async def test_help_admin_cua_owner_liet_ke_lenh_cua_owner(wired):
    """Mặt còn lại của cùng một mệnh đề: danh sách đọc từ bảng, không phải lọc cứng."""
    await make_admin(OWNER_ID, "owner", ("/help_admin", "/setcauhinh", "/ban"))

    sender = FakeSender()
    await ops.cmd_help_admin(make_update(OWNER_ID), make_context(sender))

    assert "/setcauhinh" in sender.all_text
    assert "/ban" in sender.all_text


@pytest.mark.asyncio
async def test_help_admin_theo_dung_quyen_vua_doi_trong_bang(wired):
    """Đổi `admin_permissions` là đổi ngay bản in ra — không phải deploy lại."""
    await make_admin(CSKH_ID, "cskh", ("/help_admin",))

    sender = FakeSender()
    await ops.cmd_help_admin(make_update(CSKH_ID), make_context(sender))
    assert "/done_event" not in sender.all_text

    await run_sql("INSERT INTO admin_permissions (role, command) VALUES ('cskh', '/done_event')")
    sender2 = FakeSender()
    await ops.cmd_help_admin(make_update(CSKH_ID), make_context(sender2))
    assert "/done_event" in sender2.all_text


# ── /cauhinh ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cauhinh_loc_theo_tien_to(wired):
    await make_admin(OWNER_ID, "owner", ("/cauhinh",))
    await put_setting("event.window_minutes", 10, "int")
    await put_setting("event.delete_hours", 24, "int")
    await put_setting("referral.interval", 5, "int")

    sender = FakeSender()
    await ops.cmd_cauhinh(make_update(OWNER_ID), make_context(sender, "event."))
    body = sender.all_text

    assert "event.window_minutes" in body
    assert "event.delete_hours" in body
    assert "referral.interval" not in body


@pytest.mark.asyncio
async def test_cauhinh_dinh_dang_tien_kieu_viet(wired):
    await make_admin(OWNER_ID, "owner", ("/cauhinh",))
    await put_setting("code.tanthu_value_vnd", 10_000, "money_vnd")

    sender = FakeSender()
    await ops.cmd_cauhinh(make_update(OWNER_ID), make_context(sender, "code."))

    assert "10.000đ" in sender.all_text


@pytest.mark.asyncio
async def test_cauhinh_che_khoa_bi_mat(wired):
    """Một khoá tên `*.token` không bao giờ được hiện nguyên văn trong tin nhắn."""
    await make_admin(OWNER_ID, "owner", ("/cauhinh",))
    await put_setting("integration.api_token", "sieu-mat", "string")

    sender = FakeSender()
    await ops.cmd_cauhinh(make_update(OWNER_ID), make_context(sender, "integration."))

    assert "sieu-mat" not in sender.all_text
    assert ops.MASKED in sender.all_text


# ── /stats ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_tinh_truc_tiep_khi_system_stats_rong(wired):
    await make_admin(OWNER_ID, "owner", ("/stats",))
    await make_user(TARGET_ID)
    await run_sql("UPDATE users SET verified_at = now() WHERE user_id = :uid", {"uid": TARGET_ID})
    await run_sql(
        """
        INSERT INTO codes (code_value, code_type, value_vnd, status)
             VALUES ('S-1', 'tanthu', 10000, 'available'),
                    ('S-2', 'tanthu', 10000, 'available')
        """
    )

    sender = FakeSender()
    await ops.cmd_stats(make_update(OWNER_ID), make_context(sender))
    body = sender.last

    assert "TRỰC TIẾP" in body, "phải ghi rõ là tính trực tiếp"
    assert "Code khả dụng: 2" in body


@pytest.mark.asyncio
async def test_stats_doc_system_stats_khi_co_du_lieu(wired):
    await make_admin(OWNER_ID, "owner", ("/stats",))
    await run_sql(
        """
        INSERT INTO system_stats
               (id, total_users, codes_available, codes_issued, total_referrals, total_value_vnd)
        VALUES (1, 19151, 1769, 3412, 4020, 147990000)
        """
    )

    sender = FakeSender()
    await ops.cmd_stats(make_update(OWNER_ID), make_context(sender))
    body = sender.last

    assert "system_stats" in body
    assert "19.151" in body
    assert "147.990.000đ" in body


# ── /user ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_tra_theo_username_va_theo_id(wired):
    await make_admin(CSKH_ID, "cskh", ("/user",))
    await make_user(TARGET_ID, username="NguoiDung")

    for token in (str(TARGET_ID), "@nguoidung", "@NGUOIDUNG"):
        sender = FakeSender()
        await ops.cmd_user(make_update(CSKH_ID), make_context(sender, token))
        assert str(TARGET_ID) in sender.last, f"tra bằng {token!r} phải ra hồ sơ"


@pytest.mark.asyncio
async def test_user_hien_ban_va_ma_da_nhan(wired):
    await make_admin(OWNER_ID, "owner", ("/user", "/ban"))
    await make_user(TARGET_ID)
    await run_sql(
        """
        INSERT INTO grant_types (code, label_vi, once_per_life)
             VALUES ('tanthu', 'Code tan thu', true) ON CONFLICT DO NOTHING
        """
    )
    await run_sql(
        """
        INSERT INTO code_grants (grant_key, user_id, grant_type, value_vnd, state, idempotency_key)
             VALUES ('tanthu:1', :uid, 'tanthu', 10000, 'delivered', 'idem-1')
        """,
        {"uid": TARGET_ID},
    )
    await run_sql(
        "INSERT INTO user_bans (user_id, reason, banned_by) VALUES (:uid, 'gian lan', :by)",
        {"uid": TARGET_ID, "by": OWNER_ID},
    )

    sender = FakeSender()
    await ops.cmd_user(make_update(OWNER_ID), make_context(sender, str(TARGET_ID)))
    body = sender.last

    assert "ĐANG BỊ KHOÁ" in body
    assert "gian lan" in body
    assert "tanthu" in body
    assert "10.000đ" in body


@pytest.mark.asyncio
async def test_user_khong_tim_thay(wired):
    await make_admin(CSKH_ID, "cskh", ("/user",))

    sender = FakeSender()
    await ops.cmd_user(make_update(CSKH_ID), make_context(sender, "@khong-ai"))

    assert "Không tìm thấy" in sender.last


# ── /ban · /unban ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ban_ghi_user_bans_va_khong_xoa_users(wired):
    await make_admin(OWNER_ID, "owner", ("/ban",))
    await make_user(TARGET_ID)

    sender = FakeSender()
    await ops.cmd_ban(make_update(OWNER_ID), make_context(sender, str(TARGET_ID), "gian", "lan"))

    assert await scalar("SELECT count(*) FROM users WHERE user_id = :uid", {"uid": TARGET_ID}) == 1
    reason = await scalar(
        "SELECT reason FROM user_bans WHERE user_id = :uid AND unbanned_at IS NULL",
        {"uid": TARGET_ID},
    )
    assert reason == "gian lan"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'ban'") == 1


@pytest.mark.asyncio
async def test_ban_khong_co_ly_do_van_chay(wired):
    """`user_bans.reason` là NOT NULL, nên lệnh thiếu lý do phải có mặc định."""
    await make_admin(OWNER_ID, "owner", ("/ban",))
    await make_user(TARGET_ID)

    sender = FakeSender()
    await ops.cmd_ban(make_update(OWNER_ID), make_context(sender, str(TARGET_ID)))

    reason = await scalar("SELECT reason FROM user_bans WHERE user_id = :uid", {"uid": TARGET_ID})
    assert reason == ops.DEFAULT_BAN_REASON


@pytest.mark.asyncio
async def test_unban_dien_unbanned_at_chu_khong_xoa_dong(wired):
    await make_admin(OWNER_ID, "owner", ("/ban", "/unban"))
    await make_user(TARGET_ID)
    await ops.cmd_ban(make_update(OWNER_ID), make_context(FakeSender(), str(TARGET_ID), "thu"))

    sender = FakeSender()
    await ops.cmd_unban(make_update(OWNER_ID), make_context(sender, str(TARGET_ID)))

    row = await scalar(
        "SELECT (unbanned_at IS NOT NULL) FROM user_bans WHERE user_id = :uid",
        {"uid": TARGET_ID},
    )
    assert row is True, "gỡ ban là điền unbanned_at, dòng vẫn còn để tra cứu"


@pytest.mark.asyncio
async def test_ban_user_khong_ton_tai(wired):
    await make_admin(OWNER_ID, "owner", ("/ban",))

    sender = FakeSender()
    await ops.cmd_ban(make_update(OWNER_ID), make_context(sender, "555000111"))

    assert await scalar("SELECT count(*) FROM user_bans") == 0
    assert "chưa từng /start" in sender.last


# ── /admin_add · /admin_del ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_add_va_admin_del(wired):
    from televip.services import admin as admin_service

    await make_admin(OWNER_ID, "owner", ("/admin_add", "/admin_del"))
    await make_user(TARGET_ID)

    sender = FakeSender()
    await ops.cmd_admin_add(make_update(OWNER_ID), make_context(sender, str(TARGET_ID), "cskh"))
    assert (
        await scalar(
            "SELECT role FROM admin_users WHERE user_id = :uid AND revoked_at IS NULL",
            {"uid": TARGET_ID},
        )
        == "cskh"
    )

    admin_service.invalidate_role()
    sender2 = FakeSender()
    await ops.cmd_admin_del(make_update(OWNER_ID), make_context(sender2, str(TARGET_ID)))

    # Thu hồi là ĐIỀN `revoked_at`, không DELETE — dòng phải còn đó.
    assert (
        await scalar("SELECT count(*) FROM admin_users WHERE user_id = :uid", {"uid": TARGET_ID})
        == 1
    )
    assert (
        await scalar(
            "SELECT (revoked_at IS NOT NULL) FROM admin_users WHERE user_id = :uid",
            {"uid": TARGET_ID},
        )
        is True
    )


@pytest.mark.asyncio
async def test_admin_add_vai_tro_khong_hop_le(wired):
    await make_admin(OWNER_ID, "owner", ("/admin_add",))
    await make_user(TARGET_ID)

    sender = FakeSender()
    await ops.cmd_admin_add(make_update(OWNER_ID), make_context(sender, str(TARGET_ID), "boss"))

    assert (
        await scalar("SELECT count(*) FROM admin_users WHERE user_id = :uid", {"uid": TARGET_ID})
        == 0
    )
    assert "boss" in sender.last


@pytest.mark.asyncio
async def test_admin_add_chi_owner_chay_duoc(wired):
    """`cskh` không có `/admin_add` trong `admin_permissions` nên không tự cấp quyền được."""
    await make_admin(CSKH_ID, "cskh", ("/help_admin",))
    await make_user(TARGET_ID)

    sender = FakeSender()
    await ops.cmd_admin_add(make_update(CSKH_ID), make_context(sender, str(TARGET_ID), "owner"))

    assert (
        await scalar("SELECT count(*) FROM admin_users WHERE user_id = :uid", {"uid": TARGET_ID})
        == 0
    )


# ── Hàm thuần ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "value_type", "expected"),
    [
        ("30", "int", 30),
        ("12.000.000", "money_vnd", 12_000_000),
        ("12_000_000", "money_vnd", 12_000_000),
        ("250", "bp", 250),
        ("true", "bool", True),
        ("FALSE", "bool", False),
        ("1", "bool", True),
        ("0", "bool", False),
        ("xin chao", "string", "xin chao"),
        ("[1, 2]", "json", [1, 2]),
        ('{"a": 1}', "json", {"a": 1}),
    ],
)
def test_parse_setting_value_hop_le(raw, value_type, expected):
    assert ops.parse_setting_value(raw, value_type) == expected


@pytest.mark.parametrize(
    ("raw", "value_type"),
    [
        ("1.5", "int"),
        ("10k", "money_vnd"),
        ("1.50", "money_vnd"),
        ("co", "bool"),
        ("rac", "json"),
        ("5", "json"),
        ('"chuoi"', "json"),
    ],
)
def test_parse_setting_value_sai_kieu(raw, value_type):
    with pytest.raises(ops.ValueParseError):
        ops.parse_setting_value(raw, value_type)


def test_parse_setting_value_khong_doc_nham_so_thap_phan_thanh_hang_trieu():
    """`1.5` phải bị từ chối, KHÔNG được đọc thành 15 — đây là tiền."""
    with pytest.raises(ops.ValueParseError):
        ops.parse_setting_value("1.5", "money_vnd")


def test_render_value_chi_nhom_hang_nghin_cho_tien():
    assert ops.render_value(10_000, "money_vnd") == "10.000đ"
    assert ops.render_value(3_600, "seconds") == "3600"
    assert ops.render_value(True, "bool") == "true"
