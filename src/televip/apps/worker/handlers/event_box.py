"""Callback `dap_hop_<event_id>` — nút `🎁 Đập Hộp 🎁` của tin event (§13.2.9).

Ba điều chi phối file này:

1. **`answer_callback` trước mọi thứ khác.** §13.3.3 cho 2 giây, còn nhánh phía dưới đọc
   database và có thể chạm Bot API. Trả lời muộn là Telegram huỷ query, nút quay mãi, và
   người dùng bấm lại — ở một luồng phát tiền thì mỗi lần bấm lại là một rủi ro.

   Hệ quả cố ý: kết quả được trả bằng **tin nhắn**, không bằng alert. Một query chỉ answer
   được một lần, và không có cách nào biết trước kết quả mà vẫn kịp 2 giây. §13.2.9 mô tả
   cả alert lẫn tin nhắn cho mỗi nhánh; ở đây giữ lại phần đọng lại được với người dùng.

2. **Toàn bộ quyết định nghiệp vụ nằm ở `services/event_box.open_box`.** File này không
   quay số, không đụng kho, không tính cửa sổ. Nó chỉ dịch một `BoxResult` thành câu chữ.

3. **`mark_delivered()` chỉ chạy SAU khi Telegram xác nhận đã gửi.** Gửi hỏng thì grant
   nằm lại ở `reserved` và `reap_reservations()` trả mã về kho — không mã nào bốc hơi.
"""

from __future__ import annotations

import re
from typing import Any, Final

from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers import gate
from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.services import code_issuance, event_box, settings_service, text_service
from televip.telegram import keyboards

log = get_logger(__name__)

#: `main.py` đăng ký `CallbackQueryHandler` với đúng mẫu này.
CALLBACK_PATTERN: Final = r"^dap_hop_\d+$"

_CB_RE: Final = re.compile(r"^dap_hop_(\d+)$")

SUPPORT_LINK_KEY: Final = "link.support"


def parse_event_id(data: str | None) -> int | None:
    """`'dap_hop_12'` → `12`. Trả `None` với mọi thứ khác.

    Cắt bằng tiền tố cố định chứ không `split('_')`: `dap_hop` bản thân nó đã có dấu gạch
    dưới, nên tách theo dấu gạch là cách chắc chắn lấy nhầm mảnh.
    """
    matched = _CB_RE.match(data or "")
    return int(matched.group(1)) if matched is not None else None


async def handle_open_box(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    tg_user = update.effective_user
    if query is None or chat is None or tg_user is None:
        return

    sender = context.application.bot_data["sender"]
    # TRƯỚC MỌI THỨ KHÁC (§13.3.3). `answer_callback` nuốt lỗi của chính nó nên dòng này
    # không bao giờ làm đổ luồng phía dưới.
    await sender.answer_callback(query)

    event_id = parse_event_id(query.data)
    if event_id is None:
        log.warning("dap_hop_callback_la", data=query.data)
        return

    if not await gate.require_verified(update, context):
        return

    try:
        async with transaction() as db:
            result = await event_box.open_box(db, event_id=event_id, user_id=tg_user.id)
    except ConfigError as exc:
        # Bảng tỉ lệ hỏng hoặc thiếu khoá cấu hình: lỗi của vận hành, không phải của người
        # đang bấm. Không đổ một thông báo khó hiểu lên đầu họ, nhưng phải hét trong log.
        log.error("dap_hop_cau_hinh_hong", event_id=event_id, detail=str(exc))
        await sender.send_message(chat.id, await text_service.render("error.system_busy"))
        return

    game_link = await settings_service.get_str(event_box.GAME_LINK_KEY, "")

    if result.status == "win":
        await _deliver_win(sender, chat.id, result, game_link=game_link)
        return

    if result.status == "out_of_stock":
        # §13.2.9 bước 7. Câu chữ dùng chung với mọi luồng hết kho — người dùng không cần
        # biết đây là kho event hay kho tân thủ, họ cần biết liên hệ ai.
        support_link = await settings_service.get_str(SUPPORT_LINK_KEY, "")
        await sender.send_message(
            chat.id, await text_service.render("code.out_of_stock", support_link=support_link)
        )
        return

    if result.status == "already":
        await sender.send_message(chat.id, await text_service.render("alert.already_joined_event"))
        return

    # Còn lại là `empty` và `closed`. Chạm trần ngân sách có câu riêng: đó là điều DUY
    # NHẤT được phép làm lệch tỉ lệ đã quảng cáo, nên §13.5.1 điểm 3 buộc nó phải hiện ra.
    key = "event.box_budget_capped" if result.reason == "budget_cap" else "event.box_empty"
    await sender.send_message(
        chat.id,
        await text_service.render(key, game_link=game_link),
        reply_markup=keyboards.play_game_keyboard(game_link) if game_link else None,
    )


async def _deliver_win(sender: Any, chat_id: int, result: Any, *, game_link: str) -> None:
    """Gửi mã rồi mới ghi sổ. Thứ tự này là điều kiện để kho không bị đốt."""
    sent = await sender.send_message(
        chat_id,
        await text_service.render(
            "event.box_win", code_value=result.code_value, value_vnd=result.value_vnd
        ),
        reply_markup=keyboards.enter_code_keyboard(game_link) if game_link else None,
    )
    if sent is None:
        # Người này vĩnh viễn không nhận được tin (đã chặn bot). Grant nằm lại ở
        # `reserved`; `reap_reservations()` trả mã về kho. KHÔNG đánh dấu đã giao.
        log.warning("gui_code_dap_hop_that_bai", chat_id=chat_id, grant_id=result.grant_id)
        return

    async with transaction() as db:
        await code_issuance.mark_delivered(db, grant_id=result.grant_id)


__all__ = ["CALLBACK_PATTERN", "handle_open_box", "parse_event_id"]
