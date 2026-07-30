"""Lệnh bắn tin hàng loạt — `13-dac-ta-bot-moi.md` §13.4.2 (mục 8) và §13.4.3.

    /broadcast <nội dung> [--all] [--force-small]   tạo đợt, XEM THỬ rồi mới bắn
    /broadcast_status [job_id]                      tiến độ đợt đang chạy
    /broadcast_pause  [job_id]                      tạm dừng
    /broadcast_resume [job_id]                      chạy tiếp
    /broadcast_cancel [job_id]                      huỷ hẳn, chặn cả tin đã vào hàng đợi

Ba luật của file này, mỗi luật là một chỗ hệ cũ đã trả giá (`02-broadcast.md`):

1. **Gõ lệnh KHÔNG gửi gì cả.** Lệnh chỉ dựng đợt ở trạng thái `draft`, đếm đích thật, in
   bản xem thử kèm ước tính thời gian, rồi chờ một cú bấm xác nhận. Ở hệ cũ `/broadcast`
   bắn ngay từ ký tự cuối cùng của tin nhắn — một lần gõ nhầm là hàng nghìn tin đã bay và
   không có nút nào gọi chúng về.
2. **Mặc định là tệp hẹp.** Không có `--all` thì audience là `active_30d`. Bắn toàn tệp
   phải là một hành động người ta gõ ra một cách có ý thức, không phải giá trị mặc định.
3. **Tệp quá nhỏ thì từ chối.** Dưới `broadcast.min_audience` đích thường nghĩa là bộ lọc
   sai (ví dụ chưa ai `/start` sau khi đổi bot), chứ không phải "chỉ có ngần ấy người".
   Muốn gửi thật cho nhóm nhỏ thì gõ thêm `--force-small`.

Toàn bộ phản hồi là **văn bản thuần, không `parse_mode`**: nội dung đợt do admin dán vào
và một dấu `<` hay `*` lạc chỗ sẽ làm Telegram từ chối cả tin — khi đó admin không thấy
bản xem thử và sẽ gõ lại lệnh, đúng thứ mục 1 sinh ra để ngăn.

**Nội dung đọc thẳng từ `message.text`, KHÔNG qua `context.args`** — cùng lý do đã ghi ở
`handlers/admin/texts.py` điểm 2: `python-telegram-bot` dựng `args` bằng `text.split()`,
nên mọi ký tự xuống dòng biến mất và một thông báo ba đoạn bị ép thành một khối chữ liền.
Đợt bắn tin là chỗ hậu quả đắt nhất: nó đi tới hàng chục nghìn người và không thu hồi được.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers.admin.texts import raw_argument
from televip.cache.ratelimit import BULK_FLOOR
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import broadcast as broadcast_service
from televip.services import settings_service
from televip.services.admin import Handler, admin_command

log = get_logger(__name__)

# ── Hằng ────────────────────────────────────────────────────────────

CMD_BROADCAST: Final = "/broadcast"
CMD_STATUS: Final = "/broadcast_status"
CMD_PAUSE: Final = "/broadcast_pause"
CMD_RESUME: Final = "/broadcast_resume"
CMD_CANCEL: Final = "/broadcast_cancel"

FLAG_ALL: Final = "--all"
FLAG_FORCE_SMALL: Final = "--force-small"

#: Số đích tối thiểu của một đợt. Khoá seed ở migration `0006`; hằng số dưới đây chỉ là
#: đường lui khi bảng `settings` chưa được seed (bản cài mới, database test).
MIN_AUDIENCE_KEY: Final = "broadcast.min_audience"
DEFAULT_MIN_AUDIENCE: Final = 1

#: Callback của hai nút xác nhận. `main.py` đăng ký `^bc_(confirm|cancel)_\\d+$`.
CB_CONFIRM_PREFIX: Final = "bc_confirm_"
CB_CANCEL_PREFIX: Final = "bc_cancel_"
_CB_RE: Final = re.compile(r"^bc_(confirm|cancel)_(\d+)$")

#: Độ dài phần nội dung in trong bản xem thử. Trần một tin Telegram là 4096 ký tự và bản
#: xem thử còn phải chứa số liệu + cảnh báo.
PREVIEW_CHARS: Final = 1_200

#: Trần độ dài nội dung một đợt, chặn ngay lúc đọc tham số. Trần cứng của Telegram là
#: 4.096; vượt qua nó là `BadRequest`, thứ lớp gửi coi là **lỗi vĩnh viễn** nên không thử
#: lại. Với một đợt bắn tin, hậu quả không phải "một tin hỏng" mà là toàn bộ đợt chạy hết
#: vòng, đếm đủ số đích, và **không một ai nhận được gì**. Chừa 96 ký tự đệm, cùng con số
#: và cùng lý do với `text_service.MAX_RENDERED_LENGTH`.
MAX_CONTENT_CHARS: Final = 4_000

#: Nhãn tiếng Việt của `audience`, dùng trong mọi màn hình của file này.
AUDIENCE_LABEL: Final[dict[str, str]] = {
    "active_30d": f"hoạt động {broadcast_service.ACTIVE_WINDOW_DAYS} ngày gần đây",
    "all": "TOÀN BỘ người đã /start",
    "custom": "danh sách chỉ định",
}

JOB_STATE_LABEL: Final[dict[str, str]] = {
    "draft": "nháp, chờ xác nhận",
    "running": "đang chạy",
    "paused": "đang tạm dừng",
    "done": "đã xong",
    "cancelled": "đã huỷ",
}


class BroadcastArgError(ValueError):
    """Tham số lệnh `/broadcast` không đọc được. `hint` là câu chỉ cách gõ đúng."""

    def __init__(self, hint: str) -> None:
        self.hint = hint
        super().__init__(hint)


@dataclass(frozen=True, slots=True)
class BroadcastArgs:
    content: str
    audience: str
    force_small: bool


# ── Tiện ích chung ──────────────────────────────────────────────────


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


def _actor_id(update: Update) -> int:
    tg_user = update.effective_user
    # `admin_command` đã chặn `effective_user is None`; giữ nhánh này để type checker không
    # phải tin vào một bất biến nằm ở file khác.
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
    """Hai nút của bản xem thử. `job_id` nằm trong callback data nên hai đợt không lẫn nhau."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 GỬI NGAY", callback_data=f"{CB_CONFIRM_PREFIX}{job_id}"),
                InlineKeyboardButton("🛑 HUỶ", callback_data=f"{CB_CANCEL_PREFIX}{job_id}"),
            ]
        ]
    )


