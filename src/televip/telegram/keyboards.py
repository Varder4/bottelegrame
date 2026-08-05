"""Bàn phím Telegram — nhãn nút và bảng định tuyến.

Chép từ `docs/ke-hoach-v2/13-dac-ta-bot-moi.md` §13.3. **Nhãn là dữ liệu, không phải câu
chữ trang trí**: Telegram gửi lại đúng chuỗi trên nút như một tin nhắn text bình thường,
nên nhãn đồng thời là khoá định tuyến. Ba nhãn thiếu dấu cách sau emoji
(`🎁Code Tân Thủ`, `📊Thống Kê TK`, `📈Check Chia sẻ`) được **giữ nguyên** — người dùng cũ
quen mặt chúng, và sửa "cho đẹp" là đổi khoá định tuyến.

Nhãn `💁‍♀️ Hỗ Trợ – CSKH` chứa hai chi tiết dễ mất khi gõ lại: emoji ZWJ
(`U+1F481 U+200D U+2640 U+FE0F`) và **en dash** `–` (`U+2013`), không phải dấu trừ. Đó là
lý do mọi nhãn ở đây là hằng số — không nơi nào trong code được gõ lại chuỗi này.

## Định tuyến: so khớp BẰNG NHAU TUYỆT ĐỐI

`ROUTE_TABLE` ánh xạ **nhãn → tên handler**. Router bắt buộc tra bằng
``ROUTE_TABLE.get(update.message.text)`` — **KHÔNG** dùng ``if ... in text``.

Bot cũ dùng một chuỗi `if/elif` với `in`:: ``text == "🎮 Chơi Game 🎮" or "Chơi" in text or
"Game" in text`` (`main.py:385`). Hệ quả là **mọi** tin nhắn chứa chữ "Chơi" hoặc "Game"
rơi vào handler chơi game, kể cả khi người dùng đang gõ nội dung khác, và nút đứng trước
trong chuỗi if nuốt luôn nút đứng sau. Tra dict theo khoá chính xác vừa loại hẳn lớp lỗi
đó, vừa là O(1) thay vì quét tuần tự 10 nhánh.

Text không khớp nhãn nào thì bot im lặng (hoặc gợi ý mở menu, tuỳ
`settings.ux.reply_on_unknown_text`) — quyết định đó thuộc router, không thuộc file này.
"""

from __future__ import annotations

from collections.abc import Sequence

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from televip.domain.texts import format_vnd, value_label

# ── Nhãn bàn phím chính (§13.3.1) ───────────────────────────────────

BTN_PLAY_GAME = "🎮 Chơi Game 🎮"
BTN_CODE_TAN_THU = "🎁Code Tân Thủ"
BTN_MOI_BAN = "💎 Mời bạn nhận quà"
BTN_EVENT = "📢 EVENT"
BTN_DIEM_DANH = "✅ Điểm Danh"
BTN_DOI_CODE = "🎁 Đổi CODE"
BTN_BXH = "👑 BXH"
BTN_CHECK_CHIA_SE = "📈Check Chia sẻ"
BTN_THONG_KE_TK = "📊Thống Kê TK"
BTN_HO_TRO = "💁‍♀️ Hỗ Trợ – CSKH"
BTN_SHOW_FULL = "XEM SHOW FULL 🔞"

#: Nhãn → tên handler. Tên handler trùng hậu tố khoá cooldown ở §13.6.6
#: (`cooldown.tan_thu`, `cooldown.check_share`…) để router lấy được cả hai từ một lần tra.
_ROUTE_DAY_DU: dict[str, str] = {
    BTN_PLAY_GAME: "play_game",
    BTN_CODE_TAN_THU: "tan_thu",
    BTN_MOI_BAN: "moi_ban",
    BTN_EVENT: "share_event",
    BTN_SHOW_FULL: "show_full",
    BTN_DIEM_DANH: "checkin",
    BTN_DOI_CODE: "redeem_code",
    BTN_BXH: "leaderboard",
    BTN_CHECK_CHIA_SE: "check_share",
    BTN_THONG_KE_TK: "stats",
    BTN_HO_TRO: "support",
}

#: Thứ tự các nút, trước khi lọc. Xếp hai nút một hàng.
_THU_TU: tuple[str, ...] = (
    BTN_PLAY_GAME,
    BTN_CODE_TAN_THU,
    BTN_MOI_BAN,
    BTN_EVENT,
    BTN_SHOW_FULL,
    BTN_DIEM_DANH,
    BTN_DOI_CODE,
    BTN_BXH,
    BTN_CHECK_CHIA_SE,
    BTN_THONG_KE_TK,
    BTN_HO_TRO,
)

