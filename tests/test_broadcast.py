"""Đợt bắn tin — dịch vụ và lệnh admin, chạy trên database thật.

Bốn mệnh đề trọng tâm, mỗi cái là một chỗ hệ cũ đã trả giá (`02-broadcast.md`):

- `materialize_targets` **loại đúng** người đã chặn bot, người tắt thông báo, người chưa
  từng `/start` và người đang bị khoá. Gửi cho ba nhóm đầu chỉ sinh 403 hàng loạt — tín
  hiệu xấu nhất có thể gửi cho hệ chống spam của Telegram.
- `pump` gọi bao nhiêu lần cũng ra đúng một tin cho mỗi người. Hệ cũ không có khái niệm
  khoá tất định nên mỗi lần restart giữa đợt là một vòng gửi lại từ đầu.
- `/broadcast` không có `--all` thì **chỉ** bắn tệp `active_30d`. Toàn tệp phải là một
  hành động gõ ra có ý thức.
- Số đích dưới ngưỡng thì lệnh **từ chối và huỷ luôn đợt nháp** — không để lại một nút
  "GỬI NGAY" treo lơ lửng.

⚠️ Cùng luật với `test_admin_ops.py`: KHÔNG dùng fixture `db`/`seeded` của conftest.
Handler và dịch vụ đọc ghi qua engine **toàn cục**, nên dựng dữ liệu bằng engine thứ hai
sẽ biến thứ tự commit giữa hai bên thành một cuộc đua. Ở đây chỉ có một engine.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import broadcast as handler
from televip.db.engine import session as db_session
from televip.db.engine import transaction
from televip.services import broadcast as service
from tests.conftest import TEST_DATABASE_URL, _truncate_all

OWNER_ID = 910_001

BROADCAST_COMMANDS = (
    "/broadcast",
    "/broadcast_status",
    "/broadcast_pause",
    "/broadcast_resume",
    "/broadcast_cancel",
)


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.markups: list[Any] = []
        self.answered: list[Any] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return 1000 + len(self.messages)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answered.append(query)

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]


def make_update(user_id: int = OWNER_ID, *, callback_data: str | None = None) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    query = None if callback_data is None else SimpleNamespace(data=callback_data, id="q1")
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=SimpleNamespace(message_id=1, text=None, chat=chat),
        callback_query=query,
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
    await make_admin(OWNER_ID, "owner", BROADCAST_COMMANDS)

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


async def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    async with db_session() as s:
        return list((await s.execute(text(sql), params or {})).all())


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def make_user(
    user_id: int,
    *,
    started: bool = True,
    optout: bool = False,
    blocked: bool = False,
    active_days_ago: int = 0,
) -> int:
    """Một hàng `users` với đúng ba cột quyết định tư cách nhận broadcast."""
    await run_sql(
        """
        INSERT INTO users (user_id, username, started_bot_at, notify_optout, blocked_at,
                           last_active)
             VALUES (:uid, :un,
                     CASE WHEN :started THEN now() ELSE NULL END,
                     :optout,
                     CASE WHEN :blocked THEN now() ELSE NULL END,
                     now() - make_interval(days => :days))
        ON CONFLICT (user_id) DO NOTHING
        """,
        {
            "uid": user_id,
            "un": f"user{user_id}",
            "started": started,
            "optout": optout,
            "blocked": blocked,
            "days": active_days_ago,
        },
    )
    return user_id


async def ban_user(user_id: int, *, unbanned: bool = False) -> None:
    await run_sql(
        """
        INSERT INTO user_bans (user_id, reason, banned_by, unbanned_at)
             VALUES (:uid, 'test', 1, CASE WHEN :unbanned THEN now() ELSE NULL END)
        ON CONFLICT (user_id) DO NOTHING
        """,
        {"uid": user_id, "unbanned": unbanned},
    )


async def make_admin(user_id: int, role: str, commands: tuple[str, ...]) -> None:
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
            "INSERT INTO admin_permissions (role, command) VALUES (:role, :cmd) "
            "ON CONFLICT DO NOTHING",
            {"role": role, "cmd": command},
        )
    admin_service.invalidate_role(user_id)


async def set_min_audience(value: int) -> None:
    from televip.services import settings_service

    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi)
             VALUES (:key, CAST(:val AS jsonb), 'int', 'nguong test')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {"key": handler.MIN_AUDIENCE_KEY, "val": str(value)},
    )
    settings_service.invalidate()


async def new_job(audience: str = "active_30d", *, text_body: str = "xin chao") -> int:
    async with transaction() as db:
        return await service.create_job(
            db,
            kind="broadcast",
            audience=audience,
            payload={"text": text_body},
            created_by=OWNER_ID,
        )


async def target_ids(job_id: int) -> set[int]:
    rows = await fetch_all("SELECT user_id FROM broadcast_targets WHERE job_id = :j", {"j": job_id})
    return {row.user_id for row in rows}


# ── materialize_targets ─────────────────────────────────────────────


async def test_materialize_loai_nguoi_khong_gui_duoc(wired) -> None:
    """Ba nhóm bị loại ở MỌI audience, cộng người đang bị khoá."""
    binh_thuong = await make_user(1_001)
    await make_user(1_002, blocked=True)
    await make_user(1_003, optout=True)
    await make_user(1_004, started=False)
    da_ban = await make_user(1_005)
    await ban_user(da_ban)
    da_go_ban = await make_user(1_006)
    await ban_user(da_go_ban, unbanned=True)

    job_id = await new_job("all")
    async with transaction() as db:
        total = await service.materialize_targets(db, job_id=job_id, audience="all")

    # OWNER_ID cũng là một hàng `users` hợp lệ nên nó nằm trong tệp đích — đó là hành vi
    # đúng, admin cũng là người dùng của bot.
    assert await target_ids(job_id) == {binh_thuong, da_go_ban, OWNER_ID}
    assert total == 3
    assert await scalar("SELECT total FROM broadcast_jobs WHERE job_id = :j", {"j": job_id}) == 3


async def test_materialize_active_30d_bo_nguoi_lau_khong_vao(wired) -> None:
    moi = await make_user(2_001, active_days_ago=1)
    cu = await make_user(2_002, active_days_ago=60)

    hep = await new_job("active_30d")
    rong = await new_job("all")
    async with transaction() as db:
        await service.materialize_targets(db, job_id=hep, audience="active_30d")
        await service.materialize_targets(db, job_id=rong, audience="all")

    assert await target_ids(hep) == {moi, OWNER_ID}
    assert await target_ids(rong) == {moi, cu, OWNER_ID}


async def test_materialize_goi_lai_khong_nhan_doi(wired) -> None:
    await make_user(2_101)
    job_id = await new_job("all")

    async with transaction() as db:
        first = await service.materialize_targets(db, job_id=job_id, audience="all")
    async with transaction() as db:
        second = await service.materialize_targets(db, job_id=job_id, audience="all")

    assert first == second == 2  # người test + OWNER_ID
    assert (
        await scalar("SELECT count(*) FROM broadcast_targets WHERE job_id = :j", {"j": job_id}) == 2
    )


async def test_materialize_tu_choi_audience_la(wired) -> None:
    job_id = await new_job("all")
    with pytest.raises(ValueError, match="audience"):
        async with transaction() as db:
            await service.materialize_targets(db, job_id=job_id, audience="custom")


# ── pump ────────────────────────────────────────────────────────────


async def _running_job(n_users: int = 3, *, text_body: str = "xin chao") -> int:
    for index in range(n_users):
        await make_user(3_001 + index)
    job_id = await new_job("all", text_body=text_body)
    async with transaction() as db:
        await service.materialize_targets(db, job_id=job_id, audience="all")
        await service.start(db, job_id=job_id)
    return job_id


async def test_pump_sinh_dung_so_dong_va_goi_lai_khong_nhan_doi(wired) -> None:
    job_id = await _running_job(3)  # 3 người test + OWNER_ID = 4 đích

    async with transaction() as db:
        moved = await service.pump(db, job_id=job_id, limit=100)
    assert moved == 4
    assert await scalar("SELECT count(*) FROM outbox_messages") == 4

    async with transaction() as db:
        again = await service.pump(db, job_id=job_id, limit=100)
    assert again == 0
    assert await scalar("SELECT count(*) FROM outbox_messages") == 4
    assert (
        await scalar(
            "SELECT count(*) FROM broadcast_targets WHERE job_id = :j AND state = 0", {"j": job_id}
        )
        == 0
    )


async def test_pump_theo_lo(wired) -> None:
    job_id = await _running_job(3)

    async with transaction() as db:
        assert await service.pump(db, job_id=job_id, limit=2) == 2
    async with transaction() as db:
        assert await service.pump(db, job_id=job_id, limit=2) == 2
    async with transaction() as db:
        assert await service.pump(db, job_id=job_id, limit=2) == 0

    assert await scalar("SELECT count(*) FROM outbox_messages") == 4


async def test_pump_dat_dung_khoa_va_payload(wired) -> None:
    job_id = await _running_job(1, text_body="noi dung that")
    async with transaction() as db:
        await service.pump(db, job_id=job_id, limit=10)

    rows = await fetch_all(
        "SELECT chat_id, method, payload, dedupe_key FROM outbox_messages ORDER BY chat_id"
    )
    assert {row.dedupe_key for row in rows} == {
        service.dedupe_key(job_id, row.chat_id) for row in rows
    }
    for row in rows:
        assert row.method == "sendMessage"
        assert row.payload["text"] == "noi dung that"
        assert row.payload["lane"] == service.OUTBOX_LANE
        assert row.payload["job_id"] == job_id
        assert row.payload["user_id"] == row.chat_id


async def test_pump_bo_qua_dot_khong_chay(wired) -> None:
    """`pause` phải chặn được thật, không chỉ đổi một chữ trong bảng."""
    job_id = await _running_job(2)
    async with transaction() as db:
        assert await service.pause(db, job_id=job_id) == "paused"
    async with transaction() as db:
        assert await service.pump(db, job_id=job_id, limit=100) == 0
    assert await scalar("SELECT count(*) FROM outbox_messages") == 0

    async with transaction() as db:
        assert await service.resume(db, job_id=job_id) == "running"
        assert await service.pump(db, job_id=job_id, limit=100) == 3


async def test_pump_het_dich_thi_dong_dot(wired) -> None:
    job_id = await _running_job(1)
    async with transaction() as db:
        await service.pump(db, job_id=job_id, limit=100)
    async with transaction() as db:
        assert await service.finish_if_drained(db, job_id=job_id) is True
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id}) == "done"
    )


# ── cancel ──────────────────────────────────────────────────────────


async def test_cancel_rut_tin_chua_gui_va_bo_qua_dich_con_lai(wired) -> None:
    job_id = await _running_job(3)  # 4 đích
    async with transaction() as db:
        await service.pump(db, job_id=job_id, limit=2)

    # Một trong hai tin đã gửi xong: nó phải ở lại, không rút về được nữa.
    await run_sql("UPDATE outbox_messages SET state = 1 WHERE outbox_id = 1")

    async with transaction() as db:
        result = await service.cancel(db, job_id=job_id)

    assert result.cancelled is True
    assert result.dropped_outbox == 1
    assert result.skipped_targets == 2
    assert await scalar("SELECT count(*) FROM outbox_messages") == 1
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id})
        == "cancelled"
    )


async def test_cancel_lan_hai_khong_lam_gi(wired) -> None:
    job_id = await _running_job(1)
    async with transaction() as db:
        await service.cancel(db, job_id=job_id)
    async with transaction() as db:
        again = await service.cancel(db, job_id=job_id)
    assert again.cancelled is False


# ── status ──────────────────────────────────────────────────────────


async def test_status_dem_theo_hang_doi_that(wired) -> None:
    job_id = await _running_job(3)
    async with transaction() as db:
        await service.pump(db, job_id=job_id, limit=2)
    await run_sql("UPDATE outbox_messages SET state = 1 WHERE outbox_id = 1")
    await run_sql("UPDATE outbox_messages SET state = 2 WHERE outbox_id = 2")
    await run_sql("UPDATE users SET blocked_at = now() WHERE user_id = 3001")

    async with db_session() as db:
        snapshot = await service.status(db, job_id)

    assert snapshot is not None
    assert snapshot.total == 4
    assert snapshot.queued == 2
    assert snapshot.pending == 2
    assert snapshot.sent == 1
    assert snapshot.failed == 1
    assert snapshot.blocked == 1
    assert snapshot.remaining == 2  # 2 chưa bàn giao + 0 còn chờ trong outbox


async def test_status_dot_khong_ton_tai(wired) -> None:
    async with db_session() as db:
        assert await service.status(db, 999_999) is None


# ── /broadcast ──────────────────────────────────────────────────────


def test_parse_args_mac_dinh_la_te_hep() -> None:
    parsed = handler.parse_broadcast_args(["xin", "chao"])
    assert parsed.audience == "active_30d"
    assert parsed.content == "xin chao"
    assert parsed.force_small is False


def test_parse_args_co_all_va_force_small() -> None:
    parsed = handler.parse_broadcast_args(["--all", "xin", "chao", "--force-small"])
    assert parsed.audience == "all"
    assert parsed.force_small is True
    assert parsed.content == "xin chao"


def test_parse_args_tu_choi_co_la() -> None:
    with pytest.raises(handler.BroadcastArgError):
        handler.parse_broadcast_args(["--al", "xin chao"])
    with pytest.raises(handler.BroadcastArgError):
        handler.parse_broadcast_args(["--all"])


async def test_broadcast_khong_co_all_thi_dung_active_30d(wired) -> None:
    await make_user(4_001, active_days_ago=1)
    await make_user(4_002, active_days_ago=90)
    await set_min_audience(1)

    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "xin", "chao"))

    row = (await fetch_all("SELECT job_id, audience, state, total FROM broadcast_jobs"))[0]
    assert row.audience == "active_30d"
    assert row.state == "draft"  # gõ lệnh KHÔNG gửi gì cả
    assert row.total == 2  # người mới + OWNER_ID, KHÔNG có người 90 ngày
    assert "XEM THỬ" in sender.last
    assert sender.markups[-1] is not None
    assert await scalar("SELECT count(*) FROM outbox_messages") == 0


async def test_broadcast_co_all_thi_ban_toan_te(wired) -> None:
    await make_user(4_101, active_days_ago=1)
    await make_user(4_102, active_days_ago=90)
    await set_min_audience(1)

    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "--all", "xin", "chao"))

    row = (await fetch_all("SELECT audience, total FROM broadcast_jobs"))[0]
    assert row.audience == "all"
    assert row.total == 3


async def test_broadcast_duoi_nguong_bi_tu_choi(wired) -> None:
    await make_user(4_201, active_days_ago=1)
    await set_min_audience(50)

    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "xin chao"))

    assert "TỪ CHỐI" in sender.last
    assert sender.markups[-1] is None  # không có nút GỬI NGAY nào để bấm nhầm
    row = (await fetch_all("SELECT state FROM broadcast_jobs"))[0]
    assert row.state == "cancelled"


async def test_broadcast_force_small_vuot_nguong(wired) -> None:
    await make_user(4_301, active_days_ago=1)
    await set_min_audience(50)

    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "xin chao", "--force-small"))

    assert "XEM THỬ" in sender.last
    row = (await fetch_all("SELECT state FROM broadcast_jobs"))[0]
    assert row.state == "draft"


async def test_broadcast_thieu_noi_dung(wired) -> None:
    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender))
    assert "Cú pháp" in sender.last
    assert await scalar("SELECT count(*) FROM broadcast_jobs") == 0


# ── Nút xác nhận ────────────────────────────────────────────────────


@pytest.fixture
def khong_bom(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Chặn vòng bơm nền và ghi lại đợt nào đã yêu cầu bơm.

    Test không có worker outbox, và một task nền sống lâu hơn fixture sẽ chạm vào engine
    đã bị đóng — triệu chứng là một lỗi ở test KHÁC, chỗ không liên quan gì.
    """
    started: list[int] = []
    monkeypatch.setattr(service, "start_pump", lambda job_id, **_: started.append(job_id))
    return started


