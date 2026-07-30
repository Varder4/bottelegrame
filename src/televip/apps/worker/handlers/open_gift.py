"""Nút `🎁 Mở Quà Ngay 🎁` — cửa vào luồng nhận quà từ màn hình `/start`.

Đây là nút ĐẦU TIÊN người dùng nhìn thấy sau `/start`, nên nó không được phép treo.
Trước khi có file này, callback `open_gift` rơi vào lưới an toàn và chỉ trả lời
"đang được hoàn thiện" — người dùng bấm bốn lần liên tiếp mà không có gì xảy ra.

Nút này không tự làm gì cả: nó chỉ đưa người dùng vào đúng bước kế tiếp của họ.

- Chưa xác minh  → màn hình xác minh, kèm nút mở Mini App.
- Đã xác minh    → chuyển thẳng sang luồng code tân thủ (kiểm nhóm rồi phát mã).

Cách này giữ đúng một nguồn sự thật cho "bước kế tiếp là gì": logic nằm ở `gate.py`
và `tanthu.py`, ở đây chỉ điều hướng.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers.gate import require_verified
from televip.apps.worker.handlers.tanthu import handle_tanthu
from televip.core.logging import get_logger

log = get_logger(__name__)


async def handle_open_gift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    sender = context.application.bot_data["sender"]
    # Trả lời callback TRƯỚC mọi việc khác: đặc tả §13.3.3 cho 2 giây, còn phần dưới có
    # thể chạm database và Bot API. Trả lời muộn là nút quay vòng và người dùng bấm lại.
    await sender.answer_callback(query)

    log.info("open_gift", user_id=update.effective_user.id)

    # `require_verified` tự gửi màn hình xác minh khi cần và trả False.
    if not await require_verified(update, context):
        return

    await handle_tanthu(update, context)
