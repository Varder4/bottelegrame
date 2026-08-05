"""Sơ đồ đấu nối handler — `apps/worker/main.py`.

Đăng ký handler là chỗ lỗi không bao giờ hiện ra thành một traceback: nút vẫn bấm được,
bot vẫn chạy, chỉ là **không có gì xảy ra**. Bốn bất biến dưới đây là bốn cách nó hỏng
trong im lặng:

1. Một nút trên bàn phím chính không có dòng nào trong bảng định tuyến.
2. Một nút được nối bằng so khớp lỏng, nên nó nuốt cả tin nhắn khác (`"Game" in text` của
   bot cũ khiến mọi tin chứa chữ "Game" rơi vào handler chơi game).
3. Lưới an toàn callback không đứng cuối, nên nó nuốt hết callback của các nút thật.
4. Hai lệnh admin trùng tên: `python-telegram-bot` giao update cho cái đăng ký trước, và
   cái sau không bao giờ chạy.

Phần đấu nối không cần database hay token — nó gọi thẳng `register_handlers()` với một
`Application` giả chỉ biết ghi lại handler nhận được. Phần job định kỳ ở cuối file có chạm
database, vì chu kỳ của job đọc từ bảng `settings`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from telegram.ext import CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler

from televip.apps.worker import main
from televip.db.engine import session as db_session
from televip.telegram import keyboards
from tests.conftest import TEST_DATABASE_URL, _truncate_all


class FakeApp:
    """`Application` rút gọn: `register_handlers()` chỉ dùng đúng `add_handler`."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)


@pytest.fixture
def wired_app() -> FakeApp:
    app = FakeApp()
    main.register_handlers(app)  # type: ignore[arg-type]
    return app


def _of_type(app: FakeApp, kind: type) -> list[Any]:
    return [h for h in app.handlers if isinstance(h, kind)]


# ── Bảng định tuyến ─────────────────────────────────────────────────


def test_bang_route_phu_dung_tap_route_cua_keyboards() -> None:
    """Thừa hay thiếu một dòng đều là một nút chết hoặc một handler không ai gọi."""
    assert set(main.ROUTE_HANDLERS) == set(keyboards.ROUTE_TABLE.values())


def test_moi_nhan_nut_co_dung_mot_message_handler_khop_tuyet_doi(wired_app: FakeApp) -> None:
    message_handlers = _of_type(wired_app, MessageHandler)
    assert len(message_handlers) == len(keyboards.ROUTE_TABLE)

    matched: list[str] = []
    for handler in message_handlers:
        merged = handler.filters
        # `filters.ChatType.PRIVATE & filters.Text([...])`: chỉ khớp chat riêng, và khớp
        # BẰNG NHAU với đúng một nhãn — không phải `in text`.
        assert isinstance(merged.and_filter.strings, list | tuple)
        assert len(merged.and_filter.strings) == 1
        matched.append(merged.and_filter.strings[0])

    assert sorted(matched) == sorted(keyboards.ROUTE_TABLE)


def test_khong_con_nut_nao_chua_noi_duoc(wired_app: FakeApp) -> None:
    """Mọi `"module:hàm"` trong hai bảng phải phân giải được.

    Đây là bài kiểm giữ cho bảng đấu nối và các module handler không trôi khỏi nhau: đổi
    tên một hàm mà quên sửa bảng thì nút đó lặng lẽ rơi vào lưới an toàn.
    """
    assert main.unresolved_targets() == []


# ── Callback ────────────────────────────────────────────────────────


def test_moi_mau_callback_deu_neo_hai_dau() -> None:
    for pattern, target in main.CALLBACK_HANDLERS:
        assert pattern.startswith("^"), target
        assert pattern.endswith("$"), target


