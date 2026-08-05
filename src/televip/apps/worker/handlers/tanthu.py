"""Luồng nhận code tân thủ — hai bước, theo `13-dac-ta-bot-moi.md` §13.2.3.

    bước 0   đã có grant `tanthu`  → màn hình "đã nhận rồi"
    bước 1   chưa xác minh          → nút WebApp `🤖 Xác minh ngay (BƯỚC 1)`
    bước 2   đã xác minh            → danh sách nhóm + nút `✨ Tôi Đã Tham Gia - Nhận CODE ✨`
    callback `check_groups`         → kiểm nhóm rồi cấp code

Ba chỗ trong file này chống lại một lỗi cụ thể đã tốn tiền thật:

1. `answer_callback` được gọi **trước mọi thứ khác**. §13.3.3 buộc answer trong 2 giây,
   mà nhánh phía sau chạm database và có thể chạm cả Bot API — quá 2 giây là Telegram huỷ
   query, nút quay mãi, và người dùng bấm lại (rồi lại bấm lại).
2. Cấp code đi **đúng một đường**: `code_issuance.reserve()` với `grant_key_tanthu()`.
   Bấm mười lần vẫn ra đúng một grant và đúng một mã.
3. `mark_delivered()` chỉ chạy **sau khi** Telegram xác nhận đã gửi. Hệ cũ đánh dấu trước
   rồi mới gửi, nên mỗi lần gửi lỗi là một mã bốc hơi khỏi kho mà không ai nhận được.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers import gate
from televip.cache.antispam import check_cooldown
from televip.core.errors import AlreadyClaimed, OutOfStock, RateLimited
from televip.core.ids import grant_key_tanthu
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import code_issuance, membership, settings_service, text_service
from televip.telegram import keyboards

log = get_logger(__name__)

#: Loại code trong KHO và loại phát trong SỔ CÁI trùng tên ở luồng này, nhưng chúng là hai
#: khái niệm khác nhau (`db/models/codes.py`: `CODE_TYPES` vs `GRANT_TYPES`). Đặt hai hằng
#: riêng để lần sau ai đó đổi một bên thì bên kia không âm thầm đi theo.
CODE_TYPE = "tanthu"
GRANT_TYPE = "tanthu"

#: Hậu tố khoá cooldown, trùng tên handler trong `keyboards.ROUTE_TABLE` (§13.6.6).
COOLDOWN_ACTION = "tan_thu"
DEFAULT_COOLDOWN_SECONDS = 5.0
DEFAULT_TANTHU_VALUE_VND = 10_000

_SQL_STATE = """
SELECT (u.verified_at IS NOT NULL) AS verified,
       EXISTS (SELECT 1
                 FROM code_grants g
                WHERE g.user_id = u.user_id
                  AND g.grant_type = :grant_type) AS has_grant
  FROM users u
 WHERE u.user_id = :uid