#: Nút TẠM ẨN. Bỏ một nhãn khỏi tập này là hiện nó lại — handler và cooldown vẫn còn
#: nguyên, không phải dựng lại gì.
#:
#: Ẩn ở đây gỡ nút khỏi CẢ bàn phím LẪN bảng tra: bàn phím Telegram nằm lại trên máy người
#: dùng cho tới lần `/start` sau, nên nếu chỉ gỡ khỏi bàn phím thì người đang có nút cũ vẫn
#: bấm được — tức là "ẩn" mà vẫn chạy.
AN_TAM_THOI: frozenset[str] = frozenset({BTN_DIEM_DANH, BTN_DOI_CODE})


def _xep_hang(nhan: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Gấp danh sách nhãn thành các hàng hai nút; hàng cuối có thể chỉ một nút."""
    return tuple(tuple(nhan[i : i + 2]) for i in range(0, len(nhan), 2))


_HIEN: tuple[str, ...] = tuple(n for n in _THU_TU if n not in AN_TAM_THOI)

#: Nguồn duy nhất của bố cục — `main_keyboard()` và `ROUTE_TABLE` đều dẫn xuất từ nó nên
#: không thể lệch nhau.
MAIN_KEYBOARD_LAYOUT: tuple[tuple[str, ...], ...] = _xep_hang(_HIEN)

ROUTE_TABLE: dict[str, str] = {n: _ROUTE_DAY_DU[n] for n in _HIEN}


def main_keyboard() -> ReplyKeyboardMarkup:
    """Bàn phím chính.

    `one_time_keyboard` cố ý **không** bật: bàn phím này là menu thường trực của bot, ẩn
    đi sau một lần bấm buộc người dùng phải mở lại bằng tay.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in MAIN_KEYBOARD_LAYOUT],
        resize_keyboard=True,
    )


def route_of(text: str) -> str | None:
    """Tra handler cho một tin nhắn text. So khớp bằng nhau tuyệt đối, không `in`."""
    return ROUTE_TABLE.get(text)


# ── Nhãn nút inline (§13.3.3) ───────────────────────────────────────

BTN_OPEN_GIFT = "🎁 Mở Quà Ngay 🎁"
BTN_VERIFY = "🤖 Xác minh ngay"
BTN_VERIFY_STEP1 = "🤖 Xác minh ngay (BƯỚC 1)"
BTN_CHECK_GROUPS = "✨ Tôi Đã Tham Gia - Nhận CODE ✨"
BTN_JOIN_REFERRAL = "💎 Tham gia ngay"
BTN_SHARE_POST_CTA = "🎁 Tham Gia Nhận CODE Tân Thủ 🎁"
BTN_OPEN_BOX = "🎁 Đập Hộp 🎁"
BTN_LB_TODAY = "📅 Hôm nay"
BTN_LB_ALLTIME = "👑 Toàn thời gian"
BTN_ENTER_CODE = "🎁 Nhập CODE Tại Đây 💸"
BTN_SHOW_FULL_CTA = "🔞 XEM NGAY 🔞"
BTN_SHARE_NOW = "📤 Chia Sẻ Ngay"
BTN_SHARED_CONTACT_CSKH = "✅ Đã Chia Sẻ - Liên Hệ CSKH"

# ── callback_data (§13.3.3) ─────────────────────────────────────────

CB_OPEN_GIFT = "open_gift"
CB_CHECK_GROUPS = "check_groups"
CB_JOIN_REFERRAL = "join_referral"
CB_LB_TODAY = "lb_today"
CB_LB_ALLTIME = "lb_alltime"

#: Tiền tố có tham số. Handler cắt bằng `removeprefix`, không `split('_')` — id sự kiện
#: và mệnh giá đều là số nên tiền tố cố định là cách tách duy nhất không mơ hồ.
CB_OPEN_BOX_PREFIX = "dap_hop_"
CB_REDEEM_PREFIX = "redeem_"

#: **Tự định nghĩa, không có trong §13.3.3.** Telegram không có khái niệm nút inline bị
#: vô hiệu, nên nút "đang xem" của bảng xếp hạng vẫn phải mang một `callback_data` hợp lệ.
#: Router chỉ cần `answerCallbackQuery` rỗng cho khoá này và dừng.
CB_NOOP = "noop"