async def test_confirm_chay_dot_va_dung_vong_bom(wired, khong_bom) -> None:
    await make_user(5_001)
    await set_min_audience(1)
    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "xin chao"))
    job_id = (await fetch_all("SELECT job_id FROM broadcast_jobs"))[0].job_id

    await handler.handle_broadcast_callback(
        make_update(callback_data=f"bc_confirm_{job_id}"), make_context(sender)
    )

    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id})
        == "running"
    )
    assert khong_bom == [job_id]
    assert sender.answered, "callback phải được trả lời trong 2 giây"
    assert "BẮT ĐẦU CHẠY" in sender.last


async def test_confirm_lan_hai_khong_chay_lai(wired, khong_bom) -> None:
    await make_user(5_101)
    await set_min_audience(1)
    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "xin chao"))
    job_id = (await fetch_all("SELECT job_id FROM broadcast_jobs"))[0].job_id

    update = make_update(callback_data=f"bc_confirm_{job_id}")
    await handler.handle_broadcast_callback(update, make_context(sender))
    await handler.handle_broadcast_callback(update, make_context(sender))

    assert khong_bom == [job_id]
    assert "không còn ở trạng thái chờ xác nhận" in sender.last


async def test_nut_huy_thi_huy_dot(wired, khong_bom) -> None:
    await make_user(5_201)
    await set_min_audience(1)
    sender = FakeSender()
    await handler.cmd_broadcast(make_update(), make_context(sender, "xin chao"))
    job_id = (await fetch_all("SELECT job_id FROM broadcast_jobs"))[0].job_id

    await handler.handle_broadcast_callback(
        make_update(callback_data=f"bc_cancel_{job_id}"), make_context(sender)
    )

    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id})
        == "cancelled"
    )
    assert "ĐÃ HUỶ" in sender.last


