"""Nút `✅ Điểm Danh` và `🎁 Đổi CODE` — §13.2.7 và §13.2.8.

Ba luật của tầng handler ở khối này:

1. **Câu chữ đi qua `text_service.render`**, không gọi thẳng `domain/texts.py`. Chủ bot sửa
   được mọi câu bằng `/suanoidung` mà không deploy.
   Ngoại lệ **duy nhất** là hai hàm dựng khối danh sách (`checkin_tier_lines`,
   `redeem_tier_lines`): chúng sinh trong một vòng lặp nên template không diễn đạt được,
   và `text_service` cố ý nhận chúng dưới dạng biến `{tier_lines}` đã dựng sẵn (xem
   docstring `services/text_service.py`, mục "Cái KHÔNG nằm trong bảng này").
2. **Không con số nghiệp vụ nào viết cứng** — bậc đổi, điểm mỗi ngày và cooldown đều đọc
   từ `settings`.
3. **Cấp code đi đúng một đường**: `services.checkin.redeem()` → `code_issuance.reserve()`,
   và `mark_delivered()` chỉ chạy **sau khi** Telegram xác nhận đã gửi. Hệ cũ đánh dấu
   trước rồi mới gửi, nên mỗi lần gửi lỗi là một mã bốc hơi khỏi kho.

Về thứ tự `answerCallbackQuery` ở luồng đổi code: §13.2.8 quy định alert **phụ thuộc kết
quả** (`✅ Đổi code thành công!` / `❌ CODE ĐÃ HẾT!` / `⚠️ Không đủ điểm!`), mà Telegram chỉ
cho answer một lần cho mỗi query. Vì vậy ở đây answer đứng **sau** giao dịch chứ không
trước như `tanthu.py` — hợp lệ trong mốc 2 giây của §13.3.3 vì mọi việc trước đó là thao
tác database thuần, không có lời gọi Bot API nào (khác `check_groups`, vốn phải hỏi
`getChatMember` và tốn hàng giây).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers import gate
from televip.cache.antispam import check_cooldown
from televip.core.errors import AlreadyClaimed, ConfigError, OutOfStock, RateLimited
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import checkin as checkin_service
from televip.services import code_issuance, settings_service, text_service
from televip.telegram import keyboards

log = get_logger(__name__)

#: Hậu tố khoá cooldown, trùng tên handler trong `keyboards.ROUTE_TABLE` (§13.6.6).
COOLDOWN_CHECKIN = "checkin"
COOLDOWN_REDEEM = "redeem_code"
DEFAULT_COOLDOWN_CHECKIN = 7.0
DEFAULT_COOLDOWN_REDEEM = 5.0


# ── Nút `✅ Điểm Danh` ──────────────────────────────────────────────


async def handle_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    tg_user = update.effective_user
    # Cùng luật với `/start` và `🎁Code Tân Thủ`: trong nhóm bot im lặng tuyệt đối.
    if chat is None or tg_user is None or chat.type != "private":
        return

    sender = context.application.bot_data["sender"]
    if not await _pass_cooldown(
        sender, chat.id, tg_user.id, COOLDOWN_CHECKIN, DEFAULT_COOLDOWN_CHECKIN
    ):
        return
    if not await gate.require_verified(update, context):
        return

    async with transaction() as db:
        result = await checkin_service.do_checkin(db, tg_user.id)

    if result.already:
        await sender.send_message(
            chat.id,
            await text_service.render(
                "checkin.already", balance=result.balance, streak=result.streak
            ),
        )
        return

    tiers = await checkin_service.redeem_tiers()
    await sender.send_message(
        chat.id,
        await text_service.render(
            "checkin.success",
            points=result.points,
            balance=result.balance,
            streak=result.streak,
            tier_lines=texts.checkin_tier_lines(tiers),
        ),
    )
    log.info(
        "diem_danh",
        user_id=tg_user.id,
        business_date=result.business_day.isoformat(),
        streak=result.streak,
        points=result.points,
    )


# ── Nút `🎁 Đổi CODE` ───────────────────────────────────────────────


async def handle_redeem_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    tg_user = update.effective_user
    if chat is None or tg_user is None or chat.type != "private":
        return

    sender = context.application.bot_data["sender"]
    if not await _pass_cooldown(
        sender, chat.id, tg_user.id, COOLDOWN_REDEEM, DEFAULT_COOLDOWN_REDEEM
    ):
        return
    if not await gate.require_verified(update, context):
        return

    async with session() as db:
        balance = await checkin_service.balance(db, tg_user.id)

    tiers = await checkin_service.redeem_tiers()
    per_day = await checkin_service.points_per_day()
    # Chỉ bậc đủ điểm mới có nút: một nút bấm được rồi báo "không đủ điểm" là vòng thao
    # tác thừa (xem `keyboards.redeem_keyboard`). Bậc chưa đủ vẫn hiện trong nội dung tin.
    affordable = [tier for tier in tiers if balance >= tier]

    footer = await text_service.render(
        "redeem.menu_footer_ready" if affordable else "redeem.menu_footer_not_ready"
    )
    await sender.send_message(
        chat.id,
        await text_service.render(
            "redeem.menu",
            balance=balance,
            tier_lines=texts.redeem_tier_lines(
                balance=balance, tiers=tiers, points_per_day=per_day
            ),
            points_per_day=per_day,
            footer=footer,
        ),
        reply_markup=keyboards.redeem_keyboard(affordable) if affordable else None,
    )


# ── Callback `redeem_<value>` ───────────────────────────────────────


async def _bao_loi_cau_hinh(
    sender: Any, query: Any, chat_id: int, user_id: int, value_vnd: int
) -> None:
    """Trả lời nút rồi báo "hệ thống bận" — dùng cho mọi `ConfigError` của luồng đổi code.

    Việc BẮT BUỘC ở đây là `answer_callback`: chừng nào Telegram chưa nhận được nó, nút
    còn quay. Đó là khác biệt giữa "lỗi cấu hình, thử lại sau" và "bot treo".
    """
    await sender.answer_callback(query, await text_service.render("error.system_busy"))
    await sender.send_message(chat_id, await text_service.render("error.system_busy"))
    log.error("doi_code_loi_cau_hinh", user_id=user_id, value_vnd=value_vnd, exc_info=True)


async def handle_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    tg_user = update.effective_user
    if query is None or chat is None or tg_user is None:
        return

    sender = context.application.bot_data["sender"]

    value_vnd = parse_redeem_value(query.data)
    if value_vnd is None:
        # `callback_data` bịa tay. Vẫn phải answer, nếu không nút quay vòng mãi (§13.3.3).
        await sender.answer_callback(query)
        log.warning("callback_doi_code_khong_hop_le", data=query.data, user_id=tg_user.id)
        return

    if not await gate.is_verified(tg_user.id):
        await sender.answer_callback(query, await text_service.render("alert.need_verify"))
        await gate.require_verified(update, context)
        return

    try:
        cooldown = await settings_service.get_float(
            f"cooldown.{COOLDOWN_REDEEM}", DEFAULT_COOLDOWN_REDEEM
        )
        await check_cooldown(tg_user.id, COOLDOWN_REDEEM, cooldown)
    except RateLimited as exc:
        await sender.answer_callback(
            query,
            await text_service.render("error.rate_limited", seconds=exc.retry_after_seconds),
        )
        return
    except ConfigError:
        await _bao_loi_cau_hinh(sender, query, chat.id, tg_user.id, value_vnd)
        return

    try:
        support_link = await settings_service.get_str("link.support", "")
    except ConfigError:
        await _bao_loi_cau_hinh(sender, query, chat.id, tg_user.id, value_vnd)
        return

    try:
        async with transaction() as db:
            grant = await checkin_service.redeem(db, user_id=tg_user.id, value_vnd=value_vnd)
            balance = await checkin_service.balance(db, tg_user.id)
    except checkin_service.NotEnoughPoints as exc:
        await sender.answer_callback(
            query, await text_service.render("alert.not_enough_points"), show_alert=True
        )
        await sender.send_message(
            chat.id,
            await text_service.render(
                "redeem.not_enough", balance=exc.balance, value_vnd=value_vnd
            ),
        )
        return
    except (OutOfStock, AlreadyClaimed):
        # Giao dịch đã cuộn lại: điểm KHÔNG bị trừ. `AlreadyClaimed` tới được đây khi lượt
        # trước tạo grant nhưng chưa gắn được mã — với người dùng đó vẫn là "code đang hết".
        async with session() as db:
            balance = await checkin_service.balance(db, tg_user.id)
        await sender.answer_callback(
            query, await text_service.render("alert.out_of_stock"), show_alert=True
        )
        await sender.send_message(
            chat.id,
            await text_service.render(
                "redeem.out_of_stock",
                value_vnd=value_vnd,
                balance=balance,
                support_link=support_link,
            ),
        )
        log.warning("doi_code_het_kho", user_id=tg_user.id, value_vnd=value_vnd)
        return
    except checkin_service.InvalidRedeemTier:
        await sender.answer_callback(query)
        log.warning("doi_code_bac_khong_hop_le", user_id=tg_user.id, value_vnd=value_vnd)
        return
    except ConfigError:
        # `redeem()` đọc `redeem.tiers` và `checkin.points_per_day`; một giá trị hỏng ở
        # đó ném từ TẬN TRONG giao dịch. Không có nhánh này thì ngoại lệ thoát khỏi
        # handler, `answer_callback` không bao giờ được gọi, và nút quay vòng ~15 giây
        # rồi báo lỗi mạng — người dùng đọc ra "bot chết", còn admin thì không thấy gì
        # ngoài một traceback không gắn với ai.
        await _bao_loi_cau_hinh(sender, query, chat.id, tg_user.id, value_vnd)
        return

    await sender.answer_callback(query, await text_service.render("alert.redeem_ok"))

    game_link = await settings_service.get_str("link.game_bot", "")
    sent = await sender.send_message(
        chat.id,
        await text_service.render(
            "redeem.success",
            code_value=grant.code_value,
            value_vnd=grant.value_vnd,
            balance=balance,
        ),
        reply_markup=keyboards.enter_code_keyboard(game_link) if game_link else None,
    )
    if sent is None:
        # Người này vĩnh viễn không nhận được tin (đã chặn bot). Grant nằm lại ở `reserved`
        # và `reap_reservations()` trả mã về kho. KHÔNG đánh dấu đã giao.
        log.warning("gui_code_doi_diem_that_bai", user_id=tg_user.id, grant_id=grant.grant_id)
        return

    async with transaction() as db:
        await code_issuance.mark_delivered(db, grant_id=grant.grant_id)

    log.info(
        "doi_code_thanh_cong",
        user_id=tg_user.id,
        grant_id=grant.grant_id,
        value_vnd=grant.value_vnd,
        bam_lai=grant.was_existing,
    )


def parse_redeem_value(data: str | None) -> int | None:
    """`"redeem_10000"` → `10000`. Chuỗi lạ → None.

    Cắt bằng `removeprefix` chứ không `split('_')`: mệnh giá là số nên tiền tố cố định là
    cách tách duy nhất không mơ hồ (`keyboards.CB_REDEEM_PREFIX`).
    """
    if not data or not data.startswith(keyboards.CB_REDEEM_PREFIX):
        return None
    raw = data.removeprefix(keyboards.CB_REDEEM_PREFIX)
    return int(raw) if raw.isdigit() else None


async def _pass_cooldown(
    sender: Any, chat_id: int, user_id: int, action: str, default_seconds: float
) -> bool:
    """False nghĩa là đang trong thời gian chờ **và** người dùng đã được báo."""
    try:
        seconds = await settings_service.get_float(f"cooldown.{action}", default_seconds)
        await check_cooldown(user_id, action, seconds)
    except RateLimited as exc:
        await sender.send_message(
            chat_id,
            await text_service.render("error.rate_limited", seconds=exc.retry_after_seconds),
        )
        return False
    return True


#: Nhãn nút → handler, cho khối 4 đăng ký `MessageHandler` vào `main.py`.
#: Là bảng dữ liệu chứ không phải hai dòng `app.add_handler` để không có cách nào đăng ký
#: nhầm nhãn — nhãn lấy từ `keyboards`, cùng nguồn với `ROUTE_TABLE`.
BUTTON_HANDLERS: list[tuple[str, Callable[..., Coroutine[Any, Any, None]]]] = [
    (keyboards.BTN_DIEM_DANH, handle_checkin),
    (keyboards.BTN_DOI_CODE, handle_redeem_menu),
]

#: Pattern regex → handler, cho khối 4 đăng ký `CallbackQueryHandler`.
CALLBACK_HANDLERS: list[tuple[str, Callable[..., Coroutine[Any, Any, None]]]] = [
    (f"^{keyboards.CB_REDEEM_PREFIX}\\d+$", handle_redeem),
]


__all__ = [
    "BUTTON_HANDLERS",
    "CALLBACK_HANDLERS",
    "COOLDOWN_CHECKIN",
    "COOLDOWN_REDEEM",
    "DEFAULT_COOLDOWN_CHECKIN",
    "DEFAULT_COOLDOWN_REDEEM",
    "handle_checkin",
    "handle_redeem",
    "handle_redeem_menu",
    "parse_redeem_value",
]