def format_duration(seconds: float) -> str:
    """`754.0` → `'12 phút 34 giây'`. Dưới một phút thì chỉ in giây."""
    total = max(0, math.ceil(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} giờ {minutes} phút"
    if minutes:
        return f"{minutes} phút {secs} giây"
    return f"{secs} giây"


def estimate_seconds(total: int) -> float:
    """Thời gian gửi ước tính, tính theo **sàn** 8 tin/giây của lane `bulk`.

    Lấy sàn chứ không lấy trần 30/giây: `bulk` chỉ được chạy hết tốc độ khi không ai bấm
    nút, nên con số theo trần là con số đẹp mà không bao giờ đạt được — và một ước tính
    lạc quan là lý do người ta bấm gửi lúc 8 giờ tối rồi ngồi nhìn nó chạy tới nửa đêm.
    """
    return total / float(BULK_FLOOR)


def _preview_body(content: str) -> str:
    if len(content) <= PREVIEW_CHARS:
        return content
    return f"{content[:PREVIEW_CHARS]}…\n(còn {len(content) - PREVIEW_CHARS} ký tự nữa)"


async def min_audience(db: AsyncSession | None = None) -> int:
    """Ngưỡng đích tối thiểu — xem ghi chú ở `MIN_AUDIENCE_KEY`.

    `db` để dùng lại session đang mở; bỏ trống thì `settings_service` tự mở session riêng.
    """
    return await settings_service.get_int(MIN_AUDIENCE_KEY, DEFAULT_MIN_AUDIENCE, db=db)


#: Một cờ là một từ bắt đầu bằng `--` đứng ngay sau khoảng trắng (hoặc đầu chuỗi). Ràng
#: buộc "đứng đầu từ" là cần thiết: một URL kiểu `https://x.test/a--b` không được biến
#: thành cờ lạ và làm cả lệnh bị từ chối.
_FLAG_RE: Final = re.compile(r"(?<!\S)--\S+")


def parse_broadcast_args(raw: str) -> BroadcastArgs:
    """Tách cờ khỏi nội dung THÔ. Cờ đứng đâu cũng được; **xuống dòng giữ nguyên**.

    Nhận cả phần sau tên lệnh chứ không nhận `context.args`: xem điểm cuối docstring
    module. Cắt cờ ra rồi chỉ dọn khoảng trắng **cuối mỗi dòng** — vừa đủ để chỗ vừa cắt
    không để lại vệt trắng, vừa không đụng tới bố cục nhiều dòng admin đã dán vào.

    Cờ lạ bị **từ chối** thay vì coi là nội dung: gõ nhầm `--al` mà bot lặng lẽ gửi chuỗi
    đó kèm tin nhắn cho tệp hẹp là hai lỗi cùng lúc — sai tệp và sai nội dung.

    Ném:
        BroadcastArgError: thiếu nội dung, gặp cờ không hiểu, hoặc nội dung quá dài.
    """
    audience = "active_30d"
    force_small = False

    pieces: list[str] = []
    cut = 0
    for matched in _FLAG_RE.finditer(raw):
        token = matched.group()
        if token == FLAG_ALL:
            audience = "all"
        elif token == FLAG_FORCE_SMALL:
            force_small = True
        else:
            raise BroadcastArgError(
                f"Cờ không hiểu: {token} (chỉ có {FLAG_ALL}, {FLAG_FORCE_SMALL})"
            )
        pieces.append(raw[cut : matched.start()])
        cut = matched.end()
    pieces.append(raw[cut:])

    content = "\n".join(line.rstrip() for line in "".join(pieces).splitlines()).strip()
    if not content:
        raise BroadcastArgError("Thiếu nội dung tin nhắn.")
    if len(content) > MAX_CONTENT_CHARS:
        raise BroadcastArgError(
            f"Nội dung dài {texts.format_vnd(len(content))} ký tự, vượt mức an toàn "
            f"{texts.format_vnd(MAX_CONTENT_CHARS)} (trần cứng của Telegram là 4.096). "
            "Telegram sẽ từ chối cả tin và đợt chạy hết mà không ai nhận được gì — "
            "hãy rút ngắn hoặc tách thành nhiều đợt."
        )
    return BroadcastArgs(content=content, audience=audience, force_small=force_small)


USAGE: Final = (
    "⚠️ Cú pháp: /broadcast <nội dung> [--all] [--force-small]\n"
    "\n"
    "Ví dụ:\n"
    "• /broadcast Kho code vừa về, vào nhận nhé!\n"
    f"• /broadcast Thông báo bảo trì --all   (bắn TOÀN BỘ, mặc định chỉ tệp "
    f"{broadcast_service.ACTIVE_WINDOW_DAYS} ngày)\n"
    "\n"
    "👉 Lệnh này KHÔNG gửi ngay: bạn sẽ thấy bản xem thử và phải bấm xác nhận."
)


# ── /broadcast ──────────────────────────────────────────────────────


def render_preview(*, job_id: int, args: BroadcastArgs, total: int) -> str:
    return (
        f"📢 XEM THỬ ĐỢT #{job_id} — CHƯA GỬI GÌ CẢ\n"
        f"{texts.SEP_WIDE}\n"
        f"{_preview_body(args.content)}\n"
        f"{texts.SEP_WIDE}\n"
        f"👥 Tệp nhận: {AUDIENCE_LABEL[args.audience]}\n"
        f"🎯 Số đích: {texts.format_vnd(total)} người\n"
        f"⏳ Ước tính gửi xong: {format_duration(estimate_seconds(total))} "
        f"(sàn {BULK_FLOOR} tin/giây)\n"
        "\n"
        "ℹ️ Đã tự loại: người chặn bot · người tắt thông báo · người chưa từng /start · "
        "người đang bị khoá.\n"
        f"⚠️ Bấm GỬI NGAY là bắt đầu gửi thật cho {texts.format_vnd(total)} người."
    )


@admin_command(CMD_BROADCAST)
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dựng đợt ở trạng thái `draft`, chọn đích, in bản xem thử. **Không gửi gì.**"""
    try:
        args = parse_broadcast_args(raw_argument(update))
    except BroadcastArgError as exc:
        await _reply(update, context, f"⚠️ {exc.hint}\n\n{USAGE}")
        return

    actor = _actor_id(update)

    async with transaction() as db:
        job_id = await broadcast_service.create_job(
            db,
            kind="broadcast",
            audience=args.audience,
            payload={"text": args.content},
            created_by=actor,
        )
        total = await broadcast_service.materialize_targets(
            db, job_id=job_id, audience=args.audience
        )
        minimum = await min_audience(db)
        too_small = total < minimum and not args.force_small
        if too_small:
            # Huỷ ngay trong cùng giao dịch: một đợt `draft` bị từ chối mà vẫn nằm đó là
            # một nút "GỬI NGAY" treo lơ lửng chờ ai đó bấm nhầm.
            await broadcast_service.cancel(db, job_id=job_id)

    if too_small:
        await _reply(
            update,
            context,
            f"🛑 TỪ CHỐI — đợt #{job_id} đã huỷ\n"
            "\n"
            f"🎯 Số đích: {texts.format_vnd(total)} người\n"
            f"📉 Ngưỡng tối thiểu ({MIN_AUDIENCE_KEY}): {texts.format_vnd(minimum)}\n"
            "\n"
            "Số đích thấp bất thường thường là bộ lọc sai, không phải tệp nhỏ.\n"
            f"👉 Vẫn muốn gửi: thêm {FLAG_FORCE_SMALL} vào cuối lệnh.",
        )
        return

    await _reply(
        update,
        context,
        render_preview(job_id=job_id, args=args, total=total),
        reply_markup=confirm_keyboard(job_id),
    )


# ── Nút xác nhận ────────────────────────────────────────────────────


async def handle_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vỏ ngoài của hai nút: **trả lời callback trước, kiểm quyền sau**.

    Thứ tự này là chủ ý. `admin_command` cố ý im lặng với người không có quyền, nhưng một
    callback không được `answer` trong 2 giây làm nút quay vòng tới khi Telegram tự huỷ —
    đúng hành vi §13.3.3 cấm. Trả lời rỗng không tiết lộ gì: người bấm được nút thì đã
    nhìn thấy nó rồi.
    """
    query = update.callback_query
    if query is None:
        return
    await _sender(context).answer_callback(query)
    await _dispatch_broadcast_callback(update, context)


@admin_command(CMD_BROADCAST)
async def _dispatch_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    matched = _CB_RE.match(query.data or "") if query is not None else None
    if matched is None:
        return

    action, raw_job_id = matched.group(1), int(matched.group(2))
    if action == "confirm":
        await _confirm(update, context, raw_job_id)
    else:
        await _cancel_job(update, context, raw_job_id, source="nút HUỶ")


async def _confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: int) -> None:
    async with transaction() as db:
        started = await broadcast_service.start(db, job_id=job_id)
        snapshot = await broadcast_service.status(db, job_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    if started is None:
        await _reply(
            update,
            context,
            f"ℹ️ Đợt #{job_id} không còn ở trạng thái chờ xác nhận "
            f"(hiện: {JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}).",
        )
        return

    # Vòng bơm sống trong tiến trình này; tiến độ thì không — nó nằm ở `broadcast_targets`,
    # nên tiến trình chết giữa chừng chỉ mất vòng lặp, không mất chỗ đang đứng.
    broadcast_service.start_pump(job_id)

    await _reply(
        update,
        context,
        f"🚀 ĐỢT #{job_id} BẮT ĐẦU CHẠY\n"
        "\n"
        f"🎯 Số đích: {texts.format_vnd(snapshot.total)} người\n"
        f"⏳ Ước tính: {format_duration(estimate_seconds(snapshot.total))}\n"
        "\n"
        f"👉 Theo dõi: {CMD_STATUS} {job_id}\n"
        f"👉 Dừng gấp: {CMD_CANCEL} {job_id}",
    )


async def _cancel_job(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job_id: int, *, source: str
) -> None:
    async with transaction() as db:
        result = await broadcast_service.cancel(db, job_id=job_id)
        snapshot = await broadcast_service.status(db, job_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    if not result.cancelled:
        await _reply(
            update,
            context,
            f"ℹ️ Đợt #{job_id} đã kết thúc từ trước "
            f"({JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}), không có gì để huỷ.",
        )
        return

    await _reply(
        update,
        context,
        f"🛑 ĐÃ HUỶ ĐỢT #{job_id} ({source})\n"
        "\n"
        f"⏭ Bỏ qua {texts.format_vnd(result.skipped_targets)} đích chưa tới lượt.\n"
        f"🗑 Rút {texts.format_vnd(result.dropped_outbox)} tin đã vào hàng đợi nhưng chưa gửi.\n"
        f"✅ Đã gửi trước khi huỷ: {texts.format_vnd(snapshot.sent)} tin — "
        "số này không thu hồi được.",
    )


# ── /broadcast_status ───────────────────────────────────────────────


def render_status(snapshot: broadcast_service.JobStatus) -> str:
    remaining = snapshot.remaining
    eta = (
        format_duration(remaining / snapshot.rate_per_second)
        if snapshot.rate_per_second > 0 and remaining
        else format_duration(estimate_seconds(remaining))
    )
    rate = f"{snapshot.rate_per_second:.1f}".replace(".", ",")

    return (
        f"📢 ĐỢT #{snapshot.job_id} — {JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}\n"
        f"{texts.SEP_WIDE}\n"
        f"👥 Tệp: {AUDIENCE_LABEL.get(snapshot.audience, snapshot.audience)}\n"
        f"🎯 Tổng đích: {texts.format_vnd(snapshot.total)}\n"
        "\n"
        f"✅ Đã gửi: {texts.format_vnd(snapshot.sent)}\n"
        f"⏳ Còn lại: {texts.format_vnd(remaining)} "
        f"(trong hàng đợi {texts.format_vnd(snapshot.waiting)} · "
        f"chưa bàn giao {texts.format_vnd(snapshot.pending)})\n"
        f"❌ Lỗi: {texts.format_vnd(snapshot.failed)}\n"
        f"🚫 Đã chặn bot: {texts.format_vnd(snapshot.blocked)}\n"
        f"⏭ Bỏ qua: {texts.format_vnd(snapshot.skipped)}\n"
        "\n"
        f"⚡ Tốc độ: {rate} tin/giây\n"
        f"🕒 Ước tính còn: {eta}"
    )


async def _resolve_job_id(context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, bool]:
    """`(job_id, gõ_tay)` — không có tham số thì lấy đợt đang chạy / tạm dừng gần nhất."""
    args = _args(context)
    if args:
        token = args[0].lstrip("#").strip()
        return (int(token) if token.isdigit() else None), True
    async with session() as db:
        return await broadcast_service.latest_job_id(db), False


async def _reply_no_job(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, *, explicit: bool
) -> None:
    """Trả lời khi không xác định được đợt nào.

    Hai câu khác nhau vì hai tình huống khác nhau: gõ sai `job_id` là lỗi cú pháp, còn
    không gõ gì mà cũng không có đợt nào đang chạy là một câu trả lời hợp lệ.
    """
    body = (
        f"⚠️ Cú pháp: {command} <job_id>"
        if explicit
        else f"ℹ️ Không có đợt nào đang chạy hoặc tạm dừng.\n\n👉 Chỉ định cụ thể: {command} <job_id>"
    )
    await _reply(update, context, body)


@admin_command(CMD_STATUS, mutates=False)
async def cmd_broadcast_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tiến độ một đợt. Không tham số thì lấy đợt đang chạy / tạm dừng gần nhất."""
    job_id, explicit = await _resolve_job_id(context)
    if job_id is None:
        await _reply_no_job(update, context, CMD_STATUS, explicit=explicit)
        return

    async with session() as db:
        snapshot = await broadcast_service.status(db, job_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    await _reply(update, context, render_status(snapshot))


# ── /broadcast_pause · /broadcast_resume · /broadcast_cancel ────────


@admin_command(CMD_PAUSE)
async def cmd_broadcast_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tạm dừng: vòng bơm dừng lấy đích mới. Tin ĐÃ vào hàng đợi vẫn được gửi nốt."""
    job_id, explicit = await _resolve_job_id(context)
    if job_id is None:
        await _reply_no_job(update, context, CMD_PAUSE, explicit=explicit)
        return

    async with transaction() as db:
        changed = await broadcast_service.pause(db, job_id=job_id)
        snapshot = await broadcast_service.status(db, job_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    if changed is None:
        await _reply(
            update,
            context,
            f"ℹ️ Đợt #{job_id} không đang chạy "
            f"(hiện: {JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}).",
        )
        return

    await _reply(
        update,
        context,
        f"⏸ ĐÃ TẠM DỪNG ĐỢT #{job_id}\n"
        "\n"
        f"⏭ Còn {texts.format_vnd(snapshot.pending)} đích chưa bàn giao — sẽ đứng yên.\n"
        f"📨 {texts.format_vnd(snapshot.waiting)} tin đã vào hàng đợi vẫn được gửi nốt "
        f"(dừng hẳn cả chúng: {CMD_CANCEL} {job_id}).\n"
        f"👉 Chạy tiếp: {CMD_RESUME} {job_id}",
    )


@admin_command(CMD_RESUME)
async def cmd_broadcast_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chạy tiếp một đợt đang tạm dừng — dựng lại vòng bơm, không bắn lại từ đầu."""
    job_id, explicit = await _resolve_job_id(context)
    if job_id is None:
        await _reply_no_job(update, context, CMD_RESUME, explicit=explicit)
        return

    async with transaction() as db:
        changed = await broadcast_service.resume(db, job_id=job_id)
        snapshot = await broadcast_service.status(db, job_id)

    if snapshot is None:
        await _reply(update, context, f"❌ Không tìm thấy đợt #{job_id}.")
        return
    if changed is None:
        await _reply(
            update,
            context,
            f"ℹ️ Đợt #{job_id} không đang tạm dừng "
            f"(hiện: {JOB_STATE_LABEL.get(snapshot.state, snapshot.state)}).",
        )
        return

    broadcast_service.start_pump(job_id)
    await _reply(
        update,
        context,
        f"▶️ ĐỢT #{job_id} CHẠY TIẾP\n"
        "\n"
        f"⏭ Còn {texts.format_vnd(snapshot.pending)} đích chưa bàn giao.\n"
        f"⏳ Ước tính: {format_duration(estimate_seconds(snapshot.remaining))}",
    )


@admin_command(CMD_CANCEL)
async def cmd_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Huỷ hẳn: bỏ qua đích còn lại **và** rút những tin chưa gửi khỏi hàng đợi."""
    job_id, explicit = await _resolve_job_id(context)
    if job_id is None:
        await _reply_no_job(update, context, CMD_CANCEL, explicit=explicit)
        return
    await _cancel_job(update, context, job_id, source=f"lệnh {CMD_CANCEL}")


# ── Đăng ký ─────────────────────────────────────────────────────────

#: Tên lệnh (không có dấu `/`) → handler. `main.build_application()` lặp qua đây.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (
    ("broadcast", cmd_broadcast),
    ("broadcast_status", cmd_broadcast_status),
    ("broadcast_pause", cmd_broadcast_pause),
    ("broadcast_resume", cmd_broadcast_resume),
    ("broadcast_cancel", cmd_broadcast_cancel),
)

#: Mẫu callback của hai nút xác nhận, dùng chung với `main.build_application()`.
CALLBACK_PATTERN: Final = r"^bc_(confirm|cancel)_\d+$"


__all__ = [
    "CALLBACK_PATTERN",
    "COMMANDS",
    "MAX_CONTENT_CHARS",
    "MIN_AUDIENCE_KEY",
    "USAGE",
    "BroadcastArgError",
    "BroadcastArgs",
    "cmd_broadcast",
    "cmd_broadcast_cancel",
    "cmd_broadcast_pause",
    "cmd_broadcast_resume",
    "cmd_broadcast_status",
    "confirm_keyboard",
    "estimate_seconds",
    "format_duration",
    "handle_broadcast_callback",
    "min_audience",
    "parse_broadcast_args",
    "render_preview",
    "render_status",
]
