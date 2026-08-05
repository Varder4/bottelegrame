"""Luồng code tân thủ đầu-tới-cuối: cổng xác minh, kiểm nhóm, cấp code.

Chạy **qua chính handler**, trên database thật và Redis thật. Chỉ Telegram bị thay bằng
bản giả — đó là phần duy nhất không có gì để sai ở đây.

Bốn mệnh đề được kiểm, tất cả đều là chỗ hệ cũ mất tiền hoặc mất lòng người dùng:

- chưa xác minh mà bấm `check_groups` → **không** có mã nào rời kho;
- đã xác minh nhưng thiếu nhóm → **không** có mã nào rời kho;
- đủ điều kiện → đúng MỘT mã, `code_grants` đúng MỘT dòng;
- bấm lại lần hai → **đúng mã cũ**, không phải mã thứ hai.

⚠️ File này KHÔNG dùng fixture `db`/`seeded` của `conftest`. Handler đọc ghi qua engine
**toàn cục** (`db.engine.transaction()`), nên nếu test dựng dữ liệu bằng một engine thứ
hai thì hai bên chạy trên hai kết nối khác nhau và thứ tự commit giữa chúng trở thành một
cuộc đua — đúng loại lỗi nhấp nháy làm người ta mất niềm tin vào bộ test. Ở đây chỉ có
**một** engine, và cả test lẫn handler đều đi qua nó.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from telegram.error import TimedOut

from televip.apps.worker.handlers import tanthu
from televip.db.engine import session as db_session
from televip.domain import texts
from televip.services import membership, text_service
from tests.conftest import TEST_DATABASE_URL, _truncate_all, add_codes, make_user

VERIFY_URL = "https://example.test/verify"

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

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.answers: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return 1000 + len(self.messages)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]


class FakeBot:
    """`getChatMember` giả. `statuses` ánh xạ chat_id → trạng thái muốn trả về."""

    def __init__(self, statuses: dict[int, str] | None = None, *, fail: bool = False) -> None:
        self.statuses = statuses or {}
        self.fail = fail
        self.calls: list[tuple[int, int]] = []

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append((chat_id, user_id))
        if self.fail:
            raise TimedOut()
        status = self.statuses.get(chat_id, "left")
        return SimpleNamespace(
            status=status,
            is_member=status == "member",
            user=SimpleNamespace(id=user_id),
        )


def make_update(user_id: int, *, callback: bool = False, message_text: str | None = None) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=message_text, chat=chat)
    query = SimpleNamespace(data="check_groups", message=message) if callback else None
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=query,
    )


def make_context(sender: FakeSender, bot: FakeBot | None = None) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=bot or FakeBot(),
        args=[],
    )


# ── Fixture: một engine duy nhất, trỏ vào database test ─────────────


@pytest_asyncio.fixture
async def wired(redis_clean):
    from televip.db import engine as db_engine
    from televip.services import settings_service

    # Engine phải sinh và huỷ trong CÙNG event loop với test — pytest-asyncio tạo loop
    # riêng cho từng test, xem docstring fixture `engine` ở conftest.
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


# ── Helper dựng và soi dữ liệu (đều đi qua engine toàn cục) ─────────


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


def _link(chat_id: int) -> str:
    """Link mời của một nhóm test — khớp với `add_required_chat`."""
    return f"https://t.me/nhom{abs(chat_id)}"


async def add_required_chat(chat_id: int, *, sort_order: int = 0, health: str = "healthy") -> int:
    await run_sql(
        """
        INSERT INTO required_chats (chat_id, title, invite_link, is_active, sort_order, health)
             VALUES (:cid, :title, :link, true, :so, :health)
        ON CONFLICT (chat_id) DO NOTHING
        """,
        {
            "cid": chat_id,
            "title": f"Nhom {chat_id}",
            "link": f"https://t.me/nhom{abs(chat_id)}",
            "so": sort_order,
            "health": health,
        },
    )
    return chat_id


async def join_chat(user_id: int, chat_id: int, *, status: str = "member") -> None:
    await run_sql(
        """
        INSERT INTO group_memberships (user_id, chat_id, status, is_member, source, updated_at)
             VALUES (:uid, :cid, :st, :im, 'event', now())
        ON CONFLICT (user_id, chat_id) DO UPDATE
                SET status = EXCLUDED.status, is_member = EXCLUDED.is_member, updated_at = now()
        """,
        {"uid": user_id, "cid": chat_id, "st": status, "im": status in membership.JOINED_STATUSES},
    )


async def verify_user(user_id: int) -> None:
    await run_sql("UPDATE users SET verified_at = now() WHERE user_id = :uid", {"uid": user_id})


async def count_grants(user_id: int) -> int:
    return await scalar("SELECT count(*) FROM code_grants WHERE user_id = :uid", {"uid": user_id})


async def used_codes() -> int:
    return await scalar("SELECT count(*) FROM codes WHERE status <> 'available'")


async def setup_flow(user_id: int, *, chats: int = 1, codes: int = 5) -> list[int]:
    """Người dùng + kho code + N nhóm bắt buộc + cấu hình tối thiểu."""
    async with db_session() as s:
        await make_user(s, user_id)
        if codes:
            await add_codes(s, code_type="tanthu", value_vnd=10_000, count=codes)
    await set_setting("webapp.url", VERIFY_URL)
    await set_setting("code.tanthu_value_vnd", 10_000, "money_vnd")
    return [await add_required_chat(-1000 - i, sort_order=i) for i in range(chats)]


# ── 1. Chưa xác minh thì không cấp code ─────────────────────────────


@pytest.mark.asyncio
async def test_chua_xac_minh_thi_khong_cap_code(wired):
    uid = 9101
    chat_ids = await setup_flow(uid)
    await join_chat(uid, chat_ids[0])  # đã vào nhóm, nhưng CHƯA xác minh

    sender = FakeSender()
    bot = FakeBot()
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender, bot))

    assert await count_grants(uid) == 0, "chưa xác minh mà vẫn có grant"
    assert await used_codes() == 0, "kho code bị đụng tới"
    assert texts.not_verified() in sender.messages
    assert sender.answers, "callback phải được answer ngay, kể cả ở nhánh từ chối"
    assert bot.calls == [], "chưa qua cổng xác minh thì không được tốn lời gọi API nào"


@pytest.mark.asyncio
async def test_nut_tan_thu_chua_xac_minh_hien_buoc_1(wired):
    uid = 9102
    await setup_flow(uid)

    sender = FakeSender()
    await tanthu.handle_tanthu(make_update(uid), make_context(sender))

    assert sender.last == texts.tanthu_step1()


@pytest.mark.asyncio
async def test_buoc_1_lay_cau_chu_tu_ban_admin_da_sua(wired):
    """Admin sửa bản `tanthu.step1` trong `/suanoidung` thì người dùng phải thấy bản mới.

    Handler từng gọi thẳng `texts.tanthu_step1()`, nên lệnh sửa nội dung báo "đã lưu",
    bảng `message_templates` có bản mới, và người dùng vẫn nhận đúng bản cũ cứng trong
    code — một tính năng nói dối về việc mình có tác dụng.
    """
    uid = 9104
    await setup_flow(uid)
    await text_service.set_content("tanthu.step1", "BẢN ADMIN VỪA SỬA", updated_by=uid)

    sender = FakeSender()
    await tanthu.handle_tanthu(make_update(uid), make_context(sender))

    assert sender.last == "BẢN ADMIN VỪA SỬA"


@pytest.mark.asyncio
async def test_buoc_2_lay_cau_chu_tu_ban_admin_da_sua(wired):
    """Cùng lý do, và thêm: ba biến của mẫu phải được truyền đủ.

    Thiếu một biến là `TemplateError` ném từ giữa handler — người dùng bấm nút rồi không
    nhận được gì cả.
    """
    uid = 9105
    chat_ids = await setup_flow(uid, chats=2)
    await verify_user(uid)
    await set_setting("link.fanpage", "https://fb.com/televip")
    await text_service.set_content(
        "tanthu.step2",
        "vào {total} nhóm:\n{invite_list}\nfanpage {fanpage_link}",
        updated_by=uid,
    )

    sender = FakeSender()
    await tanthu.handle_tanthu(make_update(uid), make_context(sender))

    assert sender.last.startswith("vào 2 nhóm:")
    assert "1️⃣ " in sender.last and "2️⃣ " in sender.last, "danh sách link phải giữ đánh số"
    assert sender.last.endswith("fanpage https://fb.com/televip")
    assert len(chat_ids) == 2


@pytest.mark.asyncio
async def test_nut_tan_thu_da_xac_minh_hien_buoc_2(wired):
    uid = 9103
    await setup_flow(uid, chats=2)
    await verify_user(uid)
    await set_setting("link.fanpage", "https://fb.com/televip")

    sender = FakeSender()
    await tanthu.handle_tanthu(make_update(uid), make_context(sender))

    assert "BƯỚC 2: THAM GIA NHÓM" in sender.last
    assert "Tham gia đủ 2 nhóm/kênh sau" in sender.last
    assert "https://fb.com/televip" in sender.last


# ── 2. Đã xác minh nhưng thiếu nhóm ─────────────────────────────────


@pytest.mark.asyncio
async def test_thieu_nhom_thi_khong_cap_code(wired):
    uid = 9201
    chat_ids = await setup_flow(uid, chats=2)
    await verify_user(uid)
    await join_chat(uid, chat_ids[0])  # mới vào 1 trong 2

    sender = FakeSender()
    # Nhóm thứ hai chưa có bản ghi ⇒ đi đường dự phòng, và Telegram nói "left".
    bot = FakeBot({chat_ids[0]: "member", chat_ids[1]: "left"})
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender, bot))

    assert await count_grants(uid) == 0
    assert await used_codes() == 0
    assert sender.last == texts.missing_groups(1, 2, [_link(chat_ids[1])])
    assert _link(chat_ids[1]) in sender.last, "phải chỉ ĐÚNG nhóm còn thiếu"
    assert _link(chat_ids[0]) not in sender.last, "nhóm đã vào không được liệt kê"
    assert bot.calls == [(chat_ids[1], uid)], "chỉ nhóm THIẾU bản ghi mới được hỏi Telegram"


@pytest.mark.asyncio
async def test_roi_nhom_thi_mat_quyen(wired):
    """Bản ghi nói `left` và còn tươi ⇒ kết luận ngay, không hỏi Telegram."""
    uid = 9202
    chat_ids = await setup_flow(uid)
    await verify_user(uid)
    await join_chat(uid, chat_ids[0], status="left")

    sender = FakeSender()
    bot = FakeBot()
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender, bot))

    assert sender.last == texts.missing_groups(0, 1, [_link(chat_ids[0])])
    assert bot.calls == []
    assert await count_grants(uid) == 0


# ── 3. Đủ điều kiện: đúng một mã ────────────────────────────────────


@pytest.mark.asyncio
async def test_du_dieu_kien_cap_dung_mot_ma(wired):
    uid = 9301
    chat_ids = await setup_flow(uid, chats=2)
    await verify_user(uid)
    for cid in chat_ids:
        await join_chat(uid, cid)

    sender = FakeSender()
    bot = FakeBot()
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender, bot))

    assert await count_grants(uid) == 1, "phải có ĐÚNG một dòng code_grants"
    assert await used_codes() == 1, "phải có ĐÚNG một mã rời kho"
    assert bot.calls == [], "đủ bản ghi tươi thì 0 lời gọi API — đây là lý do khối này tồn tại"
    assert "CHÚC MỪNG" in sender.last

    async with db_session() as s:
        row = (
            await s.execute(
                text("""
                SELECT g.state, g.grant_key, g.value_vnd, c.code_value, c.status
                  FROM code_grants g JOIN codes c ON c.code_id = g.code_id
                 WHERE g.user_id = :uid
                """),
                {"uid": uid},
            )
        ).one()

    assert row.state == "delivered", "mark_delivered() phải chạy sau khi gửi thành công"
    assert row.status == "issued"
    assert row.grant_key == f"tanthu:{uid}"
    assert row.value_vnd == 10_000
    assert row.code_value in sender.last


# ── 4. Bấm lại lần hai: đúng mã cũ ──────────────────────────────────


@pytest.mark.asyncio
async def test_bam_lai_tra_ve_dung_ma_cu(wired):
    uid = 9401
    chat_ids = await setup_flow(uid)
    await verify_user(uid)
    await join_chat(uid, chat_ids[0])

    sender = FakeSender()
    update, context = make_update(uid, callback=True), make_context(sender)
    await tanthu.handle_check_groups(update, context)
    await tanthu.handle_check_groups(update, context)

    assert await count_grants(uid) == 1, "lần bấm thứ hai KHÔNG được sinh grant thứ hai"
    assert await used_codes() == 1, "226 người ôm 544 code thừa vì đúng chỗ này"

    code_value = await scalar(
        "SELECT c.code_value FROM code_grants g JOIN codes c USING (code_id) WHERE g.user_id = :uid",
        {"uid": uid},
    )
    assert len(sender.messages) == 2
    assert all(code_value in msg for msg in sender.messages), "hai lần bấm phải ra cùng một mã"


@pytest.mark.asyncio
async def test_het_kho_thi_khong_tao_grant(wired):
    uid = 9402
    chat_ids = await setup_flow(uid, codes=0)
    await verify_user(uid)
    await join_chat(uid, chat_ids[0])
    await set_setting("link.support", "https://t.me/cskh")

    sender = FakeSender()
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender))

    assert await count_grants(uid) == 0, "hết kho thì không có gì để trừ, kể cả grant"
    assert sender.last == texts.out_of_stock("https://t.me/cskh")


# ── 5. Hạ tầng lỗi: không đổ lên đầu người dùng ─────────────────────


@pytest.mark.asyncio
async def test_khong_tra_duoc_thi_bao_ban_khong_bao_thieu_nhom(wired):
    """Lỗi âm tính giả của bot cũ: Telegram chậm ⇒ báo "Tiến độ 0/2" cho người đã vào đủ."""
    uid = 9501
    await setup_flow(uid)
    await verify_user(uid)

    sender = FakeSender()
    bot = FakeBot(fail=True)
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender, bot))

    assert sender.last == texts.system_busy()
    assert texts.missing_groups(0, 1) not in sender.messages
    assert await count_grants(uid) == 0


# ── 6. Bảng membership được nuôi bằng sự kiện ───────────────────────


def make_chat_member_update(user_id: int, chat_id: int, old: str, new: str) -> Any:
    return SimpleNamespace(
        chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            old_chat_member=SimpleNamespace(status=old, user=SimpleNamespace(id=user_id)),
            new_chat_member=SimpleNamespace(status=new, user=SimpleNamespace(id=user_id)),
        )
    )


@pytest.mark.asyncio
async def test_su_kien_chat_member_ghi_bang(wired):
    uid = 9601
    async with db_session() as s:
        await make_user(s, uid)
    cid = await add_required_chat(-2001)

    await membership.handle_chat_member_update(
        make_chat_member_update(uid, cid, "left", "member"), make_context(FakeSender())
    )

    async with db_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, is_member, source FROM group_memberships WHERE user_id = :uid"
                ),
                {"uid": uid},
            )
        ).one()
        events = (
            await s.execute(
                text("SELECT old_status, new_status FROM membership_events WHERE user_id = :uid"),
                {"uid": uid},
            )
        ).all()

    assert (row.status, row.is_member, row.source) == ("member", True, "event")
    assert [(e.old_status, e.new_status) for e in events] == [("left", "member")]


@pytest.mark.asyncio
async def test_su_kien_tu_nhom_khong_bat_buoc_bi_bo_qua(wired):
    """`group_memberships.chat_id` có FK — ghi một chat lạ vào là câu INSERT nổ."""
    uid = 9602
    async with db_session() as s:
        await make_user(s, uid)

    await membership.handle_chat_member_update(
        make_chat_member_update(uid, -999_999, "left", "member"), make_context(FakeSender())
    )

    assert await scalar("SELECT count(*) FROM group_memberships") == 0


@pytest.mark.asyncio
async def test_restricted_khong_tinh_khi_khong_con_la_thanh_vien(wired):
    uid = 9603
    chat_ids = await setup_flow(uid)
    await verify_user(uid)

    sender = FakeSender()
    # Telegram trả `restricted` kèm `is_member=False`: bị hạn chế VÀ đã bị đẩy ra.
    bot = FakeBot({chat_ids[0]: "restricted"})
    await tanthu.handle_check_groups(make_update(uid, callback=True), make_context(sender, bot))

    assert sender.last == texts.missing_groups(0, 1, [_link(chat_ids[0])])
    assert await count_grants(uid) == 0

    async with db_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, is_member, source FROM group_memberships WHERE user_id = :uid"
                ),
                {"uid": uid},
            )
        ).one()
    assert (row.status, row.is_member, row.source) == ("restricted", False, "poll")


@pytest.mark.asyncio
async def test_lan_bam_thu_hai_khong_hoi_lai_telegram(wired):
    """Kết quả `getChatMember` được ghi xuống bảng ⇒ lượt sau tốn 0 lời gọi."""
    uid = 9604
    chat_ids = await setup_flow(uid)
    await verify_user(uid)

    sender = FakeSender()
    bot = FakeBot({chat_ids[0]: "member"})
    update, context = make_update(uid, callback=True), make_context(sender, bot)

    await tanthu.handle_check_groups(update, context)
    assert bot.calls == [(chat_ids[0], uid)]

    await tanthu.handle_check_groups(update, context)
    assert bot.calls == [(chat_ids[0], uid)], "lượt hai phải đọc bảng, không hỏi lại Telegram"
    assert await count_grants(uid) == 1
