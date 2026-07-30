"""Event đập hộp — §13.2.9 và §13.5.1, chạy qua chính service/handler trên PostgreSQL thật.

Sáu mệnh đề bắt buộc, và năm trong sáu là một chỗ bot cũ đã sai:

- tổng trọng số bảng tỉ lệ phải đúng **10.000 bp** — lệch là từ chối, không "tự chuẩn hoá";
- **caption khớp tỉ lệ thật** — dựng caption rồi parse ngược, so từng con số với
  `settings.event.prize_table` (`test_caption_khop_ti_le_thuc` mà §13.5 dòng E gọi tên);
- mở hộp hai lần ⇒ lần hai bị chặn bởi PK `(user_id, event_id)`, không bởi một `if`;
- **cửa sổ tính theo `sent_at` của TỪNG người** — event tạo từ một giờ trước mà người này
  vừa nhận tin thì vẫn mở được; đây là lỗi làm ~94% người nhận của bot cũ bị ép hộp rỗng;
- chạm trần ngân sách ⇒ ngừng phát, và nói thẳng lý do (không mượn câu "hộp rỗng");
- thiếu mệnh giá trong kho ⇒ `/send_event` **từ chối chạy**.

⚠️ Giống `test_tanthu.py`: file này KHÔNG dùng fixture `db`/`seeded` của `conftest`.
Handler và service đọc ghi qua engine **toàn cục**, nên dựng dữ liệu bằng engine thứ hai là
biến thứ tự commit giữa hai bên thành một cuộc đua.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers import event_box as event_box_handler
from televip.apps.worker.handlers.admin import event as admin_event
from televip.core.errors import ConfigError
from televip.db.engine import session as db_session
from televip.db.engine import transaction
from televip.services import event_box
from tests.conftest import TEST_DATABASE_URL, _truncate_all, add_codes, make_user

ADMIN_ID = 8_100_001
GAME_LINK = "https://televip.game"
SUPPORT_LINK = "https://t.me/cskh"
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

#: Bảng tỉ lệ dùng cho test caption — cố ý KHÁC bộ seed (phương án B) để một caption viết
#: cứng không thể vô tình đi qua bài kiểm.
TABLE_SAU_MUC: list[dict[str, int]] = [
    {"value_vnd": 0, "weight_bp": 6_000},
    {"value_vnd": 5_000, "weight_bp": 2_500},
    {"value_vnd": 10_000, "weight_bp": 1_000},
    {"value_vnd": 20_000, "weight_bp": 400},
    {"value_vnd": 50_000, "weight_bp": 90},
    {"value_vnd": 88_000, "weight_bp": 10},
]

#: Bảng "chắc chắn trúng 5k" — làm nhánh trúng thành tất định mà không phải bơm rng giả.
TABLE_LUON_TRUNG_5K: list[dict[str, int]] = [{"value_vnd": 5_000, "weight_bp": 10_000}]

#: Bảng "chắc chắn rỗng".
TABLE_LUON_RONG: list[dict[str, int]] = [{"value_vnd": 0, "weight_bp": 10_000}]


def seed_prize_table() -> list[dict[str, int]]:
    """Bảng tỉ lệ **thật sự được seed** — đọc thẳng từ migration `0002`, không chép lại.

    Cùng đường đi với `test_settings.py`: tên file bắt đầu bằng chữ số nên phải nạp bằng
    `importlib`. Chép con số sang đây là dựng đúng cái nguồn sự thật thứ hai mà cả khối
    này sinh ra để xoá.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "televip"
        / "db"
        / "migrations"
        / "versions"
        / "0002_seed_config.py"
    )
    spec = importlib.util.spec_from_file_location("_seed_0002_event", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return next(row["value"] for row in module.SETTINGS_SEED if row["key"] == "event.prize_table")


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    def __init__(self, *, deliver: bool = True) -> None:
        self.messages: list[str] = []
        self.answers: list[str] = []
        self.deliver = deliver

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return len(self.messages) if self.deliver else None

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

        await set_setting("link.game_bot", GAME_LINK)
        await set_setting("link.support", SUPPORT_LINK)
        await set_setting("webapp.url", VERIFY_URL)
        await set_setting("event.window_minutes", 10, "int")
        await set_setting("event.budget_cap_vnd", 12_000_000, "money_vnd")
        await set_setting("event.require_full_stock", True, "bool")
        await set_setting("event.prize_table", TABLE_SAU_MUC, "json")

        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()
        text_service.invalidate()
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


async def make_player(user_id: int, *, verified: bool = True) -> int:
    async with db_session() as s:
        await make_user(s, user_id)
    await run_sql(
        """
        UPDATE users
           SET started_bot_at = now(),
               last_active = now(),
               verified_at = CASE WHEN :v THEN now() ELSE NULL END
         WHERE user_id = :uid
        """,
        {"uid": user_id, "v": verified},
    )
    return user_id


async def make_event(*, created_by: int = ADMIN_ID, caption: str = "test") -> int:
    async with transaction() as db:
        return await event_box.create_event(db, created_by=created_by, caption=caption)


async def age_event(event_id: int, *, minutes: int) -> None:
    """Đẩy `events.created_at` lùi lại — mô phỏng đợt tạo từ lâu."""
    await run_sql(
        "UPDATE events SET created_at = now() - make_interval(mins => :m) WHERE event_id = :eid",
        {"eid": event_id, "m": minutes},
    )


async def attach_delivery(event_id: int, user_id: int, *, sent_minutes_ago: int) -> int:
    """Gắn một đợt bắn tin có `broadcast_targets.sent_at` cho đúng người này."""
    async with transaction() as s:
        job_id = int(
            (
                await s.execute(
                    text("""
                    INSERT INTO broadcast_jobs (kind, audience, payload, state, created_by)
                         VALUES ('send_event', 'all', '{}'::jsonb, 'running', :by)
                      RETURNING job_id
                    """),
                    {"by": ADMIN_ID},
                )
            ).scalar_one()
        )
    await run_sql(
        """
        INSERT INTO broadcast_targets (job_id, user_id, state, sent_at)
             VALUES (:job, :uid, 1, now() - make_interval(mins => :m))
        """,
        {"job": job_id, "uid": user_id, "m": sent_minutes_ago},
    )
    async with transaction() as db:
        await event_box.attach_job(db, event_id=event_id, job_id=job_id)
    return job_id


async def open_box(event_id: int, user_id: int) -> event_box.BoxResult:
    async with transaction() as db:
        return await event_box.open_box(db, event_id=event_id, user_id=user_id)


async def participation(event_id: int, user_id: int) -> Any:
    async with db_session() as s:
        return (
            await s.execute(
                text("""
                SELECT result, window_source, code_grant_id
                  FROM event_participations
                 WHERE event_id = :eid AND user_id = :uid
                """),
                {"eid": event_id, "uid": user_id},
            )
        ).one_or_none()


async def grant_admin(user_id: int, *, commands: tuple[str, ...] = ("/send_event",)) -> int:
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, user_id)
    await run_sql(
        """
        INSERT INTO admin_users (user_id, role, added_by) VALUES (:uid, 'owner', :uid)
        ON CONFLICT (user_id) DO UPDATE SET role = 'owner', revoked_at = NULL
        """,
        {"uid": user_id},
    )
    for command in commands:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :cmd) "
            "ON CONFLICT DO NOTHING",
            {"cmd": command},
        )
    admin_service.invalidate_role(user_id)
    return user_id