# ── /broadcast_status · pause · resume · cancel ─────────────────────


async def test_status_khong_tham_so_lay_dot_dang_chay(wired) -> None:
    job_id = await _running_job(2)
    sender = FakeSender()
    await handler.cmd_broadcast_status(make_update(), make_context(sender))
    assert f"ĐỢT #{job_id}" in sender.last
    assert "đang chạy" in sender.last


async def test_status_khong_co_dot_nao(wired) -> None:
    sender = FakeSender()
    await handler.cmd_broadcast_status(make_update(), make_context(sender))
    assert "Không có đợt nào" in sender.last


async def test_pause_resume_qua_lenh(wired, khong_bom) -> None:
    job_id = await _running_job(2)
    sender = FakeSender()

    await handler.cmd_broadcast_pause(make_update(), make_context(sender, str(job_id)))
    assert "TẠM DỪNG" in sender.last
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id}) == (
        "paused"
    )

    await handler.cmd_broadcast_resume(make_update(), make_context(sender, str(job_id)))
    assert "CHẠY TIẾP" in sender.last
    assert khong_bom == [job_id]

    # Tạm dừng một đợt đang chạy tiếp thì được; tạm dừng hai lần liên tiếp thì không.
    await handler.cmd_broadcast_pause(make_update(), make_context(sender, str(job_id)))
    await handler.cmd_broadcast_pause(make_update(), make_context(sender, str(job_id)))
    assert "không đang chạy" in sender.last


async def test_cancel_qua_lenh(wired) -> None:
    job_id = await _running_job(2)
    sender = FakeSender()
    await handler.cmd_broadcast_cancel(make_update(), make_context(sender, str(job_id)))
    assert "ĐÃ HUỶ" in sender.last
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id})
        == "cancelled"
    )


# ── Trình bày ───────────────────────────────────────────────────────


def test_format_duration() -> None:
    assert handler.format_duration(45) == "45 giây"
    assert handler.format_duration(754) == "12 phút 34 giây"
    assert handler.format_duration(7_300) == "2 giờ 1 phút"


def test_uoc_tinh_theo_san_bulk() -> None:
    """Ước tính lấy SÀN 8 tin/giây, không lấy trần 30 — xem docstring `estimate_seconds`."""
    from televip.cache.ratelimit import BULK_FLOOR

    assert handler.estimate_seconds(800) == pytest.approx(800 / BULK_FLOOR)
