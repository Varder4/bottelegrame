"""Lệnh `/send_event` — bắn tin đập hộp (§13.4.2 mục 9, §13.2.9, §13.5.1).

    /send_event [lời dẫn] [--all] [--force-small]

Ba luật, mỗi luật vá một chỗ bot cũ đã trả giá:

1. **Caption luôn sinh từ `settings.event.prize_table`.** `[lời dẫn]` của admin chỉ được
   ĐẶT TRƯỚC khối tỉ lệ, không thay được nó. Không có đường nào gõ tay một bộ số khác vào
   tin gửi đi — đó là toàn bộ nội dung của §13.5 dòng E, và là lý do lệnh này không nhận
   tham số tỉ lệ nào.

2. **Thiếu mệnh giá trong kho thì TỪ CHỐI CHẠY** (`settings.event.require_full_stock`).
   Quảng cáo giải 88K trong khi kho 88K rỗng là lừa người dùng; bot cũ không kiểm, nên
   "trúng nhưng hết code" thành trải nghiệm thường gặp.

3. **Gõ lệnh KHÔNG gửi gì cả.** Lệnh dựng event + đợt `draft`, đếm đích thật, in bản xem
   thử, rồi chờ một cú bấm xác nhận — cùng luật với `/broadcast`.

Hai nút xác nhận mang tiền tố riêng (`ev_confirm_` / `ev_cancel_`) chứ không dùng lại nút
của `/broadcast`: nút kia được gác bằng quyền `/broadcast` (chỉ `owner`), trong khi
`/send_event` là `owner` **và** `admin` — dùng chung thì admin tạo được event mà không
bấm được nút xác nhận của chính mình.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers.admin import broadcast as bc
from televip.apps.worker.handlers.admin import texts as admin_texts
from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import broadcast as broadcast_service
from televip.services import event_box, settings_service
from televip.services.admin import Handler, admin_command
from televip.telegram import keyboards

log = get_logger(__name__)

CMD_SEND_EVENT: Final = "/send_event"

#: Kiểu đợt trong `broadcast_jobs.kind` — khớp `broadcast_service.KINDS`.
JOB_KIND: Final = "send_event"

CB_CONFIRM_PREFIX: Final = "ev_confirm_"
CB_CANCEL_PREFIX: Final = "ev_cancel_"
CALLBACK_PATTERN: Final = r"^ev_(confirm|cancel)_\d+$"
_CB_RE: Final = re.compile(r"^ev_(confirm|cancel)_(\d+)$")

USAGE: Final = (
    "⚠️ Cú pháp: /send_event [lời dẫn] [--all] [--force-small]\n"
    "\n"
    "Ví dụ:\n"
    "• /send_event   (chỉ khối tỉ lệ, không lời dẫn)\n"
    "• /send_event Giờ vàng 21h nổ hộp!\n"
    "• /send_event Giờ vàng 21h nổ hộp! --all   (bắn TOÀN BỘ, mặc định chỉ tệp "
    f"{broadcast_service.ACTIVE_WINDOW_DAYS} ngày)\n"
    "\n"
    "👉 Khối tỉ lệ luôn sinh từ settings.event.prize_table — không sửa được bằng lời dẫn.\n"
    "👉 Lệnh này KHÔNG gửi ngay: bạn sẽ thấy bản xem thử và phải bấm xác nhận."
)


@dataclass(frozen=True, slots=True)
class SendEventArgs:
    intro: str
    audience: str
    force_small: bool


def parse_args(raw: str) -> SendEventArgs:
    """Tách cờ khỏi lời dẫn, **giữ nguyên xuống dòng**. Cờ lạ bị từ chối.

    Nhận chuỗi THÔ chứ không nhận `context.args`: thư viện dựng `args` bằng
    `text.split()`, thứ nuốt sạch ký tự xuống dòng. Lời dẫn của một đợt event là nội dung
    nhiều dòng do admin soạn, ép nó thành một đoạn liền là làm hỏng bài đăng — đúng lỗi
    `/broadcast` đã phải sửa, đường event chỉ là chưa được che.

    Ném:
        bc.BroadcastArgError: gặp cờ không hiểu, hoặc lời dẫn quá dài.
    """
    audience = "active_30d"
    force_small = False
    kept: list[str] = []

    # Tách theo TỪ để nhận diện cờ, nhưng ghép lại theo DÒNG để giữ bố cục.
    for line in raw.split("\n"):
        words: list[str] = []
        for token in line.split(" "):
            if not token.startswith("--"):
                words.append(token)
                continue
            if token == bc.FLAG_ALL:
                audience = "all"
            elif token == bc.FLAG_FORCE_SMALL:
                force_small = True
            else:
                raise bc.BroadcastArgError(
                    f"Cờ không hiểu: {token} (chỉ có {bc.FLAG_ALL}, {bc.FLAG_FORCE_SMALL})"
                )
        kept.append(" ".join(w for w in words if w).strip())

    intro = "\n".join(kept).strip()

    # Chặn ngay tại đây, trước khi có tin nào bay đi. Vượt trần 4.096 của Telegram là
    # `BadRequest`, thứ mà lớp gửi coi là lỗi vĩnh viễn nên không thử lại: cả đợt chạy
    # hết vòng, đếm đủ số đích, và KHÔNG MỘT AI nhận được gì.
    if len(intro) > bc.MAX_CONTENT_CHARS:
        raise bc.BroadcastArgError(
            f"Lời dẫn dài {len(intro):,} ký tự, trần một tin Telegram là 4.096 "
            f"(mức an toàn {bc.MAX_CONTENT_CHARS:,}, vì khối tỉ lệ còn được nối thêm phía sau)."
        )

    return SendEventArgs(intro=intro, audience=audience, force_small=force_small)


def build_caption(intro: str, odds_caption: str) -> str:
    """Lời dẫn (nếu có) + khối tỉ lệ. Khối tỉ lệ **luôn** ở đó."""
    return f"{intro}\n\n{odds_caption}" if intro else odds_caption


# ── Tiện ích ────────────────────────────────────────────────────────


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _actor_id(update: Update) -> int:
    tg_user = update.effective_user
    return tg_user.id if tg_user is not None else 0


async def _reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    body: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await _sender(context).send_message(chat.id, body, reply_markup=reply_markup)


def confirm_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 GỬI NGAY", callback_data=f"{CB_CONFIRM_PREFIX}{job_id}"),
                InlineKeyboardButton("🛑 HUỶ", callback_data=f"{CB_CANCEL_PREFIX}{job_id}"),
            ]
        ]
    )


def render_stock_refusal(missing: list[event_box.StockRow]) -> str:
    """Bản từ chối khi kho thiếu mệnh giá đang được quảng cáo (§13.5.1 điều kiện 1)."""
    lines = "\n".join(
        f"• {texts.value_label(row.value_vnd)} (tỉ lệ {row.weight_bp / 100:g}%): kho ĐANG RỖNG"
        for row in missing
    )
    nap = " ".join(
        f"/add_giffcode {event_box.CODE_TYPE} {texts.value_label(row.value_vnd).lower()}"
        for row in missing[:1]
    )
    return (
        "🛑 TỪ CHỐI — chưa bắn event nào\n"
        "\n"
        "❌ Bảng tỉ lệ đang quảng cáo những mệnh giá mà kho không có:\n"
        f"{lines}\n"
        "\n"
        "Quảng cáo một giải mà kho rỗng là hứa suông với người dùng — bot cũ không kiểm, "
        'nên "trúng nhưng hết code" thành chuyện thường ngày.\n'
        "\n"
        f"👉 Nạp code: {nap} CODE001 CODE002\n"
        f"👉 Xem kho: /tonkho\n"
        f"👉 Bỏ kiểm (KHÔNG khuyến nghị): /setcauhinh {event_box.REQUIRE_FULL_STOCK_KEY} false"
    )


def render_preview(
    *,
    event_id: int,
    job_id: int,
    caption: str,
    args: SendEventArgs,
    total: int,
    stock: list[event_box.StockRow],
    cost_per_turn: float,
    budget_cap_vnd: int,
) -> str:
    stock_lines = "\n".join(
        f"• {texts.value_label(row.value_vnd)} (tỉ lệ {row.weight_bp / 100:g}%): "
        f"kho {texts.format_vnd(row.available)}"
        for row in stock
    )
    turns = math.floor(budget_cap_vnd / cost_per_turn) if cost_per_turn > 0 else 0
    return (
        f"🎁 XEM THỬ EVENT #{event_id} (đợt #{job_id}) — CHƯA GỬI GÌ CẢ\n"
        f"{texts.SEP_WIDE}\n"
        f"{caption}\n"
        f"{texts.SEP_WIDE}\n"
        "📦 Kho theo bảng tỉ lệ đang chạy:\n"
        f"{stock_lines}\n"
        "\n"
        f"💸 Chi phí kỳ vọng: {texts.format_vnd(round(cost_per_turn))}đ/lượt · "
        f"trần đợt {texts.format_vnd(budget_cap_vnd)}đ (~{texts.format_vnd(turns)} lượt)\n"
        f"👥 Tệp nhận: {bc.AUDIENCE_LABEL[args.audience]}\n"
        f"🎯 Số đích: {texts.format_vnd(total)} người\n"
        f"⏳ Ước tính gửi xong: {bc.format_duration(bc.estimate_seconds(total))}\n"
        "\n"
        "ℹ️ Cửa sổ mở hộp tính từ lúc TỪNG người nhận được tin, không từ lúc bấm nút này.\n"
        f"⚠️ Bấm GỬI NGAY là bắt đầu gửi thật cho {texts.format_vnd(total)} người."
    )


# ── /send_event ─────────────────────────────────────────────────────


@admin_command(CMD_SEND_EVENT)
async def cmd_send_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dựng event + đợt `draft`, in bản xem thử. **Không gửi gì.**"""
    try:
        args = parse_args(admin_texts.raw_argument(update))
    except bc.BroadcastArgError as exc:
        await _reply(update, context, f"⚠️ {exc.hint}\n\n{USAGE}")
        return

    actor = _actor_id(update)

    try:
        async with session() as db:
            prizes = await event_box.prize_table(db)
            stock = await event_box.stock_report(db)
            require_full_stock = await settings_service.get_bool(
                event_box.REQUIRE_FULL_STOCK_KEY, db=db
            )
            budget_cap_vnd = await settings_service.get_int(event_box.BUDGET_CAP_KEY, db=db)
            odds_caption = await event_box.render_caption(db)
    except ConfigError as exc:
        await _reply(
            update,
            context,
            f"🛑 TỪ CHỐI — cấu hình event không hợp lệ\n\n❌ {exc}\n\n"
            f"👉 Xem: /cauhinh {event_box.PRIZE_TABLE_KEY}",
        )
        return

    missing = [row for row in stock if row.missing]
    if require_full_stock and missing:
        await _reply(update, context, render_stock_refusal(missing))
        log.warning("send_event_tu_choi_thieu_kho", thieu=[row.value_vnd for row in missing])
        return

    caption = build_caption(args.intro, odds_caption)

    async with transaction() as db:
        event_id = await event_box.create_event(db, created_by=actor, caption=caption)
        job_id = await broadcast_service.create_job(
            db,
            kind=JOB_KIND,
            audience=args.audience,
            payload={
                "text": caption,
                "reply_markup": keyboards.open_box_keyboard(event_id).to_dict(),
            },
            created_by=actor,
        )
        # Gắn job vào event NGAY: `open_box` đọc `broadcast_targets.sent_at` qua chính
        # `events.job_id`, và thiếu nó thì cửa sổ 10 phút rơi về mốc toàn cục của bot cũ.
        await event_box.attach_job(db, event_id=event_id, job_id=job_id)
        total = await broadcast_service.materialize_targets(
            db, job_id=job_id, audience=args.audience
        )
        minimum = await bc.min_audience(db)
        too_small = total < minimum and not args.force_small
        if too_small:
            await broadcast_service.cancel(db, job_id=job_id)
            await event_box.close_event(db, event_id=event_id)

    if too_small:
        await _reply(
            update,
            context,
            f"🛑 TỪ CHỐI — event #{event_id} đã huỷ\n"
            "\n"
            f"🎯 Số đích: {texts.format_vnd(total)} người\n"
            f"📉 Ngưỡng tối thiểu ({bc.MIN_AUDIENCE_KEY}): {texts.format_vnd(minimum)}\n"
            "\n"
            "Số đích thấp bất thường thường là bộ lọc sai, không phải tệp nhỏ.\n"
            f"👉 Vẫn muốn gửi: thêm {bc.FLAG_FORCE_SMALL} vào cuối lệnh.",
        )
        return

    await _reply(
        update,
        context,
        render_preview(
            event_id=event_id,
            job_id=job_id,
            caption=caption,
            args=args,
            total=total,
            stock=stock,
            cost_per_turn=event_box.expected_cost_vnd(prizes),
            budget_cap_vnd=budget_cap_vnd,
        ),
        reply_markup=confirm_keyboard(job_id),
    )