"""


@dataclass(frozen=True, slots=True)
class TanThuState:
    verified: bool
    has_grant: bool


async def load_state(db: AsyncSession, user_id: int) -> TanThuState:
    """Hai câu hỏi của bước 0 và bước 1, trả lời trong **một** lượt đọc.

    Người chưa từng `/start` không có hàng nào — coi như chưa xác minh, chưa có grant.
    """
    row = (
        await db.execute(text(_SQL_STATE), {"uid": user_id, "grant_type": GRANT_TYPE})
    ).one_or_none()
    if row is None:
        return TanThuState(verified=False, has_grant=False)
    return TanThuState(verified=row.verified, has_grant=row.has_grant)


# ── Nút `🎁Code Tân Thủ` ────────────────────────────────────────────


async def handle_tanthu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    tg_user = update.effective_user

    # Cùng luật với `/start`: trong nhóm bot im lặng tuyệt đối.
    if message is None or chat is None or tg_user is None or chat.type != "private":
        return

    sender = context.application.bot_data["sender"]

    try:
        cooldown = await settings_service.get_float(
            f"cooldown.{COOLDOWN_ACTION}", DEFAULT_COOLDOWN_SECONDS
        )
        await check_cooldown(tg_user.id, COOLDOWN_ACTION, cooldown)
    except RateLimited as exc:
        await sender.send_message(
            chat.id,
            await text_service.render("error.rate_limited", seconds=exc.retry_after_seconds),
        )
        return

    async with session() as db:
        state = await load_state(db, tg_user.id)
        chats = await membership.required_chats(db)

    if state.has_grant:
        game_link = await settings_service.get_str("link.game_bot", "")
        await sender.send_message(
            chat.id,
            await text_service.render("tanthu.already_claimed", game_link=game_link),
            reply_markup=keyboards.enter_code_keyboard(game_link) if game_link else None,
        )
        return

    if not state.verified:
        url = await gate.verify_url()
        if not url:
            log.error("thieu_cau_hinh", key=gate.VERIFY_URL_KEY)
            await sender.send_message(chat.id, await text_service.render("error.system_busy"))
            return
        await sender.send_message(
            chat.id,
            await text_service.render("tanthu.step1"),
            reply_markup=keyboards.verify_step1_keyboard(url),
        )
        return

    if not chats:
        # Không có nhóm bắt buộc nào đang bật là lỗi cấu hình (G0.3 yêu cầu ít nhất một).
        # Hiện màn hình bước 2 rỗng thì người dùng không có gì để làm mà vẫn bị chặn.
        log.error("khong_co_nhom_bat_buoc")
        await sender.send_message(chat.id, await text_service.render("error.system_busy"))
        return

    fanpage_link = await settings_service.get_str("link.fanpage", "")
    invite_links = [row.invite_link for row in chats]
    await sender.send_message(
        chat.id,
        await text_service.render(
            "tanthu.step2",
            total=len(invite_links),
            invite_list=texts.numbered_links(invite_links),
            fanpage_link=fanpage_link,
        ),
        reply_markup=keyboards.check_groups_keyboard(),
    )


# ── Callback `check_groups` ─────────────────────────────────────────


async def handle_check_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    tg_user = update.effective_user
    if query is None or chat is None or tg_user is None:
        return

    sender = context.application.bot_data["sender"]

    # TRƯỚC MỌI THỨ KHÁC (§13.3.3). `answer_callback` nuốt lỗi của chính nó nên dòng này
    # không bao giờ làm đổ luồng phía dưới.
    await sender.answer_callback(query, await text_service.render("alert.checking"))

    if not await gate.require_verified(update, context):
        return

    try:
        async with transaction() as db:
            missing, joined, total = await membership.check_membership(db, context.bot, tg_user.id)
    except membership.MembershipUnavailable as exc:
        # Bot bị gỡ quyền admin, chat_id chết, hoặc Telegram lỗi. §13.2.3 bước 4: không
        # chặn người dùng vì lỗi hạ tầng của mình.
        log.error("kiem_nhom_that_bai", user_id=tg_user.id, chat_id=exc.chat_id)
        await sender.send_message(chat.id, await text_service.render("error.system_busy"))
        return

    if total == 0:
        log.error("khong_co_nhom_bat_buoc")
        await sender.send_message(chat.id, await text_service.render("error.system_busy"))
        return

    if missing:
        # Nói ĐÚNG nhóm nào còn thiếu. `check_membership` trả về `chat_id`, còn thứ người
        # dùng bấm được là link mời — nên phải nối lại qua `required_chats`. Đọc theo đúng
        # `sort_order` để danh sách này khớp thứ tự người ta vừa thấy ở màn BƯỚC 2.
        async with session() as db:
            chats = await membership.required_chats(db)
        thieu = {int(c) for c in missing}
        link_thieu = [row.invite_link for row in chats if row.chat_id in thieu]

        await sender.send_message(
            chat.id,
            await text_service.render(
                "tanthu.missing_groups",
                joined=joined,
                total=total,
                con_thieu=len(link_thieu),
                missing_list=texts.numbered_links(link_thieu),
            ),
            # Gắn lại nút để người dùng bấm ngay tại tin này, không phải cuộn lên tìm.
            reply_markup=keyboards.check_groups_keyboard(),
        )
        return

    value_vnd = await settings_service.get_int("code.tanthu_value_vnd", DEFAULT_TANTHU_VALUE_VND)
    support_link = await settings_service.get_str("link.support", "")

    try:
        async with transaction() as db:
            grant = await code_issuance.reserve(
                db,
                user_id=tg_user.id,
                grant_type=GRANT_TYPE,
                grant_key=grant_key_tanthu(tg_user.id),
                code_type=CODE_TYPE,
                value_vnd=value_vnd,
            )
    except OutOfStock:
        await sender.send_message(
            chat.id,
            await text_service.render("code.out_of_stock", support_link=support_link),
        )
        return
    except AlreadyClaimed:
        # Grant đã có nhưng CHƯA gắn được mã — lần trước chạm đúng lúc kho rỗng. Với người
        # dùng thì tình huống này là "code đang hết", không phải "bạn đã nhận rồi".
        log.warning("grant_tanthu_chua_co_ma", user_id=tg_user.id)
        await sender.send_message(
            chat.id,
            await text_service.render("code.out_of_stock", support_link=support_link),
        )
        return

    game_link = await settings_service.get_str("link.game_bot", "")
    sent = await sender.send_message(
        chat.id,
        await text_service.render(
            "code.delivered", code_value=grant.code_value, value_vnd=grant.value_vnd
        ),
        reply_markup=keyboards.enter_code_keyboard(game_link) if game_link else None,
    )
    if sent is None:
        # Người này vĩnh viễn không nhận được tin (đã chặn bot). Grant nằm lại ở
        # `reserved`; `reap_reservations()` trả mã về kho. KHÔNG đánh dấu đã giao.
        log.warning("gui_code_tanthu_that_bai", user_id=tg_user.id, grant_id=grant.grant_id)
        return

    async with transaction() as db:
        await code_issuance.mark_delivered(db, grant_id=grant.grant_id)

    log.info(
        "phat_code_tanthu",
        user_id=tg_user.id,
        grant_id=grant.grant_id,
        value_vnd=grant.value_vnd,
        bam_lai=grant.was_existing,
    )


__all__ = [
    "CODE_TYPE",
    "COOLDOWN_ACTION",
    "GRANT_TYPE",
    "TanThuState",
    "handle_check_groups",
    "handle_tanthu",
    "load_state",
]
