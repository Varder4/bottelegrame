"""Năm màn hình chỉ-đọc của bàn phím chính, chạy **qua chính handler**.

Trọng tâm là bốn chỗ dễ hỏng mà không ai nhận ra:

- màn hình thống kê của người mới toanh phải in số 0 và `Chưa xếp hạng`, **không** ném lỗi;
- nút bảng xếp hạng đang xem vẫn phải được `answerCallbackQuery` (`noop`), nếu không nó
  quay vòng cho tới khi Telegram tự huỷ query;
- link rỗng thì **không dựng nút**: `InlineKeyboardButton(url="")` bị Telegram từ chối cả
  tin, và người dùng mất luôn cả phần chữ;
- `file_id` ảnh event chết thì gửi lại dạng text kèm đủ nút, không im lặng (§13.2.10).

⚠️ Cùng luật với `test_tanthu.py`: một engine toàn cục duy nhất, không dùng fixture `db`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers import misc
from televip.db.engine import session as db_session
from televip.domain import texts
from televip.telegram import keyboards
from tests.conftest import TEST_DATABASE_URL, _truncate_all

SUPPORT_LINK = "https://t.me/cskh_test"
GAME_LINK = "https://game.test/play"
SHARE_LINK = "https://facebook.test/bai-viet"


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    def __init__(self, *, photo_fails: bool = False) -> None:
        self.messages: list[str] = []
        self.photos: list[tuple[str, str | None]] = []
        self.markups: list[Any] = []
        self.answers: list[str] = []
        self.photo_fails = photo_fails

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return 1000 + len(self.messages)

    async def send_photo(
        self, chat_id: int, photo: str, caption: str | None = None, **kwargs: Any
    ) -> int | None:
        self.photos.append((photo, caption))
        if self.photo_fails:
            return None
        self.markups.append(kwargs.get("reply_markup"))
        return 2000 + len(self.photos)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]


def make_update(user_id: int, *, callback: str | None = None, chat_type: str = "private") -> Any:
    chat = SimpleNamespace(id=user_id, type=chat_type)
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=None, chat=chat)
    query = SimpleNamespace(data=callback, message=message) if callback else None
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=query,
    )


def make_context(sender: FakeSender) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=SimpleNamespace(username="televip_test_bot"),
        args=[],
    )


@pytest_asyncio.fixture
async def wired(redis_clean):
    from televip.db import engine as db_engine
    from televip.services import settings_service, text_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    settings_service.invalidate()
    text_service.invalidate()

    async with db_session() as s:
        await _truncate_all(s)
        await s.commit()

    try:
        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()
        text_service.invalidate()


# ── Helper ──────────────────────────────────────────────────────────


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


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


async def add_user(user_id: int, username: str = "nguoidung") -> int:
    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:uid, :un) ON CONFLICT DO NOTHING",
        {"uid": user_id, "un": username},
    )
    return user_id


# ── 📊Thống Kê TK ───────────────────────────────────────────────────


async def test_thong_ke_nguoi_moi_toanh_in_chua_xep_hang_va_khong_no(wired) -> None:
    sender = FakeSender()
    await misc.handle_stats(make_update(9_001), make_context(sender))

    assert "THỐNG KÊ TỔNG QUAN HỆ THỐNG" in sender.last
    assert "🆔 ID của bạn: 9001" in sender.last
    assert sender.last.count(texts.NO_RANK) == 2  # hạng hôm nay và hạng toàn thời gian


async def test_thong_ke_in_so_lieu_ca_nhan_that(wired) -> None:
    uid = await add_user(9_101)
    await run_sql(
        """
        UPDATE users SET total_codes_received = 3, total_value_received = 30000
         WHERE user_id = :uid
        """,
        {"uid": uid},
    )
    await run_sql(
        """
        INSERT INTO user_stats (user_id, refs_total, refs_qualified) VALUES (:uid, 7, 5)
        """,
        {"uid": uid},
    )

    sender = FakeSender()
    await misc.handle_stats(make_update(uid), make_context(sender))

    assert "👬 Số người bạn đã mời thành công: 5 người" in sender.last
    assert "🎁 Tổng mã đã nhận: 3" in sender.last
    assert "30.000 VNĐ" in sender.last
    assert "📅 Hạng hôm nay:" in sender.last


async def test_trong_nhom_thi_im_lang_tuyet_doi(wired) -> None:
    sender = FakeSender()
    await misc.handle_stats(make_update(9_201, chat_type="supergroup"), make_context(sender))
    assert sender.messages == []


async def test_cooldown_chan_lan_bam_thu_hai(wired) -> None:
    sender = FakeSender()
    update, context = make_update(9_301), make_context(sender)

    await misc.handle_stats(update, context)
    await misc.handle_stats(update, context)

    assert len(sender.messages) == 2
    assert "quá nhanh" in sender.last.lower() or "chờ" in sender.last.lower()


# ── 👑 BXH ──────────────────────────────────────────────────────────


async def test_bxh_mo_o_che_do_hom_nay_va_vo_hieu_dung_nut(wired) -> None:
    sender = FakeSender()
    await misc.handle_leaderboard(make_update(9_401), make_context(sender))

    assert "BẢNG XẾP HẠNG — HÔM NAY" in sender.last
    buttons = sender.markups[-1].inline_keyboard[0]
    assert [b.text for b in buttons] == [keyboards.BTN_LB_TODAY, keyboards.BTN_LB_ALLTIME]
    # Nút "đang xem" mang `noop`; nút kia mang callback thật.
    assert buttons[0].callback_data == keyboards.CB_NOOP
    assert buttons[1].callback_data == keyboards.CB_LB_ALLTIME


async def test_callback_bxh_toan_thoi_gian_tra_loi_callback_truoc(wired) -> None:
    sender = FakeSender()
    await misc.handle_leaderboard_alltime(
        make_update(9_501, callback=keyboards.CB_LB_ALLTIME), make_context(sender)
    )

    assert sender.answers, "callback phải được trả lời trong 2 giây"
    assert "BẢNG XẾP HẠNG — TOÀN THỜI GIAN" in sender.last
    buttons = sender.markups[-1].inline_keyboard[0]
    assert buttons[0].callback_data == keyboards.CB_LB_TODAY
    assert buttons[1].callback_data == keyboards.CB_NOOP


async def test_callback_bxh_hom_nay_khong_dinh_cooldown(wired) -> None:
    """Đổi qua đổi lại giữa hai chế độ là thao tác một tay, không phải spam."""
    sender = FakeSender()
    update = make_update(9_601, callback=keyboards.CB_LB_TODAY)
    context = make_context(sender)

    await misc.handle_leaderboard_alltime(update, context)
    await misc.handle_leaderboard_today(update, context)

    assert len(sender.messages) == 2
    assert "TOÀN THỜI GIAN" in sender.messages[0]
    assert "HÔM NAY" in sender.messages[1]


async def test_bxh_bang_rong_in_chua_co_du_lieu_ca_ba_khoi(wired) -> None:
    sender = FakeSender()
    await misc.handle_leaderboard(make_update(9_701), make_context(sender))
    assert sender.last.count(texts.NO_DATA) == 3


async def test_noop_chi_tra_loi_callback_khong_gui_gi(wired) -> None:
    sender = FakeSender()
    await misc.handle_noop(make_update(9_801, callback=keyboards.CB_NOOP), make_context(sender))

    assert sender.answers == [""]
    assert sender.messages == []


# ── 💁‍♀️ Hỗ Trợ · 🎮 Chơi Game ───────────────────────────────────────


async def test_ho_tro_in_link_cskh_tu_settings(wired) -> None:
    await set_setting(misc.LINK_SUPPORT_KEY, SUPPORT_LINK)
    sender = FakeSender()
    await misc.handle_support(make_update(9_901), make_context(sender))
    assert SUPPORT_LINK in sender.last


async def test_choi_game_co_link_thi_co_nut(wired) -> None:
    await set_setting(misc.LINK_GAME_KEY, GAME_LINK)
    sender = FakeSender()
    await misc.handle_play_game(make_update(10_001), make_context(sender))

    assert sender.markups[-1].inline_keyboard[0][0].url == GAME_LINK


async def test_choi_game_thieu_link_thi_khong_dung_nut(wired) -> None:
    sender = FakeSender()
    await misc.handle_play_game(make_update(10_101), make_context(sender))

    assert "Chơi Game" in sender.last
    # Không nút, vì `url=""` làm Telegram từ chối cả tin.
    assert sender.markups[-1] is None


# ── 📢 EVENT ────────────────────────────────────────────────────────


async def test_event_chia_se_dung_noi_dung_seed_khi_chua_cau_hinh(wired) -> None:
    await set_setting(misc.LINK_SUPPORT_KEY, SUPPORT_LINK)
    await set_setting(misc.LINK_SHARE_KEY, SHARE_LINK)
    await set_setting(misc.LINK_GAME_KEY, GAME_LINK)
    await set_setting(misc.LINK_GROUP_KEY, "https://t.me/cong_dong")

    sender = FakeSender()
    await misc.handle_share_event(make_update(10_201), make_context(sender))

    assert "NHẬN CODE HK79 - Quà Tặng SIÊU NHANH" in sender.last
    assert "https://t.me/televip_test_bot" in sender.last  # link bot lấy từ getMe
    assert "https://t.me/cong_dong" in sender.last
    urls = [b.url for row in sender.markups[-1].inline_keyboard for b in row]
    assert urls == [SHARE_LINK, SUPPORT_LINK]


async def test_event_chia_se_uu_tien_noi_dung_admin_da_cau_hinh(wired) -> None:
    await set_setting(misc.LINK_SUPPORT_KEY, SUPPORT_LINK)
    await run_sql(
        """
        INSERT INTO share_event_config (id, caption, share_link)
             VALUES (1, 'Bài viết do admin soạn', :link)
        """,
        {"link": SHARE_LINK},
    )

    sender = FakeSender()
    await misc.handle_share_event(make_update(10_301), make_context(sender))

    assert sender.last == "Bài viết do admin soạn"


async def test_event_chia_se_anh_chet_thi_gui_lai_dang_text(wired) -> None:
    await set_setting(misc.LINK_SUPPORT_KEY, SUPPORT_LINK)
    await run_sql(
        """
        INSERT INTO share_event_config (id, image_file_id, caption, share_link)
             VALUES (1, 'file_id_da_chet', 'Nội dung event', :link)
        """,
        {"link": SHARE_LINK},
    )

    sender = FakeSender(photo_fails=True)
    await misc.handle_share_event(make_update(10_401), make_context(sender))

    assert sender.photos == [("file_id_da_chet", "Nội dung event")]
    # Không im lặng: cùng nội dung, cùng nút, gửi lại bằng text.
    assert sender.last == "Nội dung event"
    urls = [b.url for row in sender.markups[-1].inline_keyboard for b in row]
    assert urls == [SHARE_LINK, SUPPORT_LINK]


async def test_event_chia_se_anh_song_thi_khong_gui_them_text(wired) -> None:
    await set_setting(misc.LINK_SUPPORT_KEY, SUPPORT_LINK)
    await run_sql(
        """
        INSERT INTO share_event_config (id, image_file_id, caption, share_link)
             VALUES (1, 'file_id_song', 'Nội dung event', :link)
        """,
        {"link": SHARE_LINK},
    )

    sender = FakeSender()
    await misc.handle_share_event(make_update(10_501), make_context(sender))

    assert len(sender.photos) == 1
    assert sender.messages == []