# ══════════════════════════════════════════════════════════════════════════════
# 1. Bảng tỉ lệ — một bảng, tổng đúng 10.000 bp
# ══════════════════════════════════════════════════════════════════════════════


def test_tong_trong_so_phai_dung_10000() -> None:
    assert sum(p.weight_bp for p in event_box.parse_prize_table(TABLE_SAU_MUC)) == 10_000

    thieu = [{"value_vnd": 0, "weight_bp": 9_999}]
    thua = [{"value_vnd": 0, "weight_bp": 5_000}, {"value_vnd": 5_000, "weight_bp": 5_001}]
    for bad in (thieu, thua):
        with pytest.raises(ConfigError, match="10000|10.000|trọng số"):
            event_box.parse_prize_table(bad)


def test_bo_ti_le_seed_di_qua_duoc_bo_kiem() -> None:
    """Bộ seed (phương án B, §13.5.1) phải hợp lệ với đúng bộ kiểm của service.

    Không chỉ kiểm tổng: nếu ai đó seed một mệnh giá mà `event_participations.result`
    không ghi được thì lỗi chỉ lộ ra lúc có người trúng — tức lúc muộn nhất có thể.
    """
    prizes = event_box.parse_prize_table(seed_prize_table())
    assert sum(p.weight_bp for p in prizes) == 10_000
    # §13.5.1 chốt phương án B ở 1.578đ/lượt. Lệch là ai đó đã đổi seed mà không đổi kế hoạch.
    assert round(event_box.expected_cost_vnd(prizes)) == 1_578


