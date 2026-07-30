"""Mời bạn bè — ba màn hình và một móc nối, theo §13.2.4 và §13.2.14.

    nút `💎 Mời bạn nhận quà`  → khối chiến dịch + link riêng          (13.2.4)
    nút `📈Check Chia sẻ`      → khối tiến độ + khối chiến dịch        (13.2.14)
    callback `join_referral`   → hướng dẫn + bài viết để chuyển tiếp   (13.2.4)
    on_referee_verified()      → chốt referral rồi phát bù mọi mốc     (13.2.2 bước 6)

Ba điều file này cố ý làm khác bot cũ:

1. **Khối chiến dịch có đúng MỘT bản.** Hai màn hình gọi cùng `_campaign_block()`, cùng
   khoá câu chữ, cùng công thức đếm ngược. §13.2.14 nói thẳng: "không được có bản sao thứ
   hai của đoạn văn đó" — bản sao là thứ trôi khác nhau rồi nói hai điều trái ngược.
2. **`claimed` đếm `referral_rewards`, không phải `số người mời // 5`.** Hai con số đó
   lệch nhau ngay khi có một referral bị từ chối vì rủi ro, và khi ấy màn hình báo người
   dùng đủ điều kiện trong khi hệ thống không nợ họ mốc nào (§13.5 dòng A).
3. **Gửi xong mới `mark_delivered()`.** Gửi lỗi thì mã nằm lại ở `reserved` và job dọn trả
   nó về kho — không có nhánh nào đốt mã.

Câu chữ đi qua `text_service.render()`, không gọi thẳng `domain/texts.py`. Hai ngoại lệ
`texts.referral_link` và `texts.value_label` là **quy tắc dựng chuỗi** (sinh URL, đọc
mệnh giá), không phải câu chữ admin sửa được — xem docstring `services/text_service.py`.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers import gate
from televip.cache.antispam import check_cooldown
from televip.core.errors import AlreadyClaimed, OutOfStock, RateLimited
from televip.core.logging import get_logger
from televip.db.engine import session
from televip.domain import texts
from televip.services import code_issuance, referral, settings_service, text_service
from televip.services.admin import Handler
from televip.telegram import keyboards
from televip.telegram.sender import Sender

log = get_logger(__name__)

#: Hậu tố khoá cooldown, trùng tên handler trong `keyboards.ROUTE_TABLE` (§13.6.6).
COOLDOWN_MOI_BAN: Final = "moi_ban"
COOLDOWN_CHECK_SHARE: Final = "check_share"
DEFAULT_COOLDOWN_SECONDS: Final = 3.0


# ══════════════════════════════════════════════════════════════════════════════
# Dựng màn hình — dùng chung cho cả hai nút
# ══════════════════════════════════════════════════════════════════════════════


def _ref_link(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    """`https://t.me/<bot>?start=ref_<user_id>`."""
    return texts.referral_link(context.bot.username, user_id)


async def _countdown(window: referral.CampaignWindow) -> str:
    """Đếm ngược của chiến dịch — công thức bắt buộc của §13.2.4.

    Tính trên **tổng giây**, hai nhánh và chỉ hai nhánh. Bot cũ đọc `timedelta.days` và
    `.seconds` riêng lẻ; với timedelta âm thì `.days` âm (bị `max(0,·)` ép về 0) còn
    `.seconds` **vẫn dương**, nên một chiến dịch hết hạn từ 01/12/2025 vẫn hiện
    "Còn 0 ngày 13 giờ" mãi mãi (§13.5 dòng F).
    """
    if not window.is_running:
        return await text_service.render("referral.countdown_ended")
    return await text_service.render(
        "referral.countdown_running",
        days=window.remaining_seconds // 86_400,
        hours=(window.remaining_seconds % 86_400) // 3_600,
    )


async def _campaign_block(
    *,
    cfg: referral.ReferralParams,
    window: referral.CampaignWindow,
    claimed: int,
    ref_link: str,
) -> str:
    """Khối mô tả chiến dịch — bản DUY NHẤT, dùng chung cho 13.2.4 và 13.2.14."""
    return await text_service.render(
        "referral.campaign_block",
        countdown=await _countdown(window),
        reward_label=texts.value_label(cfg.reward_value_vnd),
        max_people=cfg.max_people,
        interval=cfg.interval,
        max_claims=cfg.max_claims,
        claimed=claimed,
        ref_link=ref_link,
    )


async def _status_line(*, cfg: referral.ReferralParams, snapshot: referral.Progress) -> str:
    """Dòng trạng thái của §13.2.14 — đúng **ba** trạng thái, không có trạng thái thứ tư.

    Trạng thái 3 là một **tín hiệu bất thường**, không phải trạng thái bình thường: mốc
    được phát bù ngay trong luồng xác minh của người được mời, nên "đủ người mà chưa có
    thưởng" chỉ có thể do kho `moibanbe` rỗng hoặc mã còn đang giữ chỗ. Câu chữ vì thế
    kèm luôn dòng "đang được xử lý" — bản cũ để trạng thái này đứng vĩnh viễn.
    """
    if snapshot.claimed >= cfg.max_claims:
        return await text_service.render("share.status_max", max_claims=cfg.max_claims)

    next_tier = snapshot.claimed + 1
    next_milestone = next_tier * cfg.interval
    people_needed = max(0, next_milestone - snapshot.qualified)

    if people_needed > 0:
        return await text_service.render(
            "share.status_waiting",
            people_needed=people_needed,
            next_tier=next_tier,
            next_milestone=next_milestone,
        )
    return await text_service.render("share.status_ready", next_tier=next_tier)


def _join_keyboard(window: referral.CampaignWindow) -> InlineKeyboardMarkup | None:
    """Nút `💎 Tham gia ngay` biến mất khi chiến dịch đã kết thúc (§13.2.4, §13.2.14)."""
    return keyboards.join_referral_keyboard() if window.is_running else None


async def _guard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    action: str,
) -> bool:
    """Cooldown + cổng xác minh. True nghĩa là được đi tiếp.

    Nhánh từ chối đều đã tự gửi tin cho người dùng, caller chỉ cần dừng.
    """
    sender = context.application.bot_data["sender"]
    try:
        seconds = await settings_service.get_float(f"cooldown.{action}", DEFAULT_COOLDOWN_SECONDS)
        await check_cooldown(user_id, action, seconds)
    except RateLimited as exc:
        await sender.send_message(
            chat_id,
            await text_service.render("error.rate_limited", seconds=exc.retry_after_seconds),
        )
        return False

    # Cổng chặn toàn cục §13.2.2: mọi nút chức năng đều bị chặn khi chưa xác minh.
    return await gate.require_verified(update, context)


# ══════════════════════════════════════════════════════════════════════════════
# Nút `💎 Mời bạn nhận quà` (§13.2.4)
# ══════════════════════════════════════════════════════════════════════════════


async def handle_moi_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    tg_user = update.effective_user
    # Cùng luật với `/start`: trong nhóm bot im lặng tuyệt đối.
    if chat is None or tg_user is None or chat.type != "private":
        return

    if not await _guard(
        update, context, chat_id=chat.id, user_id=tg_user.id, action=COOLDOWN_MOI_BAN
    ):
        return

    sender = context.application.bot_data["sender"]
    cfg = await referral.params()
    async with session() as db:
        claimed = await referral.count_claimed(db, tg_user.id)
        window = await referral.campaign_window(db)

    block = await _campaign_block(
        cfg=cfg,
        window=window,
        claimed=claimed,
        ref_link=_ref_link(context, tg_user.id),
    )
    await sender.send_message(
        chat.id,
        await text_service.render("referral.invite", campaign_block=block),
        reply_markup=_join_keyboard(window),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Nút `📈Check Chia sẻ` (§13.2.14)
# ══════════════════════════════════════════════════════════════════════════════


async def handle_check_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Màn hình **chỉ đọc**: không ghi một dòng nào vào database."""
    chat = update.effective_chat
    tg_user = update.effective_user
    if chat is None or tg_user is None or chat.type != "private":
        return

    if not await _guard(
        update, context, chat_id=chat.id, user_id=tg_user.id, action=COOLDOWN_CHECK_SHARE
    ):
        return

    sender = context.application.bot_data["sender"]
    cfg = await referral.params()
    # Ba con số trong MỘT lượt đọc — đọc rời rạc là màn hình tự mâu thuẫn với chính nó
    # (số người mời đã tăng nhưng số mốc đã nhận còn là số cũ).
    async with session() as db:
        snapshot = await referral.progress(db, tg_user.id)
        window = await referral.campaign_window(db)

    block = await _campaign_block(
        cfg=cfg,
        window=window,
        claimed=snapshot.claimed,
        ref_link=_ref_link(context, tg_user.id),
    )
    await sender.send_message(
        chat.id,
        await text_service.render(
            "share.progress",
            qualified=snapshot.qualified,
            claimed=snapshot.claimed,
            max_claims=cfg.max_claims,
            total_vnd=snapshot.total_vnd,
            status_line=await _status_line(cfg=cfg, snapshot=snapshot),
            campaign_block=block,
        ),
        reply_markup=_join_keyboard(window),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Callback `join_referral` (§13.2.4)
# ══════════════════════════════════════════════════════════════════════════════


async def handle_join_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gửi bài viết chia sẻ. Cùng một callback cho cả hai màn hình, xử lý ở đúng đây."""
    query = update.callback_query
    chat = update.effective_chat
    tg_user = update.effective_user
    if query is None or chat is None or tg_user is None:
        return

    sender = context.application.bot_data["sender"]
    # TRƯỚC MỌI THỨ KHÁC (§13.3.3 cho 2 giây). `answer_callback` nuốt lỗi của chính nó nên
    # dòng này không bao giờ làm đổ luồng phía dưới.
    await sender.answer_callback(query, await text_service.render("alert.loading_share_post"))

    if not await gate.require_verified(update, context):
        return

    ref_link = _ref_link(context, tg_user.id)
    await sender.send_message(chat.id, await text_service.render("referral.share_post_intro"))
    await sender.send_message(
        chat.id,
        await text_service.render("referral.share_post"),
        reply_markup=keyboards.share_post_keyboard(ref_link),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Móc nối: người được mời vừa xác minh xong (§13.2.2 bước 6 → §13.2.4)
# ══════════════════════════════════════════════════════════════════════════════


async def on_referee_verified(
    sender: Sender,
    db: AsyncSession,
    referee_id: int,
) -> list[int]:
    """Chốt referral cho `referee_id` rồi phát bù mọi mốc người mời đã đạt.

    Trả về danh sách `tier_no` đã phát **và gửi tới tay người mời** trong lượt này.

    **Hợp đồng gọi**

    * Gọi **sau khi** `users.verified_at` đã được ghi **và đã commit**. Gọi sớm hơn thì
      `qualify()` thấy người dùng chưa xác minh và giữ nguyên ý định giới thiệu.
    * `db` là một `AsyncSession` **chưa mở transaction** — hàm tự mở và đóng từng pha.
      Đó là chủ ý: giữa hai pha có một lời gọi mạng tới Telegram, và ôm một transaction
      qua lời gọi mạng nghĩa là giữ khoá hàng `codes` suốt thời gian đó.
    * Mỗi mốc nằm trong transaction **riêng**. Mốc 2 hết kho không được cuộn lại mốc 1 đã
      phát xong, và cũng không được cuộn lại chính quan hệ referral vừa chốt.

    Gọi lại với cùng `referee_id` là vô hại: ý định đã bị tiêu, `referrals.referee_id` là
    PK, và `(user_id, tier_no)` của `referral_rewards` là PK.
    """
    async with db.begin():
        referrer_id = await referral.qualify(db, referee_id=referee_id)
        window = await referral.campaign_window(db)

    if referrer_id is None:
        return []

    if not window.is_running:
        # §13.5 dòng F: hết hạn là ngừng phát THẬT, không chỉ ngừng hiển thị. Quan hệ
        # referral vẫn được ghi ở trên — người mời không mất công, chỉ không có mốc mới.
        log.info("chien_dich_khong_chay_khong_phat_moc", referrer_id=referrer_id)
        return []

    return await award_pending_tiers(sender, db, referrer_id)


async def award_pending_tiers(sender: Sender, db: AsyncSession, user_id: int) -> list[int]:
    cfg = await referral.params()
    game_link = await settings_service.get_str("link.game_bot", "")
    support_link = await settings_service.get_str("link.support", "")

    async with db.begin():
        tiers = await referral.pending_tiers(db, user_id)
        qualified = await referral.count_qualified(db, user_id)

    awarded: list[int] = []
    for tier_no in tiers:
        try:
            async with db.begin():
                grant = await referral.grant_tier(db, user_id=user_id, tier_no=tier_no)
        except OutOfStock:
            # Mốc KHÔNG được đánh dấu đã phát (transaction đã cuộn lại), để lần chạy sau
            # phát bù khi admin nạp thêm mã. Dừng luôn: các mốc sau cũng cùng một kho.
            log.warning("het_code_moc_moi_ban", user_id=user_id, tier_no=tier_no)
            await sender.send_message(
                user_id,
                await text_service.render(
                    "referral.milestone_out_of_stock",
                    qualified=qualified,
                    tier_no=tier_no,
                    max_claims=cfg.max_claims,
                    support_link=support_link,
                ),
            )
            break
        except AlreadyClaimed:
            # Một worker khác vừa phát đúng mốc này. Hàng rào DB đã làm việc của nó.
            log.info("moc_moi_ban_da_co_worker_khac_phat", user_id=user_id, tier_no=tier_no)
            continue

        sent = await sender.send_message(
            user_id,
            await text_service.render(
                "referral.milestone",
                tier_no=tier_no,
                max_claims=cfg.max_claims,
                qualified=qualified,
                code_value=grant.code_value,
                value_vnd=grant.value_vnd,
                interval=cfg.interval,
            ),
            reply_markup=keyboards.enter_code_keyboard(game_link) if game_link else None,
        )
        if sent is None:
            # Người mời đã chặn bot. Grant nằm lại ở `reserved`; `reap_reservations()` trả
            # mã về kho. KHÔNG đánh dấu đã giao — hệ cũ đánh dấu trước rồi mới gửi, nên mỗi
            # lần gửi lỗi là một mã bốc hơi khỏi kho mà không ai nhận được.
            log.warning("gui_moc_moi_ban_that_bai", user_id=user_id, grant_id=grant.grant_id)
            continue

        async with db.begin():
            await code_issuance.mark_delivered(db, grant_id=grant.grant_id)
        awarded.append(tier_no)

    return awarded


# ══════════════════════════════════════════════════════════════════════════════
# Đăng ký — khối 4 đọc hai bảng này, không gõ lại tên handler ở `main.py`
# ══════════════════════════════════════════════════════════════════════════════

#: Nhãn nút bàn phím chính → handler. Khớp BẰNG NHAU TUYỆT ĐỐI (`filters.Text([...])`).
BUTTON_HANDLERS: Final[tuple[tuple[str, Handler], ...]] = (
    (keyboards.BTN_MOI_BAN, handle_moi_ban),
    (keyboards.BTN_CHECK_CHIA_SE, handle_check_share),
)

#: `callback_data` → handler. Đăng ký bằng `pattern=f"^{data}$"`.
CALLBACK_HANDLERS: Final[tuple[tuple[str, Handler], ...]] = (
    (keyboards.CB_JOIN_REFERRAL, handle_join_referral),
)


__all__ = [
    "award_pending_tiers",
    "BUTTON_HANDLERS",
    "CALLBACK_HANDLERS",
    "COOLDOWN_CHECK_SHARE",
    "COOLDOWN_MOI_BAN",
    "handle_check_share",
    "handle_join_referral",
    "handle_moi_ban",
    "on_referee_verified",
]
