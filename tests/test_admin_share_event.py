"""Bộ ba lệnh của chiến dịch chia sẻ — `/update_share_event`, `/show_share_event`,
`/done_event`.

Bốn mệnh đề trọng tâm:

- `/update_share_event` **chỉ** nhận reply vào một ảnh, và caption phải nằm trong trần
  1.024 ký tự của tin CÓ ẢNH. Lưu một caption dài hơn thì bảng ghi nhận thành công còn
  bài đăng vĩnh viễn không tới được ai.
- Không truyền link mới thì **giữ nguyên link cũ** — ghi đè bằng NULL nghĩa là hai nút
  của người dùng mất đường dẫn chỉ vì admin sửa mỗi cái ảnh.
- `/done_event` là **một lần trọn đời**, và hàng rào đó do database giữ.
- Gửi cho người nhận thất bại thì **KHÔNG** đánh dấu đã giao: mã phải quay về kho.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import share_event as se
from televip.db.engine import session as db_session
from tests.conftest import TEST_DATABASE_URL, _truncate_all, add_codes, make_user

OWNER_ID = 940_001
OUTSIDER_ID = 940_002
TARGET_ID = 940_100

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
    def __init__(self, *, deliver: bool = True) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str, str]] = []
        self.deliver = deliver

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append((chat_id, text))
        return len(self.messages) if self.deliver else None

    async def send_photo(
        self, chat_id: int, photo: str, caption: str | None = None, **kwargs: Any
    ) -> int | None:
        self.photos.append((chat_id, photo, caption or ""))
        return len(self.photos)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        return None

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1][1]

    @property
    def all_text(self) -> str:
        return "\n".join(body for _, body in self.messages)

    def to(self, chat_id: int) -> list[str]:
        return [body for cid, body in self.messages if cid == chat_id]


def make_update(user_id: int, *, reply_photo: tuple[str, str] | None = None) -> Any:
    """`reply_photo = (file_id, caption)` dựng một lệnh reply vào ảnh."""
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    replied = None
    if reply_photo is not None:
        file_id, caption = reply_photo
        # Telegram trả NHIỀU cỡ của cùng một ảnh, sắp nhỏ → lớn. Dựng đúng như vậy để bài
        # kiểm bắt được lỗi lấy nhầm bản thu nhỏ.
        replied = SimpleNamespace(
            photo=[
                SimpleNamespace(file_id=f"{file_id}_thumb"),
                SimpleNamespace(file_id=file_id),
            ],
            caption=caption,
        )
    message = SimpleNamespace(message_id=1, text=None, chat=chat, reply_to_message=replied)
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=None,
    )


def make_context(sender: FakeSender, *args: str) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}, bot=SimpleNamespace()),
        bot=SimpleNamespace(username="testbot"),
        args=list(args),
    )


# ── Fixture ─────────────────────────────────────────────────────────


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


async def grant_role(user_id: int, role: str) -> None:
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, user_id)
        await s.commit()
    await run_sql(
        """
        INSERT INTO admin_users (user_id, role, added_by) VALUES (:uid, :role, :uid)
        ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL
        """,
        {"uid": user_id, "role": role},
    )
    for command, _ in se.COMMANDS:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES (:role, :cmd) "
            "ON CONFLICT DO NOTHING",
            {"role": role, "cmd": f"/{command}"},
        )
    admin_service.invalidate_role(user_id)


@pytest_asyncio.fixture
async def owner(wired):
    await grant_role(OWNER_ID, "owner")
    return OWNER_ID


async def config_row() -> Any:
    async with db_session() as s:
        return (
            await s.execute(text("SELECT * FROM share_event_config WHERE id = 1"))
        ).one_or_none()


# ── /update_share_event ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_doi_reply_vao_mot_anh(owner):
    sender = FakeSender()
    await se.cmd_update_share_event(make_update(owner), make_context(sender))

    assert "CÁCH DÙNG" in sender.last
    assert await config_row() is None, "không có ảnh mà vẫn ghi bảng"


@pytest.mark.asyncio
async def test_update_ghi_anh_caption_va_link(owner):
    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("FILE123", "Bài chia sẻ mới")),
        make_context(sender, "https://t.me/kenh/9"),
    )

    row = await config_row()
    assert row.image_file_id == "FILE123", "lấy nhầm bản thu nhỏ — bài đăng sẽ vỡ khi hiện"
    assert row.caption == "Bài chia sẻ mới"
    assert row.share_link == "https://t.me/kenh/9"
    assert row.updated_by == owner
    assert "ĐÃ CẬP NHẬT" in sender.last
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'update_share_event'") == 1


@pytest.mark.asyncio
async def test_update_khong_truyen_link_thi_giu_link_cu(owner):
    """Ghi đè bằng NULL nghĩa là hai nút của người dùng mất đường dẫn."""
    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("F1", "bản 1")), make_context(sender, "https://cu.test/1")
    )
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("F2", "bản 2")), make_context(sender)
    )

    row = await config_row()
    assert row.caption == "bản 2", "lần cập nhật thứ hai không ghi được"
    assert row.share_link == "https://cu.test/1", "link cũ bị xoá dù admin chỉ đổi ảnh"


@pytest.mark.asyncio
async def test_update_tu_choi_caption_rong(owner):
    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("F1", "   ")), make_context(sender)
    )

    assert "không có caption" in sender.last
    assert await config_row() is None


@pytest.mark.asyncio
async def test_update_tu_choi_caption_vuot_tran_cua_tin_co_anh(owner):
    """Trần của tin CÓ ẢNH là 1.024, không phải 4.096 — vượt là bài đăng không tới được ai."""
    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("F1", "x" * (se.MAX_CAPTION_CHARS + 1))),
        make_context(sender),
    )

    assert "trần của một tin CÓ ẢNH" in sender.last
    assert await config_row() is None

    # Đúng ngưỡng thì vẫn qua — đây là trần, không phải mốc dưới.
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("F1", "y" * se.MAX_CAPTION_CHARS)), make_context(sender)
    )
    assert (await config_row()) is not None


@pytest.mark.asyncio
async def test_update_nguoi_ngoai_khong_ghi_duoc_gi(wired):
    async with db_session() as s:
        await make_user(s, OUTSIDER_ID)
        await s.commit()

    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(OUTSIDER_ID, reply_photo=("F1", "cướp bài")), make_context(sender)
    )

    assert sender.messages == [], "không được rò rỉ sự tồn tại của lệnh"
    assert await config_row() is None


# ── /show_share_event ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_dung_chinh_duong_cua_nguoi_dung(owner):
    """Bản xem thử tự dựng sẽ trôi khỏi bản thật đúng lúc người ta cần nó nhất."""
    await set_setting("link.support", "https://t.me/cskh")
    await set_setting("cooldown.share_event", 0, "seconds")
    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("FILEXEM", "Nội dung người dùng thấy")),
        make_context(sender, "https://chia.se/1"),
    )

    sender = FakeSender()
    await se.cmd_show_share_event(make_update(owner), make_context(sender))

    assert "CẤU HÌNH EVENT CHIA SẺ" in sender.all_text
    # ...và ảnh thật được gửi qua đúng `file_id` đã lưu.
    assert sender.photos, "không gửi bản xem thử — admin không kiểm được trước khi công bố"
    assert sender.photos[-1][1] == "FILEXEM"
    assert "Nội dung người dùng thấy" in sender.photos[-1][2]


@pytest.mark.asyncio
async def test_show_noi_ro_khi_chua_cau_hinh(owner):
    await set_setting("cooldown.share_event", 0, "seconds")
    sender = FakeSender()
    await se.cmd_show_share_event(make_update(owner), make_context(sender))

    assert "CHƯA CẤU HÌNH" in sender.all_text


# ── /done_event ─────────────────────────────────────────────────────


async def _san_sang_trao(*, codes: int = 2) -> None:
    await set_setting(se.VALUE_KEY, 10_000, "money_vnd")
    async with db_session() as s:
        await make_user(s, TARGET_ID)
        if codes:
            await add_codes(
                s, code_type=se.CODE_TYPE, value_vnd=10_000, count=codes, prefix="SHARE"
            )
        await s.commit()


@pytest.mark.asyncio
async def test_done_event_trao_code_va_ghi_so(owner):
    await _san_sang_trao()

    sender = FakeSender()
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    assert "ĐÃ TRAO CODE" in sender.to(owner)[-1]
    assert sender.to(TARGET_ID), "người nhận không được gửi gì cả"
    assert (
        await scalar(
            "SELECT state FROM code_grants WHERE user_id = :u AND grant_type = 'share_event'",
            {"u": TARGET_ID},
        )
        == "delivered"
    )
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'done_event'") == 1


@pytest.mark.asyncio
async def test_done_event_mot_lan_tron_doi(owner):
    """Hàng rào do database giữ, không do một câu `if` mà hai CSKH bấm nhanh chạy qua được."""
    await _san_sang_trao()

    sender = FakeSender()
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    assert "ĐÃ NHẬN" in sender.to(owner)[-1]
    assert (
        await scalar(
            "SELECT count(*) FROM code_grants WHERE user_id = :u AND grant_type = 'share_event'",
            {"u": TARGET_ID},
        )
        == 1
    ), "trao hai lần cho cùng một người"

    # `reserve()` idempotent nên lần hai trả về ĐÚNG grant cũ, không nổ. Hai kiểm dưới là
    # chỗ phân biệt "không cấp mã thứ hai" với "báo cáo trung thực rằng không cấp":
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'done_event'") == 1, (
        "hai dòng sổ cho một lần trao — sổ mất giá trị đối chiếu"
    )
    assert len(sender.to(TARGET_ID)) == 1, "gửi lại mã cho người dùng ở lần chạy thứ hai"


@pytest.mark.asyncio
async def test_done_event_het_kho_thi_noi_ro_cach_nap(owner):
    await _san_sang_trao(codes=0)

    sender = FakeSender()
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    assert "RỖNG" in sender.last
    assert "/add_giffcode" in sender.last
    assert await scalar("SELECT count(*) FROM code_grants") == 0


@pytest.mark.asyncio
async def test_done_event_gui_that_bai_thi_khong_danh_dau_da_giao(owner):
    """Grant nằm lại ở `reserved` để `reap_reservations()` trả mã về kho."""
    await _san_sang_trao()

    sender = FakeSender(deliver=False)
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    assert (
        await scalar("SELECT state FROM code_grants WHERE user_id = :u", {"u": TARGET_ID})
        == "reserved"
    ), "đánh dấu đã giao một mã chưa ai nhận — sổ cái nói dối"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'done_event'") == 0


@pytest.mark.asyncio
async def test_done_event_khong_tim_thay_nguoi_dung(owner):
    sender = FakeSender()
    await se.cmd_done_event(make_update(owner), make_context(sender, "@aokhongcothat"))
    assert "Không tìm thấy" in sender.last


# ── Ba nhánh bộ soát chỉ ra, và không bài kiểm nào canh ─────────────


def make_group_update(user_id: int) -> Any:
    """Lệnh gõ trong NHÓM điều hành — trường hợp thường, không phải ngoại lệ."""
    update = make_update(user_id)
    update.effective_chat = SimpleNamespace(id=-100_123, type="supergroup")
    return update


@pytest.mark.asyncio
async def test_show_trong_nhom_khong_duoc_hua_roi_im_lang(owner):
    """`misc._enter` trả `None` với mọi chat không phải riêng — nên bản xem thử không hiện.

    Bản trước vẫn in "Bên dưới là ĐÚNG thứ người dùng thấy 👇" rồi **không gửi gì cả**:
    không ảnh, không lỗi, không dấu hiệu nào. Mà cả lệnh này tồn tại để kiểm tra trước khi
    công bố, nên im lặng là kiểu hỏng tệ nhất có thể ở đây.
    """
    await set_setting("cooldown.share_event", 0, "seconds")
    sender = FakeSender()
    await se.cmd_update_share_event(
        make_update(owner, reply_photo=("F1", "bài đăng")), make_context(sender, "https://c.se/1")
    )

    sender = FakeSender()
    await se.cmd_show_share_event(make_group_update(owner), make_context(sender))

    assert sender.messages, "gõ trong nhóm mà bot im lặng hoàn toàn"
    assert "CHAT RIÊNG" in sender.last
    # Vẫn phải trả lời được câu hỏi thường gặp nhất: bài đã đặt chưa, link nào.
    assert "https://c.se/1" in sender.last
    assert "👇" not in sender.last, "hứa một bản xem thử mà không có gì theo sau"


@pytest.mark.asyncio
async def test_done_event_gui_hong_roi_thu_lai_thi_NHAN_DUOC_MA(owner):
    """Kịch bản bộ soát dựng lại được: gửi hỏng → job dọn kho → CSKH chạy lại.

    Lệnh tự dặn "bảo họ mở chat với bot rồi chạy lại lệnh". Bản trước, chạy lại đúng câu
    dặn đó trả về "CHƯA gắn được mã (lúc đó kho rỗng)" — trong khi kho đầy 50 mã. Người
    dùng không bao giờ nhận được code, và không lệnh admin nào gỡ được.
    """
    await _san_sang_trao(codes=50)

    # Lần một: gửi cho người nhận thất bại.
    sender = FakeSender(deliver=False)
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))
    assert (
        await scalar("SELECT state FROM code_grants WHERE user_id = :u", {"u": TARGET_ID})
        == "reserved"
    )

    # Job dọn kho chạy — trả mã về kho và NULL `code_grants.code_id`.
    from televip.services import code_issuance

    await run_sql("UPDATE codes SET reserved_until = now() - interval '1 minute'")
    async with db_session() as s:
        await code_issuance.reap_reservations(s)
        await s.commit()

    # Lần hai: đúng câu lệnh mà bot vừa dặn.
    sender = FakeSender()
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    assert "ĐÃ TRAO CODE" in sender.to(owner)[-1], sender.to(owner)[-1]
    assert sender.to(TARGET_ID), "người dùng vẫn không nhận được gì"
    assert (
        await scalar("SELECT state FROM code_grants WHERE user_id = :u", {"u": TARGET_ID})
        == "delivered"
    )
    assert (
        await scalar("SELECT count(*) FROM code_grants WHERE user_id = :u", {"u": TARGET_ID}) == 1
    ), "gắn lại phải dùng ĐÚNG grant cũ, không tạo grant thứ hai"


@pytest.mark.asyncio
async def test_done_event_gui_hong_roi_thu_lai_NGAY_thi_van_gui_lai_ma(owner):
    """Trước khi job dọn kho kịp chạy: grant vẫn giữ mã, state vẫn `reserved`.

    Bản trước rơi vào nhánh `was_existing` và báo "ĐÃ NHẬN code trước đó" kèm số hiệu mã —
    đóng hồ sơ trên một lời không đúng, vì người dùng chưa cầm gì cả. Mã đó rồi bị dọn về
    kho và trao cho người kế tiếp.
    """
    await _san_sang_trao()

    sender = FakeSender(deliver=False)
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    sender = FakeSender()
    await se.cmd_done_event(make_update(owner), make_context(sender, str(TARGET_ID)))

    assert "ĐÃ NHẬN" not in sender.to(owner)[-1], sender.to(owner)[-1]
    assert sender.to(TARGET_ID), "không gửi lại mã cho người chưa từng nhận được gì"
    assert (
        await scalar("SELECT state FROM code_grants WHERE user_id = :u", {"u": TARGET_ID})
        == "delivered"
    )