def test_menh_gia_khong_ghi_duoc_vao_so_thi_bi_tu_choi() -> None:
    """`event_participations.result` chỉ nhận 5k/10k/20k/50k/88k — chặn ở tầng cấu hình."""
    with pytest.raises(ConfigError, match="30000"):
        event_box.parse_prize_table(
            [{"value_vnd": 0, "weight_bp": 7_000}, {"value_vnd": 30_000, "weight_bp": 3_000}]
        )


def test_quay_so_di_dung_bien_trong_so() -> None:
    prizes = event_box.parse_prize_table(TABLE_SAU_MUC)
    # Biên: vé cuối cùng của mức trước và vé đầu tiên của mức sau.
    assert event_box.draw(prizes, roll=lambda _: 0).value_vnd == 0
    assert event_box.draw(prizes, roll=lambda _: 5_999).value_vnd == 0
    assert event_box.draw(prizes, roll=lambda _: 6_000).value_vnd == 5_000
    assert event_box.draw(prizes, roll=lambda _: 9_989).value_vnd == 50_000
    assert event_box.draw(prizes, roll=lambda _: 9_990).value_vnd == 88_000
    assert event_box.draw(prizes, roll=lambda _: 9_999).value_vnd == 88_000


def test_quay_so_dung_secrets_khong_dung_random() -> None:
    """Nguồn ngẫu nhiên dính tới tiền thì không được đoán trước được (§13.2.9 bước 4)."""
    source = inspect.getsource(event_box)
    assert "import secrets" in source
    assert "import random" not in source
    assert "random.random" not in source