# ── Nút xác nhận ────────────────────────────────────────────────────


async def handle_send_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vỏ ngoài: **trả lời callback trước, kiểm quyền sau** (cùng lý do như `/broadcast`)."""
    query = update.callback_query
    if query is None:
        return
    await _sender(context).answer_callback(query)
    await _dispatch(update, context)


@admin_command(CMD_SEND_EVENT)
async def _dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    matched = _CB_RE.match(query.data or "") if query is not None else None
    if matched is None:
        return

    action, job_id = matched.group(1), int(matched.group(2))
    if action == "confirm":
        await _confirm(update, context, job_id)
    else:
        await _cancel(update, context, job_id)


async def _confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: int) -> None:
    async with transaction() as db:
        started = await broadcast_service.start(db, job_id=job_id)
        snapshot = await broadcast_service.status(db, job_id)
        event_id = await event_box.event_of_job(db, job_id=job_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    if started is None:
        await _reply(
            update,
            context,
            f"ℹ️ Đợt #{job_id} không còn ở trạng thái chờ xác nhận "
            f"(hiện: {bc.JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}).",
        )
        return

    broadcast_service.start_pump(job_id)
    window_minutes = await settings_service.get_int(event_box.WINDOW_MINUTES_KEY)

    await _reply(
        update,
        context,
        f"🚀 EVENT #{event_id} BẮT ĐẦU BẮN (đợt #{job_id})\n"
        "\n"
        f"🎯 Số đích: {texts.format_vnd(snapshot.total)} người\n"
        f"⏳ Ước tính: {bc.format_duration(bc.estimate_seconds(snapshot.total))}\n"
        f"⏱ Mỗi người có {window_minutes} phút KỂ TỪ LÚC HỌ NHẬN TIN.\n"
        "\n"
        f"👉 Theo dõi: {bc.CMD_STATUS} {job_id}\n"
        f"👉 Dừng gấp: {bc.CMD_CANCEL} {job_id}",
    )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: int) -> None:
    async with transaction() as db:
        result = await broadcast_service.cancel(db, job_id=job_id)
        snapshot = await broadcast_service.status(db, job_id)
        event_id = await event_box.event_of_job(db, job_id=job_id)
        if result.cancelled and event_id is not None:
            # Đóng luôn event: vài trăm tin có thể đã bay trước khi ai đó bấm huỷ, và một
            # event còn mở nhưng không ai theo dõi là một đường phát tiền không có người gác.
            await event_box.close_event(db, event_id=event_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    if not result.cancelled:
        await _reply(
            update,
            context,
            f"ℹ️ Đợt #{job_id} đã kết thúc từ trước "
            f"({bc.JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}), không có gì để huỷ.",
        )
        return

    await _reply(
        update,
        context,
        f"🛑 ĐÃ HUỶ EVENT #{event_id} (đợt #{job_id})\n"
        "\n"
        f"⏭ Bỏ qua {texts.format_vnd(result.skipped_targets)} đích chưa tới lượt.\n"
        f"🗑 Rút {texts.format_vnd(result.dropped_outbox)} tin đã vào hàng đợi nhưng chưa gửi.\n"
        f"✅ Đã gửi trước khi huỷ: {texts.format_vnd(snapshot.sent)} tin — "
        "event đã được đóng nên nút đập hộp của những tin đó không còn tác dụng.",
    )


# ── Đăng ký ─────────────────────────────────────────────────────────

#: Tên lệnh (không có dấu `/`) → handler. `main.build_application()` lặp qua đây.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (("send_event", cmd_send_event),)


__all__ = [
    "CALLBACK_PATTERN",
    "CB_CANCEL_PREFIX",
    "CB_CONFIRM_PREFIX",
    "CMD_SEND_EVENT",
    "COMMANDS",
    "USAGE",
    "SendEventArgs",
    "build_caption",
    "cmd_send_event",
    "confirm_keyboard",
    "handle_send_event_callback",
    "parse_args",
    "render_preview",
    "render_stock_refusal",
]
