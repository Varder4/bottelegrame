"""Năm màn hình chỉ-đọc của bàn phím chính.

    📊Thống Kê TK    §13.2.5   số liệu hệ thống + số liệu cá nhân + hai thứ hạng
    👑 BXH           §13.2.6   ba khối xếp hạng, hai chế độ (`lb_today` / `lb_alltime`)
    💁‍♀️ Hỗ Trợ – CSKH §13.2.11  link CSKH
    🎮 Chơi Game 🎮   §13.2.12  link bot game
    📢 EVENT          §13.2.10  chiến dịch chia sẻ, trao thưởng thủ công qua CSKH

Không màn hình nào ở đây chạm vào kho code hay sổ cái tiền: chúng chỉ đọc và in. Vì vậy
cũng không màn hình nào đi qua cổng xác minh — §13.2 chỉ đặt cổng đó trước các luồng
**phát quà**, và bắt người dùng xác minh để được xem link CSKH là dựng một bức tường ngay
trước cửa phòng hỗ trợ.

Ba luật chung, giống mọi handler khác:

1. Chỉ làm việc trong chat riêng; trong nhóm bot im lặng tuyệt đối.
2. Mỗi nút có cooldown riêng theo `settings.cooldown.*` (§13.6.6).
3. Câu chữ lấy qua `text_service.render(...)` — **không** gọi thẳng `domain/texts.py`, vì
   mọi câu ở đây đều sửa được bằng `/suanoidung`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text as sql_text
from telegram import Update
from telegram.ext import ContextTypes

from televip.cache.antispam import check_cooldown
from televip.core.errors import RateLimited
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import settings_service, stats, text_service
from televip.telegram import keyboards

log = get_logger(__name__)

# ── Khoá cấu hình ───────────────────────────────────────────────────

LINK_SUPPORT_KEY: Final = "link.support"
LINK_GAME_KEY: Final = "link.game_bot"
LINK_SHARE_KEY: Final = "link.share_url"
#: Link nhóm cộng đồng in trong nội dung mặc định của `📢 EVENT`. Khoá này §13.6.8 chưa
#: liệt kê, nhưng caption mẫu ở §13.2.10 có dòng "📱 Group Cộng Đồng: <link group>" — nên
#: nó phải là dữ liệu như bốn link còn lại, không phải chuỗi gõ vào handler.
LINK_GROUP_KEY: Final = "link.group"

SHARE_REQUIRED_GROUPS_KEY: Final = "share_event.required_groups"
DEFAULT_SHARE_REQUIRED_GROUPS: Final = 5

#: Cooldown seed của §13.6.6. Hằng ở đây chỉ là đường lui khi bảng `settings` chưa seed.
DEFAULT_COOLDOWNS: Final[dict[str, float]] = {
    "stats": 5.0,
    "leaderboard": 5.0,
    "support": 3.0,
    "play_game": 2.0,
    "share_event": 3.0,
}


# ── Khung chung của một màn hình ────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Screen:
    """Ba thứ mọi handler dưới đây cần, đã qua kiểm chat riêng và cooldown."""

    chat_id: int
    user_id: int
    sender: Any


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


async def _enter(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> _Screen | None:
    """Kiểm chat riêng + cooldown. `None` nghĩa là dừng — thông báo (nếu có) đã gửi rồi.

    `action` trùng tên handler trong `keyboards.ROUTE_TABLE`, nên nó vừa là hậu tố khoá
    `cooldown.*` (§13.6.6) vừa là khoá chống spam trong Redis: một lần tra ra cả hai.
    """
    message = update.effective_message
    chat = update.effective_chat
    tg_user = update.effective_user
    if message is None or chat is None or tg_user is None or chat.type != "private":
        return None

    sender = _sender(context)
    try:
        seconds = await settings_service.get_float(
            f"cooldown.{action}", DEFAULT_COOLDOWNS.get(action, 2.0)
        )
        await check_cooldown(tg_user.id, action, seconds)
    except RateLimited as exc:
        await sender.send_message(
            chat.id,
            await text_service.render("error.rate_limited", seconds=exc.retry_after_seconds),
        )
        return None

    return _Screen(chat_id=chat.id, user_id=tg_user.id, sender=sender)


# ── 📊Thống Kê TK (§13.2.5) ─────────────────────────────────────────


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Số liệu hệ thống + số liệu cá nhân, đọc từ hai bảng tổng hợp.

    Mở `transaction()` chứ không `session()` vì `system_snapshot()` có một nhánh tự tính
    lại khi ảnh chụp quá cũ (job làm mới đã chết) — nhánh đó ghi.
    """
    screen = await _enter(update, context, "stats")
    if screen is None:
        return

    async with transaction() as db:
        system = await stats.system_snapshot(db)
        me = await stats.user_snapshot(db, screen.user_id)

    await screen.sender.send_message(
        screen.chat_id,
        await text_service.render(
            "stats.account",
            total_refs=system.total_referrals,
            total_value_vnd=system.total_value_vnd,
            available_codes=system.codes_available,
            used_codes=system.codes_issued,
            total_users=system.total_users,
            user_id=me.user_id,
            qualified_refs=me.refs_qualified,
            rank_today=_rank_label(me.rank_today),
            rank_alltime=_rank_label(me.rank_alltime),
            total_codes_received=me.codes_received,
            total_value_received_vnd=me.value_received,
            updated_at=texts.vn_time(system.updated_at, with_seconds=True),
        ),
    )


