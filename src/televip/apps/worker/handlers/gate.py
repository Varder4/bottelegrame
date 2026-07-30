"""Cổng chặn xác minh — **một** bản, dùng chung cho mọi luồng phát code.

Đặc tả: `13-dac-ta-bot-moi.md` §13.2.2 ("Cổng chặn toàn cục").

Bot cũ có bản sao riêng của màn hình này cho nhánh đập hộp, nên hai chỗ trôi khác nhau và
người dùng thấy hai câu chữ khác nhau cho cùng một tình huống. Ở đây chỉ có một hàm, và
mọi handler phát code phải gọi nó **trước** khi chạm vào kho code.
"""

from __future__ import annotations

from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.logging import get_logger
from televip.db.engine import session
from televip.domain import texts
from televip.services import settings_service
from televip.telegram import keyboards

log = get_logger(__name__)

#: Khoá cấu hình chứa URL Mini App xác minh.
#:
#: §13.2.2 gọi đích danh `settings.webapp.url` và migration 0002 đã seed đúng khoá đó (mô
#: tả: "URL Mini App xác minh"). Vì vậy KHÔNG tạo thêm khoá `webapp.verify_url`: hai khoá
#: cho cùng một giá trị là đúng loại lỗi mà cả `domain/texts.py` lẫn `settings` sinh ra để
#: chặn — admin đổi một khoá, nửa số màn hình vẫn trỏ khoá kia.
VERIFY_URL_KEY = "webapp.url"

_SQL_IS_VERIFIED = "SELECT (verified_at IS NOT NULL) AS verified FROM users WHERE user_id = :uid"


async def verify_url() -> str:
    """URL Mini App xác minh. Chuỗi rỗng nghĩa là vận hành chưa cấu hình.

    Seed để rỗng có chủ ý (migration 0002): đặc tả không cho URL nào, và chép link cứng
    vào code là dựng lại đúng cái tật §13.6.8 đang mô tả.
    """
    return await settings_service.get_str(VERIFY_URL_KEY, "")


async def is_verified(user_id: int) -> bool:
    """Đọc `users.verified_at`. Người chưa từng `/start` cũng là chưa xác minh."""
    async with session() as db:
        row = (await db.execute(text(_SQL_IS_VERIFIED), {"uid": user_id})).scalar_one_or_none()
    return bool(row)


async def require_verified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True nếu được đi tiếp. False thì màn hình xác minh **đã được gửi**, caller chỉ cần dừng.

    Trả về bool thay vì ném lỗi vì đây là một nhánh kịch bản bình thường của người dùng
    mới, không phải một sự cố — và mọi caller đều xử lý nó giống hệt nhau: dừng lại.
    """
    chat = update.effective_chat
    tg_user = update.effective_user
    if chat is None or tg_user is None:
        return False

    if await is_verified(tg_user.id):
        return True

    sender = context.application.bot_data["sender"]
    url = await verify_url()
    if not url:
        # Lỗi cấu hình của mình, không phải lỗi của người dùng: không đổ lên đầu họ một
        # thông báo khó hiểu, và cũng không dựng `WebAppInfo(url="")` để Telegram từ chối.
        log.error("thieu_cau_hinh", key=VERIFY_URL_KEY)
        await sender.send_message(chat.id, texts.system_busy())
        return False

    await sender.send_message(
        chat.id,
        texts.not_verified(),
        reply_markup=keyboards.verify_keyboard(url),
    )
    return False


__all__ = ["VERIFY_URL_KEY", "is_verified", "require_verified", "verify_url"]