def _co_bang_ti_le_viet_cung(module: Any) -> bool:
    """Có literal nào trong module trông như một mức của bảng tỉ lệ không.

    Bắt bằng AST chứ không bằng `in source`: một bảng viết cứng luôn hiện ra thành dict
    literal có khoá `weight_bp`, còn tên biến thì đặt kiểu gì cũng được. Đây là cái lưới
    chặn `EVENT_PROBABILITIES_DISPLAY` sống lại dưới một cái tên khác.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        # Một mức của bảng tỉ lệ luôn mang CẢ HAI khoá. Chỉ `weight_bp` thì có thể là một
        # payload ghi sổ (`reason={"event_id": …, "weight_bp": …}`), không phải bảng.
        if {"value_vnd", "weight_bp"} <= keys:
            return True
    return False


def test_khong_co_bang_ti_le_thu_hai_trong_code() -> None:
    """Bot cũ có `EVENT_PROBABILITIES_REAL` và `_DISPLAY`. Ở đây không được có bảng nào.

    Bảng tỉ lệ chỉ được sống ở `settings.event.prize_table`; mọi bản sao trong code — dù
    đặt tên gì — là nguồn sự thật thứ hai, và hai nguồn thì sớm muộn nói khác nhau.
    """
    from televip.domain import texts as domain_texts

    for module in (event_box, event_box_handler, admin_event, domain_texts):
        assert not _co_bang_ti_le_viet_cung(module), (
            f"{module.__name__} có bảng tỉ lệ viết cứng — chỉ settings.event.prize_table "
            f"được giữ nó"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Caption khớp tỉ lệ thật — bài kiểm §13.5 dòng E gọi đích danh
# ══════════════════════════════════════════════════════════════════════════════

_EMPTY_RE = re.compile(r"Hộp rỗng (\d+(?:\.\d+)?)%")
_PRIZE_RE = re.compile(r"🎲 (\d+)k = 🎯 (\d+(?:\.\d+)?)%")


def parse_caption(caption: str) -> dict[int, float]:
    """Caption → `{mệnh giá: phần trăm}`. Đọc ngược đúng thứ người dùng nhìn thấy."""
    out: dict[int, float] = {}
    for match in _EMPTY_RE.finditer(caption):
        out[0] = float(match.group(1))
    for match in _PRIZE_RE.finditer(caption):
        out[int(match.group(1)) * 1_000] = float(match.group(2))
    return out


@pytest.mark.asyncio
async def test_caption_khop_ti_le_thuc(wired):
    async with db_session() as db:
        caption = await event_box.render_caption(db)
        prizes = await event_box.prize_table(db)

    hien_thi = parse_caption(caption)
    that = {p.value_vnd: p.weight_bp / 100 for p in prizes}

    assert hien_thi == that, "caption in ra một bộ số khác bộ dùng để quay — lỗi của bot cũ"
    assert GAME_LINK in caption


@pytest.mark.asyncio
async def test_doi_bang_ti_le_thi_caption_doi_theo(wired):
    """Không có bảng hiển thị riêng: đổi cấu hình là caption đổi, không cần deploy."""
    await set_setting(
        "event.prize_table",
        [{"value_vnd": 0, "weight_bp": 9_500}, {"value_vnd": 88_000, "weight_bp": 500}],
        "json",
    )
    async with db_session() as db:
        caption = await event_box.render_caption(db)

    assert parse_caption(caption) == {0: 95.0, 88_000: 5.0}


# ══════════════════════════════════════════════════════════════════════════════
# 3. Mở hộp hai lần
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mo_hop_hai_lan_thi_lan_hai_bi_chan(wired):
    uid = await make_player(8_200_001)
    event_id = await make_event()
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=5)

    first = await open_box(event_id, uid)
    second = await open_box(event_id, uid)

    assert first.status == "win"
    assert second.status == "already", "PK (user_id, event_id) phải chặn lượt thứ hai"
    assert await scalar("SELECT count(*) FROM event_participations") == 1
    assert await scalar("SELECT count(*) FROM code_grants") == 1, "không được phát mã thứ hai"
    assert await scalar("SELECT count(*) FROM codes WHERE status <> 'available'") == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Cửa sổ tính theo `sent_at` của TỪNG người nhận
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ngoai_cua_so_thi_rong_va_khong_ton_ma(wired):
    uid = await make_player(8_300_001)
    event_id = await make_event()
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=5)
    await attach_delivery(event_id, uid, sent_minutes_ago=30)  # cửa sổ 10 phút

    result = await open_box(event_id, uid)

    assert result.status == "empty"
    assert result.reason == "window_closed"
    assert await scalar("SELECT count(*) FROM code_grants") == 0
    assert await scalar("SELECT count(*) FROM codes WHERE status <> 'available'") == 0

    row = await participation(event_id, uid)
    assert (row.result, row.window_source) == ("empty", "per_recipient")


@pytest.mark.asyncio
async def test_cua_so_tinh_tu_sent_at_chu_khong_tu_created_at(wired):
    """Đợt tạo từ MỘT GIỜ trước, nhưng người này vừa nhận tin ⇒ vẫn mở được.

    Đây là lỗi đắt nhất của bot cũ: nó đo từ `events.created_at` toàn cục trong khi giao
    tin mất hàng giờ, nên phần lớn người nhận bị ép hộp rỗng mà không ai biết vì sao.
    """
    uid = await make_player(8_300_002)
    event_id = await make_event()
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=5)
    await age_event(event_id, minutes=60)
    await attach_delivery(event_id, uid, sent_minutes_ago=1)

    result = await open_box(event_id, uid)

    assert result.status == "win", "cửa sổ phải tính từ lúc CHÍNH người này nhận tin"
    assert result.window_source == "per_recipient"
    assert (await participation(event_id, uid)).window_source == "per_recipient"


@pytest.mark.asyncio
async def test_chua_co_sent_at_thi_lui_ve_created_at_va_ghi_lai(wired):
    uid = await make_player(8_300_003)
    event_id = await make_event()
    await set_setting("event.prize_table", TABLE_LUON_RONG, "json")

    result = await open_box(event_id, uid)

    assert result.status == "empty"
    assert result.window_source == "event_created"
    assert (await participation(event_id, uid)).window_source == "event_created"


@pytest.mark.asyncio
async def test_event_da_dong_thi_khong_ghi_gi(wired):
    uid = await make_player(8_300_004)
    event_id = await make_event()
    async with transaction() as db:
        await event_box.close_event(db, event_id=event_id)

    result = await open_box(event_id, uid)

    assert result.status == "closed"
    assert await scalar("SELECT count(*) FROM event_participations") == 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Trần ngân sách
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cham_tran_ngan_sach_thi_dung_phat(wired):
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    await set_setting("event.budget_cap_vnd", 5_000, "money_vnd")
    event_id = await make_event()
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=10)

    winner = await make_player(8_400_001)
    latecomer = await make_player(8_400_002)

    first = await open_box(event_id, winner)
    second = await open_box(event_id, latecomer)

    assert first.status == "win"
    assert second.status == "empty"
    assert second.reason == "budget_cap", "chạm trần phải nói rõ lý do, không mượn 'hộp rỗng'"
    assert await scalar("SELECT count(*) FROM code_grants") == 1, "quá trần thì ngừng tiêu tiền"

    async with db_session() as db:
        assert await event_box.spent_vnd(db, event_id=event_id) == 5_000


@pytest.mark.asyncio
async def test_cham_tran_gui_cau_khac_han_hop_rong(wired):
    from televip.services import text_service

    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    await set_setting("event.budget_cap_vnd", 0, "money_vnd")
    event_id = await make_event()
    uid = await make_player(8_400_003)

    sender = FakeSender()
    await event_box_handler.handle_open_box(
        make_update(uid, callback_data=f"dap_hop_{event_id}"), make_context(sender)
    )

    hop_rong = await text_service.render("event.box_empty", game_link=GAME_LINK)
    assert sender.last != hop_rong
    assert sender.last == await text_service.render("event.box_budget_capped", game_link=GAME_LINK)


# ══════════════════════════════════════════════════════════════════════════════
# 6. Hết kho đúng mệnh giá vừa trúng
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_het_kho_thi_khong_doi_sang_menh_gia_khac(wired):
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    event_id = await make_event()
    uid = await make_player(8_500_001)
    # Kho có 10K nhưng KHÔNG có 5K — mức vừa trúng.
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=10_000, count=5)

    result = await open_box(event_id, uid)

    assert result.status == "out_of_stock"
    assert result.value_vnd == 5_000
    assert await scalar("SELECT count(*) FROM codes WHERE status <> 'available'") == 0
    assert await scalar("SELECT count(*) FROM code_grants") == 0, (
        "SAVEPOINT phải cuộn lại dòng grant rỗng của reserve()"
    )

    row = await participation(event_id, uid)
    assert (row.result, row.code_grant_id) == ("out_of_stock", None)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Callback `dap_hop_<id>` — qua chính handler
# ══════════════════════════════════════════════════════════════════════════════


def test_doc_event_id_tu_callback_data() -> None:
    assert event_box_handler.parse_event_id("dap_hop_12") == 12
    assert event_box_handler.parse_event_id("dap_hop_") is None
    assert event_box_handler.parse_event_id("dap_hop_abc") is None
    assert event_box_handler.parse_event_id(None) is None


@pytest.mark.asyncio
async def test_callback_tra_ma_va_ghi_so(wired):
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    event_id = await make_event()
    uid = await make_player(8_600_001)
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=3)

    sender = FakeSender()
    await event_box_handler.handle_open_box(
        make_update(uid, callback_data=f"dap_hop_{event_id}"), make_context(sender)
    )

    assert sender.answers, "callback phải được answer ngay (§13.3.3)"
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

    assert row.state == "delivered", "mark_delivered() chỉ chạy sau khi gửi thành công"
    assert row.status == "issued"
    assert row.grant_key == f"event:{event_id}:{uid}"
    assert row.value_vnd == 5_000
    assert row.code_value in sender.last


@pytest.mark.asyncio
async def test_chua_xac_minh_thi_khong_mo_duoc_hop(wired):
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    event_id = await make_event()
    uid = await make_player(8_600_002, verified=False)
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=3)

    sender = FakeSender()
    await event_box_handler.handle_open_box(
        make_update(uid, callback_data=f"dap_hop_{event_id}"), make_context(sender)
    )

    assert await scalar("SELECT count(*) FROM event_participations") == 0
    assert await scalar("SELECT count(*) FROM code_grants") == 0
    assert sender.answers, "nhánh từ chối cũng phải answer callback"


@pytest.mark.asyncio
async def test_gui_that_bai_thi_khong_danh_dau_da_giao(wired):
    """Người đã chặn bot: mã nằm lại `reserved` cho `reap_reservations()` thu về."""
    await set_setting("event.prize_table", TABLE_LUON_TRUNG_5K, "json")
    event_id = await make_event()
    uid = await make_player(8_600_003)
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=3)

    sender = FakeSender(deliver=False)
    await event_box_handler.handle_open_box(
        make_update(uid, callback_data=f"dap_hop_{event_id}"), make_context(sender)
    )

    assert await scalar("SELECT state FROM code_grants WHERE user_id = :uid", {"uid": uid}) == (
        "reserved"
    )
    assert await scalar("SELECT count(*) FROM code_ledger") == 0


# ══════════════════════════════════════════════════════════════════════════════
# 8. `/send_event`
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_send_event_tu_choi_khi_thieu_menh_gia(wired):
    """§13.5.1 điều kiện 1: quảng cáo giải mà kho rỗng là lừa người dùng."""
    admin = await grant_admin(ADMIN_ID)
    await make_player(8_700_001)
    # Kho chỉ có 5K; bảng tỉ lệ quảng cáo cả sáu mức.
    async with db_session() as s:
        await add_codes(s, code_type="event", value_vnd=5_000, count=10)

    sender = FakeSender()
    await admin_event.cmd_send_event(make_update(admin), make_context(sender))

    assert "TỪ CHỐI" in sender.last
    for label in ("10K", "20K", "50K", "88K"):
        assert label in sender.last
    assert await scalar("SELECT count(*) FROM events") == 0, "từ chối thì không tạo event"
    assert await scalar("SELECT count(*) FROM broadcast_jobs") == 0


@pytest.mark.asyncio
async def test_send_event_du_kho_thi_tao_event_va_job_draft(wired):
    admin = await grant_admin(ADMIN_ID)
    await make_player(8_700_002)
    async with db_session() as s:
        for value in (5_000, 10_000, 20_000, 50_000, 88_000):
            await add_codes(s, code_type="event", value_vnd=value, count=3, prefix=f"E{value}")

    sender = FakeSender()
    await admin_event.cmd_send_event(
        make_update(admin), make_context(sender, "Giờ", "vàng", "21h!")
    )

    assert "XEM THỬ EVENT" in sender.last
    assert "Giờ vàng 21h!" in sender.last, "lời dẫn của admin phải hiện trong bản xem thử"
    assert "Xác suất mở hộp" in sender.last, "khối tỉ lệ LUÔN đi kèm, không tắt được"

    async with db_session() as s:
        row = (
            await s.execute(
                text("""
                SELECT e.event_id, e.caption, e.job_id, e.is_active, j.state, j.kind, j.payload
                  FROM events e JOIN broadcast_jobs j ON j.job_id = e.job_id
                """)
            )
        ).one()

    assert row.state == "draft", "gõ lệnh KHÔNG được gửi gì cả"
    assert row.kind == "send_event"
    assert row.is_active is True
    assert parse_caption(row.caption) == {
        p["value_vnd"]: p["weight_bp"] / 100 for p in TABLE_SAU_MUC
    }
    assert row.payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        f"dap_hop_{row.event_id}"
    )


@pytest.mark.asyncio
async def test_send_event_tat_kiem_kho_thi_van_chay(wired):
    """`event.require_full_stock = false` là một quyết định có audit, không phải mặc định."""
    admin = await grant_admin(ADMIN_ID)
    await make_player(8_700_003)
    await set_setting("event.require_full_stock", False, "bool")

    sender = FakeSender()
    await admin_event.cmd_send_event(make_update(admin), make_context(sender))

    assert "XEM THỬ EVENT" in sender.last
    assert await scalar("SELECT count(*) FROM events") == 1


@pytest.mark.asyncio
async def test_send_event_bang_ti_le_hong_thi_tu_choi(wired):
    admin = await grant_admin(ADMIN_ID)
    await make_player(8_700_004)
    await set_setting("event.prize_table", [{"value_vnd": 0, "weight_bp": 9_000}], "json")

    sender = FakeSender()
    await admin_event.cmd_send_event(make_update(admin), make_context(sender))

    assert "TỪ CHỐI" in sender.last
    assert await scalar("SELECT count(*) FROM events") == 0


@pytest.mark.asyncio
async def test_send_event_khong_co_quyen_thi_im_lang(wired):
    outsider = await make_player(8_700_005)

    sender = FakeSender()
    await admin_event.cmd_send_event(make_update(outsider), make_context(sender))

    assert sender.messages == [], "từ chối quyền phải IM LẶNG (§13.4.1)"
    assert await scalar("SELECT count(*) FROM events") == 0
    assert (
        await scalar(
            "SELECT count(*) FROM audit_log WHERE action = :a", {"a": "/send_event.denied"}
        )
        == 1
    ), "im lặng với người gõ, nhưng phải để lại dấu vết trong audit_log"


@pytest.mark.asyncio
async def test_nut_xac_nhan_moi_bat_dau_ban(wired):
    admin = await grant_admin(ADMIN_ID)
    await make_player(8_700_006)
    async with db_session() as s:
        for value in (5_000, 10_000, 20_000, 50_000, 88_000):
            await add_codes(s, code_type="event", value_vnd=value, count=3, prefix=f"F{value}")

    sender = FakeSender()
    await admin_event.cmd_send_event(make_update(admin), make_context(sender))
    job_id = int(await scalar("SELECT job_id FROM broadcast_jobs"))

    update = make_update(admin, callback_data=f"{admin_event.CB_CONFIRM_PREFIX}{job_id}")
    await admin_event.handle_send_event_callback(update, make_context(sender))

    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id}) == (
        "running"
    )
    assert "BẮT ĐẦU BẮN" in sender.last


@pytest.mark.asyncio
async def test_nut_huy_dong_luon_event(wired):
    admin = await grant_admin(ADMIN_ID)
    await make_player(8_700_007)
    async with db_session() as s:
        for value in (5_000, 10_000, 20_000, 50_000, 88_000):
            await add_codes(s, code_type="event", value_vnd=value, count=3, prefix=f"G{value}")

    sender = FakeSender()
    await admin_event.cmd_send_event(make_update(admin), make_context(sender))
    job_id = int(await scalar("SELECT job_id FROM broadcast_jobs"))

    update = make_update(admin, callback_data=f"{admin_event.CB_CANCEL_PREFIX}{job_id}")
    await admin_event.handle_send_event_callback(update, make_context(sender))

    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job_id}) == (
        "cancelled"
    )
    assert await scalar("SELECT is_active FROM events") is False