def _rank_label(rank: int | None) -> str:
    """ "Chưa xếp hạng" không phải một con số, nên nó đi vào template dưới dạng chuỗi."""
    return str(rank) if rank is not None else texts.NO_RANK


# ── 👑 BXH (§13.2.6) ────────────────────────────────────────────────


async def handle_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nút `👑 BXH` — mở ở chế độ **hôm nay**."""
    screen = await _enter(update, context, "leaderboard")
    if screen is None:
        return
    await _send_leaderboard(screen, today=True)


async def handle_leaderboard_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback `lb_today`."""
    await _leaderboard_callback(update, context, today=True)


async def handle_leaderboard_alltime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback `lb_alltime`."""
    await _leaderboard_callback(update, context, today=False)


async def _leaderboard_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, today: bool
) -> None:
    """Đổi chế độ bằng một tin MỚI, không sửa tin cũ.

    Sửa tin tại chỗ đẹp hơn, nhưng `telegram/sender.py` là lớp gửi duy nhất và nó không
    mở `editMessageText`. Gửi tin mới giữ được luật đó và vẫn cho người dùng đúng nội dung
    họ vừa bấm; lịch sử hội thoại dài thêm một tin là cái giá rẻ hơn nhiều so với một
    đường gọi Bot API thứ hai nằm ngoài token bucket.
    """
    query = update.callback_query
    chat = update.effective_chat
    tg_user = update.effective_user
    if query is None or chat is None or tg_user is None:
        return

    sender = _sender(context)
    # TRƯỚC MỌI THỨ KHÁC (§13.3.3): quá 2 giây là Telegram huỷ query và nút quay vòng.
    await sender.answer_callback(query)
    await _send_leaderboard(
        _Screen(chat_id=chat.id, user_id=tg_user.id, sender=sender), today=today
    )


async def _send_leaderboard(screen: _Screen, *, today: bool) -> None:
    limit = await stats.top_n()
    async with session() as db:
        board = await stats.leaderboard(db, today=today, limit=limit)

    await screen.sender.send_message(
        screen.chat_id,
        await text_service.render(
            "leaderboard.screen",
            mode="HÔM NAY" if today else "TOÀN THỜI GIAN",
            block_referrers=texts.referrer_lines(board.top_referrers),
            block_receivers=texts.receiver_lines(board.top_receivers),
            block_streaks=texts.streak_lines(board.top_streaks),
            updated_at=texts.vn_time(board.updated_at, with_seconds=False),
        ),
        reply_markup=keyboards.leaderboard_keyboard(today=today),
    )


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nút "đang xem" của bảng xếp hạng (`keyboards.CB_NOOP`).

    Telegram không có khái niệm nút inline bị vô hiệu, nên nút đó vẫn gửi về một callback
    thật. Nó phải được answer rỗng — thiếu handler này thì lưới an toàn ở `main.py` trả
    lời "chức năng đang được hoàn thiện" cho một nút đang hoạt động bình thường.
    """
    query = update.callback_query
    if query is None:
        return
    await _sender(context).answer_callback(query)


# ── 💁‍♀️ Hỗ Trợ – CSKH (§13.2.11) ───────────────────────────────────


