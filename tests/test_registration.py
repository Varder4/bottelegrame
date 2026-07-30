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

    # Bốn job phải đủ mặt. Thiếu `award_referral_tiers` thì mốc mời bạn không bao giờ được
    # phát (người được mời xác minh ở tiến trình web, việc gửi mã thuộc worker); thiếu
    # `refresh_user_stats` thì bảng xếp hạng toàn thời gian rỗng vĩnh viễn.
    assert set(intervals) == {
        "refresh_system_stats",
        "reap_reservations",
        "award_referral_tiers",
        "refresh_user_stats",
    }


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
CHUA_XAY: frozenset[str] = frozenset(
    {
        "/baocao",  # cần lớp báo cáo tổng hợp
        "/checkip",  # cần lớp chống gian lận (GĐ4B): signal_owners, risk_assessments
        "/chiendich",
        "/done_event",
        "/show_share_event",
        "/update_share_event",
    }
)


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