def test_moi_callback_data_cua_keyboards_deu_co_handler(wired_app: FakeApp) -> None:
    """Mọi nút inline `keyboards` sinh ra phải có handler riêng, không rơi vào lưới."""
    patterns = [h.pattern for h in _of_type(wired_app, CallbackQueryHandler) if h.pattern]
    for data in (
        keyboards.CB_OPEN_GIFT,
        keyboards.CB_CHECK_GROUPS,
        keyboards.CB_JOIN_REFERRAL,
        keyboards.CB_LB_TODAY,
        keyboards.CB_LB_ALLTIME,
        keyboards.CB_NOOP,
        f"{keyboards.CB_OPEN_BOX_PREFIX}12",
        f"{keyboards.CB_REDEEM_PREFIX}10000",
    ):
        assert any(p.match(data) for p in patterns), data


def test_luoi_an_toan_callback_dung_cuoi_cung(wired_app: FakeApp) -> None:
    last = wired_app.handlers[-1]
    assert isinstance(last, CallbackQueryHandler)
    assert last.pattern is None  # bắt mọi callback còn lại
    assert last.callback is main._answer_unhandled_callback

    # ...và nó là handler KHÔNG pattern duy nhất: một cái thứ hai đứng trước sẽ nuốt hết.
    catch_all = [h for h in _of_type(wired_app, CallbackQueryHandler) if h.pattern is None]
    assert len(catch_all) == 1


def test_cap_nhat_thanh_vien_nhom_van_duoc_dang_ky(wired_app: FakeApp) -> None:
    """Nguồn duy nhất nuôi `group_memberships`; thiếu nó là bảng đóng băng vĩnh viễn."""
    assert len(_of_type(wired_app, ChatMemberHandler)) == 1


# ── Lệnh ────────────────────────────────────────────────────────────


def test_start_luon_duoc_dang_ky(wired_app: FakeApp) -> None:
    commands = {name for h in _of_type(wired_app, CommandHandler) for name in h.commands}
    assert "start" in commands


def test_lenh_admin_khong_trung_ten() -> None:
    names = [name for name, _ in main.admin_command_handlers()]
    assert len(names) == len(set(names)), sorted(names)


def test_vong_quet_nhat_duoc_lenh_cua_module_admin_moi() -> None:
    """Module admin xuất `COMMANDS`/`HANDLERS` là tự vào bot, không cần sửa `main.py`."""
    names = {name for name, _ in main.admin_command_handlers()}
    declared = {name for name, _ in main.ADMIN_COMMANDS}
    assert declared <= names
    # `/cauhinh` và `/broadcast` đến từ bảng tường minh; `/send_event` là module xuất bảng.
    assert {"cauhinh", "broadcast"} <= names


# ── Job định kỳ ─────────────────────────────────────────────────────


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, float, Any]] = []

    def run_repeating(self, callback: Any, *, interval: float, first: float, name: str) -> None:
        # Bất biến thật là "không job nào chạy NGAY lúc khởi động": lúc đó tiến trình cần
        # phục vụ update, không phải chạy vài truy vấn tổng hợp toàn bảng.
        #
        # Cố ý KHÔNG đòi `first == interval`. Job phát mốc mời bạn chạy sớm hơn chu kỳ
        # (first=30, interval=60) vì đó là phần thưởng người dùng đang chờ, và job làm mới
        # `user_stats` lệch vài giây so với job kia để hai truy vấn nặng không đụng nhau.
        assert first > 0, f"job {name!r} chạy ngay lúc khởi động"
        self.jobs.append((name, interval, callback))


@pytest_asyncio.fixture
async def wired_db():
    from televip.db import engine as db_engine
    from televip.services import settings_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    settings_service.invalidate()
    async with db_session() as s:
        await _truncate_all(s)
        await s.commit()
    try:
        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()


async def _set_setting(key: str, value: Any, value_type: str) -> None:
    async with db_session() as s:
        await s.execute(
            text("""
            INSERT INTO settings (key, value, value_type, label_vi)
                 VALUES (:k, CAST(:v AS jsonb), :t, :k)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """),
            {"k": key, "v": json.dumps(value), "t": value_type},
        )
        await s.commit()
    from televip.services import settings_service

    settings_service.invalidate()