async def handle_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    screen = await _enter(update, context, "support")
    if screen is None:
        return

    support_link = await settings_service.get_str(LINK_SUPPORT_KEY, "")
    if not support_link:
        log.error("thieu_cau_hinh", key=LINK_SUPPORT_KEY)
    await screen.sender.send_message(
        screen.chat_id,
        await text_service.render("support.contact", support_link=support_link),
    )


# ── 🎮 Chơi Game 🎮 (§13.2.12) ──────────────────────────────────────


async def handle_play_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    screen = await _enter(update, context, "play_game")
    if screen is None:
        return

    game_link = await settings_service.get_str(LINK_GAME_KEY, "")
    if not game_link:
        log.error("thieu_cau_hinh", key=LINK_GAME_KEY)
    await screen.sender.send_message(
        screen.chat_id,
        await text_service.render("game.play"),
        # Link rỗng thì KHÔNG dựng nút: `InlineKeyboardButton(url="")` bị Telegram từ chối
        # cả tin, và người dùng mất luôn cả phần chữ.
        reply_markup=keyboards.play_game_keyboard(game_link) if game_link else None,
    )


# ── 📢 EVENT (§13.2.10) ─────────────────────────────────────────────

_SQL_SHARE_EVENT = """
SELECT image_file_id, caption, share_link FROM share_event_config WHERE id = 1
"""


async def handle_share_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chiến dịch chia sẻ: ảnh + caption + hai nút. Trao thưởng do CSKH duyệt thủ công.

    Ảnh gửi bằng `file_id` đã lưu trong `share_event_config`. `file_id` chết thì gửi lại
    dạng text kèm đủ nút — §13.2.10 cấm im lặng ở nhánh này.
    """
    screen = await _enter(update, context, "share_event")
    if screen is None:
        return

    async with session() as db:
        row = (await db.execute(sql_text(_SQL_SHARE_EVENT))).one_or_none()

    support_link = await settings_service.get_str(LINK_SUPPORT_KEY, "")
    share_link = (row.share_link if row is not None else "") or await settings_service.get_str(
        LINK_SHARE_KEY, ""
    )
    caption = (row.caption if row is not None else "") or await _default_share_caption(context)

    markup = None
    if share_link and support_link:
        markup = keyboards.share_event_keyboard(share_link, support_link)
    else:
        log.error("thieu_cau_hinh", key=f"{LINK_SHARE_KEY}/{LINK_SUPPORT_KEY}")

    file_id = row.image_file_id if row is not None else None
    if file_id:
        sent = await screen.sender.send_photo(screen.chat_id, file_id, caption, reply_markup=markup)
        if sent is not None:
            return
        log.warning("anh_event_chia_se_chet", file_id=file_id)

    await screen.sender.send_message(screen.chat_id, caption, reply_markup=markup)


async def _default_share_caption(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Nội dung seed khi admin chưa cấu hình `share_event_config`."""
    return await text_service.render(
        "event.share_default",
        bot_link=_bot_link(context),
        game_link=await settings_service.get_str(LINK_GAME_KEY, ""),
        group_link=await settings_service.get_str(LINK_GROUP_KEY, ""),
        required_groups=await settings_service.get_int(
            SHARE_REQUIRED_GROUPS_KEY, DEFAULT_SHARE_REQUIRED_GROUPS
        ),
    )


def _bot_link(context: ContextTypes.DEFAULT_TYPE) -> str:
    """`https://t.me/<username>` của chính bot đang chạy.

    Đọc từ `getMe` mà thư viện đã cache lúc khởi động, không từ một khoá cấu hình: link
    này không phải một lựa chọn vận hành mà là danh tính của tiến trình — một khoá sửa
    được ở đây chỉ tạo cơ hội cho nó trỏ sai bot.
    """
    username = getattr(context.bot, "username", None)
    return f"https://t.me/{username}" if username else ""


__all__ = [
    "DEFAULT_COOLDOWNS",
    "LINK_GAME_KEY",
    "LINK_GROUP_KEY",
    "LINK_SHARE_KEY",
    "LINK_SUPPORT_KEY",
    "SHARE_REQUIRED_GROUPS_KEY",
    "handle_leaderboard",
    "handle_leaderboard_alltime",
    "handle_leaderboard_today",
    "handle_noop",
    "handle_play_game",
    "handle_share_event",
    "handle_stats",
    "handle_support",
]
