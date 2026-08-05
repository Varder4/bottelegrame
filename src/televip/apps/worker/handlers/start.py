"""Luồng `/start` — cửa vào duy nhất của bot.

Đặc tả: `13-dac-ta-bot-moi.md` §13.2.1.

Bốn việc, theo đúng thứ tự:
1. Chặn nếu không phải chat riêng — trong nhóm bot im lặng tuyệt đối.
2. Chống spam theo `settings.cooldown.start`.
3. Tạo/cập nhật hồ sơ, nhận biết người mới.
4. Nếu là người mới và có deep link `ref_<id>` thì ghi nhận ý định giới thiệu.

5. **Chưa xác thực thì dừng ở màn xác thực** — không gắn bàn phím chính.

Xác thực xong mới gửi hai tin: banner chào + banner quà kèm nút mở quà, kèm bàn phím.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers import gate
from televip.cache.antispam import check_cooldown
from televip.core.errors import RateLimited
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.services import settings_service, text_service, users
from televip.telegram import keyboards

log = get_logger(__name__)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    tg_user = update.effective_user

    # Bot chỉ làm việc trong chat riêng. Trong nhóm nó không trả lời gì — kể cả `/start`.
    if message is None or chat is None or tg_user is None or chat.type != "private":
        return

    sender = context.application.bot_data["sender"]

    try:
        cooldown = await settings_service.get_float("cooldown.start", 3.0)
        await check_cooldown(tg_user.id, "start", cooldown)
    except RateLimited as exc:
        await sender.send_message(
            chat.id,
            await text_service.render("error.rate_limited", seconds=exc.retry_after_seconds),
        )
        return

    payload = context.args[0] if context.args else None
    referrer_id = users.parse_referral_payload(payload)

    async with transaction() as db:
        result = await users.upsert_user(
            db,
            user_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
        )
        # Chỉ người MỚI mới được tính cho người giới thiệu. Người đã dùng bot rồi mà bấm
        # link của ai đó thì không ai được thưởng — nếu không, một nhóm người chỉ cần
        # bấm link của nhau là sinh ra referral vô hạn.
        if result.is_new and referrer_id is not None:
            await users.record_referral_intent(db, referee_id=tg_user.id, referrer_id=referrer_id)

    log.info(
        "start",
        user_id=tg_user.id,
        is_new=result.is_new,
        is_verified=result.is_verified,
        co_nguoi_gioi_thieu=referrer_id is not None,
    )

    # Chưa xác thực thì DỪNG ở màn xác thực, không gắn bàn phím chính. Cổng chặn vốn nằm
    # trước từng nút phát quà; đưa nó lên `/start` khiến người mới chỉ nhìn thấy đúng một
    # việc phải làm, thay vì một bàn phím mười nút mà bấm cái nào cũng ra cùng một câu.
    #
    # Dùng lại `gate.require_verified()` chứ không dựng bản sao: cùng câu chữ, cùng nút,
    # cùng nhánh xử lý khi `webapp.url` chưa cấu hình.
    if not await gate.require_verified(update, context):
        return

    await sender.send_message(
        chat.id,
        await text_service.render("start.welcome"),
        reply_markup=keyboards.main_keyboard(),
    )
    await sender.send_message(
        chat.id,
        await text_service.render("start.gift_teaser"),
        reply_markup=keyboards.open_gift_keyboard(),
    )