async def test_job_dinh_ky_lay_chu_ky_tu_settings(wired_db) -> None:
    from televip.services import stats

    await _set_setting(stats.REFRESH_SECONDS_KEY, 17, "seconds")
    await _set_setting(main.REAP_SECONDS_KEY, 23, "seconds")

    queue = FakeJobQueue()
    await main.schedule_jobs(SimpleNamespace(job_queue=queue))  # type: ignore[arg-type]

    intervals = dict((name, interval) for name, interval, _ in queue.jobs)

    # Hai job này đọc chu kỳ từ `settings` — đó là điều đang được kiểm.
    assert intervals["refresh_system_stats"] == 17
    assert intervals["reap_reservations"] == 23
    assert intervals["refresh_user_stats"] == 17, "phải dùng cùng chu kỳ với thống kê hệ thống"

    # Sáu job phải đủ mặt. Thiếu `award_referral_tiers` thì mốc mời bạn không bao giờ được
    # phát (người được mời xác minh ở tiến trình web, việc gửi mã thuộc worker); thiếu
    # `refresh_user_stats` thì bảng xếp hạng toàn thời gian rỗng vĩnh viễn; thiếu
    # `bom_dot_bantin` thì một đợt do PANEL WEB bấm gửi sẽ đứng im — tệp đích đầy, không
    # một dòng outbox nào, không một tin nào bay, và không một dòng log lỗi nào — cho tới
    # lần khởi động lại kế tiếp, lúc đó cả đợt bỗng bắn đi.
    assert set(intervals) == {
        "refresh_system_stats",
        "reap_reservations",
        "award_referral_tiers",
        "refresh_user_stats",
        "bom_dot_bantin",
        "nap_anh",
    }
    # Chu kỳ bơm phải ≤ 60 giây: đó là trần đã có của panel ("hiệu lực cấu hình tối đa 60
    # giây"). Chu kỳ cao hơn nghĩa là một đợt chờ lâu hơn cả thời gian một khoá cấu hình
    # lan ra, và người vận hành sẽ kết luận đợt hỏng.
    assert intervals["bom_dot_bantin"] <= 60
    # Thiếu `nap_anh` thì một tấm ảnh panel vừa nhận nằm mãi trong `media_uploads` — người
    # vận hành thấy "đang tải lên…" vĩnh viễn và không có dòng lỗi nào.
    assert intervals["nap_anh"] <= 60


async def test_job_lam_moi_thong_ke_ghi_that_vao_bang(wired_db) -> None:
    await main.job_refresh_system_stats(None)  # type: ignore[arg-type]

    async with db_session() as s:
        assert (await s.execute(text("SELECT count(*) FROM system_stats"))).scalar_one() == 1


async def test_job_don_ma_giu_cho_chay_duoc_khi_khong_co_gi_de_don(wired_db) -> None:
    await main.job_reap_reservations(None)  # type: ignore[arg-type]


# ── Lệnh được cấp quyền phải có handler ─────────────────────────────

#: Lệnh đã có dòng quyền trong migration `0003` nhưng **chưa được xây**. Danh sách này là
#: một bản khai nợ, không phải một chỗ để giấu lệnh hỏng: `/help_admin` sinh từ
#: `admin_permissions`, nên mọi lệnh ở đây đang được quảng cáo với admin trong khi gõ vào
#: thì không có gì xảy ra. Xây xong một lệnh thì XOÁ nó khỏi đây — nếu quên xoá, bài kiểm
#: dưới sẽ đỏ và nhắc.
CHUA_XAY: frozenset[str] = frozenset({})