def open_gift_keyboard() -> InlineKeyboardMarkup:
    """Tin 2 của `/start`."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_OPEN_GIFT, callback_data=CB_OPEN_GIFT)]])


def verify_keyboard(webapp_url: str) -> InlineKeyboardMarkup:
    """Cổng chặn xác minh và nhánh event."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_VERIFY, web_app=WebAppInfo(url=webapp_url))]]
    )


def verify_step1_keyboard(webapp_url: str) -> InlineKeyboardMarkup:
    """Bước 1 của code tân thủ — nhãn khác `verify_keyboard()` vì màn hình nói rõ "BƯỚC 1"."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_VERIFY_STEP1, web_app=WebAppInfo(url=webapp_url))]]
    )


def check_groups_keyboard() -> InlineKeyboardMarkup:
    """Bước 2 của code tân thủ.

    Một chuỗi nhãn duy nhất cho cả ba nơi phát sinh nút này (bot, `web_app_data`, API) —
    bot cũ có ba biến thể khác nhau cho cùng `callback_data='check_groups'`.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_CHECK_GROUPS, callback_data=CB_CHECK_GROUPS)]]
    )


def join_referral_keyboard() -> InlineKeyboardMarkup:
    """Mời bạn bè và Check chia sẻ — cùng một callback, xử lý ở một chỗ."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_JOIN_REFERRAL, callback_data=CB_JOIN_REFERRAL)]]
    )


def share_post_keyboard(ref_link: str) -> InlineKeyboardMarkup:
    """Bài viết chia sẻ — link giới thiệu của **chính người bấm**."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_SHARE_POST_CTA, url=ref_link)]])


def open_box_keyboard(event_id: int) -> InlineKeyboardMarkup:
    """Tin event đập hộp."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_OPEN_BOX, callback_data=f"{CB_OPEN_BOX_PREFIX}{event_id}")]]
    )


def redeem_keyboard(affordable_tiers: Sequence[int]) -> InlineKeyboardMarkup:
    """Màn hình đổi code — **chỉ** các bậc người dùng đủ điểm mới có nút.

    Bậc chưa đủ vẫn hiện trong nội dung tin (kèm số còn thiếu), nhưng không có nút: nút
    bấm được rồi báo "không đủ điểm" là một vòng thao tác thừa.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"💎 Đổi Code {value_label(v)} ({format_vnd(v)}đ)",
                    callback_data=f"{CB_REDEEM_PREFIX}{v}",
                )
            ]
            for v in affordable_tiers
        ]
    )


def leaderboard_keyboard(*, today: bool) -> InlineKeyboardMarkup:
    """Hai chế độ bảng xếp hạng; chế độ đang xem bị vô hiệu (xem `CB_NOOP`)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(BTN_LB_TODAY, callback_data=CB_NOOP if today else CB_LB_TODAY),
                InlineKeyboardButton(
                    BTN_LB_ALLTIME, callback_data=CB_LB_ALLTIME if today else CB_NOOP
                ),
            ]
        ]
    )


def play_game_keyboard(game_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_PLAY_GAME, url=game_link)]])


def show_full_keyboard(show_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_SHOW_FULL_CTA, url=show_link)]])


def enter_code_keyboard(game_link: str) -> InlineKeyboardMarkup:
    """Đi kèm **mọi** màn hình phát code."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_ENTER_CODE, url=game_link)]])


def share_event_keyboard(share_link: str, support_link: str) -> InlineKeyboardMarkup:
    """Chiến dịch chia sẻ (nút `📢 EVENT`) — trao thưởng thủ công qua CSKH."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BTN_SHARE_NOW, url=share_link)],
            [InlineKeyboardButton(BTN_SHARED_CONTACT_CSKH, url=support_link)],
        ]
    )


def confirm_keyboard(
    *, ok_data: str, cancel_data: str, ok_label: str, cancel_label: str
) -> InlineKeyboardMarkup:
    """Cặp nút xác nhận / huỷ cho một lệnh admin phá huỷ.

    Nhãn và `callback_data` do nơi gọi truyền vào chứ không cố định ở đây: mỗi lệnh cần
    tiền tố callback RIÊNG để lớp phân giải gác đúng quyền của lệnh đó. Dùng chung một
    tiền tố nghĩa là nút xác nhận của lệnh chỉ-owner có thể bị bấm bởi người chỉ có quyền
    của lệnh kia.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(ok_label, callback_data=ok_data),
                InlineKeyboardButton(cancel_label, callback_data=cancel_data),
            ]
        ]
    )