def _lenh_duoc_cap_quyen() -> set[str]:
    """Mọi lệnh được cấp quyền, gom từ TẤT CẢ migration.

    Không đọc từ database: bảng `admin_permissions` bị `TRUNCATE` giữa các test, nên đọc
    ở đó thì kết quả phụ thuộc bài nào chạy trước. Không chỉ đọc `0003`: `0005` nạp quyền
    của bốn lệnh câu chữ và `0006` nạp `/broadcast_cancel` — bỏ sót chúng thì bài kiểm này
    tố cáo nhầm những lệnh hoàn toàn lành lặn.
    """
    import re
    from pathlib import Path

    versions = Path(main.__file__).resolve().parents[3] / "televip" / "db" / "migrations"
    lenh: set[str] = set()
    for path in sorted((versions / "versions").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "admin_permissions" not in src:
            continue
        lenh |= set(re.findall(r"""["'](/[a-z_]+)["']""", src))
    return lenh


def test_moi_lenh_duoc_cap_quyen_deu_co_handler() -> None:
    """Quyền có mà handler không có = một lệnh chết được quảng cáo là đang sống.

    Đây là lỗi không bao giờ thành traceback: admin gõ lệnh, Telegram không giao cho ai,
    bot im lặng. Người gõ kết luận "bot hỏng" và đi sửa thẳng database — đúng thứ mà cả
    khối lệnh admin được viết ra để không còn phải làm.
    """
    co_handler = {f"/{name}" for name, _ in main.admin_command_handlers()}
    thieu = _lenh_duoc_cap_quyen() - co_handler - CHUA_XAY
    assert thieu == set(), f"có quyền nhưng không có handler: {sorted(thieu)}"


def test_ban_khai_no_khong_con_thua_dong_nao() -> None:
    """Xây xong mà quên xoá khỏi `CHUA_XAY` thì bài kiểm trên ngừng bảo vệ lệnh đó."""
    co_handler = {f"/{name}" for name, _ in main.admin_command_handlers()}
    da_xay = CHUA_XAY & co_handler
    assert da_xay == set(), f"đã có handler, phải xoá khỏi CHUA_XAY: {sorted(da_xay)}"


def test_khong_handler_nao_chay_ma_khong_co_quyen() -> None:
    """Chiều ngược lại: handler không có dòng quyền thì KHÔNG AI gõ được, kể cả owner."""
    co_handler = {f"/{name}" for name, _ in main.admin_command_handlers()}
    thua = co_handler - _lenh_duoc_cap_quyen()
    assert thua == set(), f"có handler nhưng không ai được cấp quyền: {sorted(thua)}"


# ── Menu lệnh riêng của từng admin ──────────────────────────────────
#
# Ghi chú của `PUBLIC_COMMANDS` hứa "lệnh admin chỉ hiện với từng admin qua
# `BotCommandScopeChat`", nhưng suốt một thời gian **không có dòng code nào làm việc đó**:
# menu của admin chỉ có /start và /help, còn 33 lệnh vận hành thì phải thuộc lòng mới gõ
# được. Một lệnh không ai tìm thấy thì cũng như chưa xây.


class FakeBot:
    """Ghi lại mọi lời gọi `set_my_commands`, kèm scope."""

    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        self.calls: list[tuple[Any, list[Any]]] = []
        self.fail_for = fail_for or set()

    async def set_my_commands(self, commands: Any, scope: Any = None) -> None:
        chat_id = getattr(scope, "chat_id", None)
        if chat_id in self.fail_for:
            from telegram.error import BadRequest

            raise BadRequest("chat not found")
        self.calls.append((scope, list(commands)))


def _menu_for(bot: FakeBot, user_id: int) -> list[str]:
    for scope, cmds in bot.calls:
        if getattr(scope, "chat_id", None) == user_id:
            return [c.command for c in cmds]
    return []


async def _them_admin(user_id: int, role: str, commands: tuple[str, ...]) -> None:
    async with db_session() as s:
        await s.execute(
            text("INSERT INTO users (user_id, username) VALUES (:u, :n) ON CONFLICT DO NOTHING"),
            {"u": user_id, "n": f"u{user_id}"},
        )
        await s.execute(
            text(
                "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, :r, :u) "
                "ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL"
            ),
            {"u": user_id, "r": role},
        )
        for cmd in commands:
            await s.execute(
                text(
                    "INSERT INTO admin_permissions (role, command) VALUES (:r, :c) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"r": role, "c": cmd},
            )
        await s.commit()


async def test_menu_admin_sinh_tu_quyen_that(wired_db) -> None:
    """Menu đọc từ `admin_permissions` — cùng nguồn với `/help_admin`, nên không trôi khác."""
    await _them_admin(970_001, "owner", ("/stats", "/baocao", "/del_all_code"))
    await _them_admin(970_002, "cskh", ("/stats",))

    bot = FakeBot()
    so = await main.refresh_admin_menus(SimpleNamespace(bot=bot))  # type: ignore[arg-type]

    assert so == 2
    assert _menu_for(bot, 970_001) == ["baocao", "del_all_code", "stats"]
    assert _menu_for(bot, 970_002) == ["stats"], "cskh không được thấy lệnh của owner"


async def test_menu_admin_bo_qua_nguoi_da_thu_hoi_quyen(wired_db) -> None:
    await _them_admin(970_010, "owner", ("/stats",))
    async with db_session() as s:
        await s.execute(text("UPDATE admin_users SET revoked_at = now() WHERE user_id = 970010"))
        await s.commit()

    bot = FakeBot()
    assert await main.refresh_admin_menus(SimpleNamespace(bot=bot)) == 0  # type: ignore[arg-type]
    assert bot.calls == []


async def test_mot_admin_chua_start_khong_lam_hong_menu_nguoi_khac(wired_db) -> None:
    """Telegram từ chối đặt scope cho người chưa mở chat riêng. Đó là chuyện thường."""
    await _them_admin(970_020, "owner", ("/stats",))
    await _them_admin(970_021, "owner", ("/stats",))

    bot = FakeBot(fail_for={970_020})
    so = await main.refresh_admin_menus(SimpleNamespace(bot=bot))  # type: ignore[arg-type]

    assert so == 1, "một admin chưa /start làm cả vòng đặt menu dừng lại"
    assert _menu_for(bot, 970_021) == ["stats"]


async def test_menu_khong_bao_gio_co_mo_ta_rong(wired_db) -> None:
    """Telegram từ chối cả danh sách nếu một mô tả rỗng — lệnh lạ phải có đường lui."""
    await _them_admin(970_030, "owner", ("/stats", "/mot_lenh_chua_co_trong_bang"))

    bot = FakeBot()
    await main.refresh_admin_menus(SimpleNamespace(bot=bot))  # type: ignore[arg-type]

    _, cmds = bot.calls[-1]
    assert all(c.description for c in cmds), "mô tả rỗng làm Telegram từ chối CẢ menu"


def test_moi_lenh_co_handler_deu_co_mo_ta_cho_menu() -> None:
    """Thiếu dòng trong `COMMAND_SYNTAX` thì lệnh vào menu với mô tả là chính tên nó.

    Không hỏng, nhưng vô dụng — admin nhìn `del_all_code` mà không biết nó làm gì. Bài
    kiểm này giữ cho bảng cú pháp không tụt lại sau khi thêm lệnh mới.
    """
    from televip.apps.worker.handlers.admin import ops as admin_ops

    thieu = [
        name
        for name, _ in main.admin_command_handlers()
        if f"/{name}" not in admin_ops.COMMAND_SYNTAX
    ]
    assert thieu == [], f"lệnh chưa có mô tả trong COMMAND_SYNTAX: {sorted(thieu)}"


async def test_set_menu_cho_mot_nguoi_theo_quyen_hien_tai(wired_db) -> None:
    await _them_admin(970_040, "cskh", ("/stats", "/user"))

    bot = FakeBot()
    assert await main.set_menu_for_user(SimpleNamespace(bot=bot), 970_040) is True  # type: ignore[arg-type]
    assert _menu_for(bot, 970_040) == ["stats", "user"]


async def test_set_menu_cho_nguoi_khong_con_quyen_thi_XOA_menu(wired_db) -> None:
    """Người vừa bị thu hồi vẫn thấy nguyên menu cũ là menu đang quảng cáo thứ họ không dùng được."""
    await _them_admin(970_050, "owner", ("/stats", "/baocao"))
    async with db_session() as s:
        await s.execute(text("UPDATE admin_users SET revoked_at = now() WHERE user_id = 970050"))
        await s.commit()
    from televip.services import admin as admin_service

    admin_service.invalidate_role(970_050)

    bot = FakeBot()
    await main.set_menu_for_user(SimpleNamespace(bot=bot), 970_050)  # type: ignore[arg-type]

    assert _menu_for(bot, 970_050) == [], "menu phải rỗng — Telegram khi đó rơi về /start, /help"
    assert bot.calls, "phải GỌI set_my_commands với danh sách rỗng, không phải bỏ qua"


async def test_set_menu_nguoi_chua_start_thi_tra_False_chu_khong_nem(wired_db) -> None:
    await _them_admin(970_060, "owner", ("/stats",))

    bot = FakeBot(fail_for={970_060})
    assert await main.set_menu_for_user(SimpleNamespace(bot=bot), 970_060) is False  # type: ignore[arg-type]


# ── /start sau khi đã có hàng users ─────────────────────────────────


async def test_start_dien_started_bot_at_cho_hang_da_ton_tai(wired_db) -> None:
    """Mở Mini App trước rồi mới bấm `/start` — người này phải vào được tệp bắn tin.

    Mini App **cố ý** tạo hàng `users` mà không đặt `started_bot_at`: mở Mini App qua một
    link `t.me` không phải là cho phép bot nhắn tin, và đặt nhầm cột đó là đẩy người chưa
    /start vào danh sách broadcast rồi ăn 403 hàng loạt.

    Nhưng lần `/start` SAU đó thì phải điền. Bản trước chỉ đặt cột này ở nhánh `INSERT`,
    nên hàng đã tồn tại với `NULL` không bao giờ được sửa — và người đó bị loại khỏi MỌI
    đợt bắn tin vĩnh viễn. Hỏng im lặng: họ vẫn dùng bot bình thường, vẫn nhận code, chỉ
    là không bao giờ nghe được một thông báo nào.
    """
    from televip.db.engine import transaction
    from televip.services import users as users_service

    uid = 971_101
    # Đúng câu mà Mini App chạy: KHÔNG có `started_bot_at`.
    async with db_session() as s:
        await s.execute(
            text(
                "INSERT INTO users (user_id, username, full_name, last_active) "
                "VALUES (:u, 'tumini', 'Tu Mini App', now())"
            ),
            {"u": uid},
        )
        await s.commit()

    async with db_session() as s:
        truoc = (
            await s.execute(text("SELECT started_bot_at FROM users WHERE user_id = :u"), {"u": uid})
        ).scalar_one()
    assert truoc is None, "dữ liệu mẫu phải bắt đầu từ trạng thái chưa /start"

    async with transaction() as s:
        await users_service.upsert_user(s, user_id=uid, username="tumini", full_name="Tu Mini App")

    async with db_session() as s:
        sau = (
            await s.execute(text("SELECT started_bot_at FROM users WHERE user_id = :u"), {"u": uid})
        ).scalar_one()
    assert sau is not None, "/start phải điền started_bot_at cho hàng đã tồn tại"


async def test_start_lan_hai_KHONG_doi_moc_start_dau_tien(wired_db) -> None:
    """Giữ nguyên mốc /start ĐẦU TIÊN — nó là dữ liệu, không phải một cờ."""
    from televip.db.engine import transaction
    from televip.services import users as users_service

    uid = 971_102
    async with transaction() as s:
        await users_service.upsert_user(s, user_id=uid, username="lan1", full_name="Lan 1")
    async with db_session() as s:
        lan1 = (
            await s.execute(text("SELECT started_bot_at FROM users WHERE user_id = :u"), {"u": uid})
        ).scalar_one()

    async with transaction() as s:
        await users_service.upsert_user(s, user_id=uid, username="lan2", full_name="Lan 2")
    async with db_session() as s:
        lan2 = (
            await s.execute(text("SELECT started_bot_at FROM users WHERE user_id = :u"), {"u": uid})
        ).scalar_one()

    assert lan1 == lan2, "mốc /start đầu tiên không được ghi đè"
