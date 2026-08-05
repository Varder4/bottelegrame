"""Lệnh quản lý kho code — `13-dac-ta-bot-moi.md` §13.4.2 (mục 1, 2, 4, 7, 7b) và §13.4.3.

    /add_giffcode <loại> <mệnh_giá> <mã…>   nạp kho, bỏ qua mã trùng, gán batch_id
    /tonkho                                  tồn kho theo (loại × mệnh giá)
    /codes                                   20 mã chưa dùng gần nhất
    /codes used                              20 mã đã phát gần nhất + ai nhận, khi nào
    /del_code <mã>                           thu hồi MỘT mã chưa phát
    /del_all_code <loại> [mệnh giá]          thu hồi TOÀN BỘ mã chưa phát của một loại
    /resend_tanthu <@user|user_id>           gửi lại mã tân thủ ĐÃ cấp, không cấp mã mới

Ba chỗ ở đây cố ý làm khác hệ cũ, mỗi chỗ vá một lỗ đã tốn tiền thật:

1. **`/del_code` không `DELETE`.** Hệ cũ xoá thẳng hàng trong `codes`, kể cả mã đã trao
   cho người dùng — sổ cái thủng và không còn bằng chứng ai đã nhận gì. Ở đây mã đã phát
   bị **từ chối** kèm tên người đang giữ; mã chưa phát chuyển `status='revoked'`, hàng dữ
   liệu vẫn còn.
2. **`/resend_tanthu` đọc `code_grants`, không gọi `reserve()`.** Gửi lại là gửi **đúng
   mã cũ**; cấp mã mới cho người đã có grant chính là cách 226 tài khoản ôm 544 code thừa.
3. **`batch_id` = `code_id` của mã đầu tiên trong lô.** Đủ để `/del_code` thu hồi trọn lô
   mà không cần bảng `batches` (đặc tả không định nghĩa bảng đó), và lấy từ chính sequence
   của `codes` nên không có cuộc đua `max(batch_id) + 1` giữa hai admin nạp cùng lúc.
   `audit_log` trỏ về lô qua `entity_id`.

**Toàn bộ phản hồi là văn bản thuần, không `parse_mode`** — giống mọi chuỗi trong
`domain/texts.py`. Lý do rất cụ thể: chuỗi mã do admin dán vào, và một mã chứa `<` hay `&`
sẽ làm Telegram từ chối cả tin nhắn ở chế độ HTML. Khi đó admin nạp code xong **không thấy
phản hồi nào** và sẽ dán lại lần nữa.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.clock import VN_TZ
from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.db.models.codes import CODE_TYPES
from televip.domain import texts
from televip.services import code_issuance, settings_service, text_service
from televip.services import stock as stock_service
from televip.services.admin import Handler, admin_command, write_audit
from televip.services.stock import (
    StockRow,
    read_stock,
    summarize,
    warn_threshold,
)

#: Đọc mệnh giá giờ nằm ở `services/stock.py` để panel web dùng chung một cách đọc. Giữ
#: tên cũ ở đây vì `/del_all_code` bên dưới và các bài kiểm đang gọi qua tên này.
from televip.services.stock import doc_menh_gia as parse_value_vnd
from televip.telegram import keyboards

log = get_logger(__name__)

# ── Hằng ────────────────────────────────────────────────────────────

CMD_ADD: Final = "/add_giffcode"
CMD_TONKHO: Final = "/tonkho"
CMD_CODES: Final = "/codes"
CMD_DEL: Final = "/del_code"
CMD_RESEND: Final = "/resend_tanthu"

#: Loại phát trong SỔ CÁI cho code tân thủ. Khác khái niệm với `CODE_TYPES` của KHO —
#: xem `db/models/codes.py`. Trùng chữ ở luồng này là ngẫu nhiên, không phải cùng một thứ.
GRANT_TYPE_TANTHU: Final = "tanthu"

#: §13.4.2 mục 1 và 7b: "10 code đầu", "20 bản ghi mới nhất".
SAMPLE_LIMIT: Final = 10
LIST_LIMIT: Final = 20

# ── Đọc tham số ─────────────────────────────────────────────────────


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    """Tham số của lệnh.

    PTB tách bằng `str.split()`, nên tin nhắn **nhiều dòng** vào đây thành nhiều token —
    đúng dạng "mỗi dòng một mã" mà admin quen dán từ file.
    """
    return list(getattr(context, "args", None) or [])


def _chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return None if chat is None else chat.id


def _moment(value: datetime | None) -> str:
    """Mốc thời gian cho admin đọc, luôn quy về giờ Việt Nam.

    Không dùng lại hàm riêng tư của `domain.texts`: chuỗi ở đó là câu chữ cho người dùng
    cuối theo §13.2, còn đây là màn hình vận hành và hai thứ được phép đổi độc lập.
    """
    if value is None:
        return texts.NO_DATA
    return value.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y")


# ── /add_giffcode ───────────────────────────────────────────────────


ADD_USAGE = (
    "📥 CÁCH DÙNG\n"
    "/add_giffcode <loại> <mệnh giá> MA1 MA2 …\n\n"
    "Ví dụ: /add_giffcode tanthu 10k ABC123 DEF456\n"
    "Nhiều dòng cũng được — mỗi dòng một mã.\n"
    f"Loại hợp lệ: {', '.join(CODE_TYPES)}"
)

DEL_USAGE = "📥 Cách dùng: /del_code MA123"

RESEND_USAGE = "📥 Cách dùng: /resend_tanthu @username  hoặc  /resend_tanthu 123456789"


#: Ghi vào kho giờ nằm ở `services/stock.py` — panel web ghi qua CHÍNH hàm đó. Giữ tên cũ
#: ở đây vì các bài kiểm gọi qua tên này.
LoadResult = stock_service.KetQuaNap


def render_load_result(result: LoadResult, *, code_type: str, value_vnd: int) -> str:
    lines = [
        "✅ ĐÃ NẠP CODE",
        texts.SEP_WIDE,
        f"📂 Loại: {code_type}",
        f"💰 Mệnh giá: {texts.format_vnd(value_vnd)}đ",
        f"🆔 Lô: #{result.batch_id}" if result.batch_id is not None else "🆔 Lô: (không có mã mới)",
        texts.SEP_WIDE,
        f"✅ Đã nạp: {result.added}",
        f"⏭ Bỏ qua (trùng): {result.skipped}",
        f"💵 Giá trị lô: {texts.format_vnd(result.added * value_vnd)}đ",
        f"📦 Tồn kho {code_type} {texts.value_label(value_vnd)}: {result.stock_after}",
    ]
    if result.samples:
        lines.append("")
        lines.append(f"🔢 {len(result.samples)} mã đầu:")
        lines.extend(f"• {code}" for code in result.samples)
    return "\n".join(lines)


@admin_command(CMD_ADD)
async def handle_add_giffcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = context.application.bot_data["sender"]

    args = _args(context)
    if len(args) < 3:
        await sender.send_message(chat_id, ADD_USAGE)
        return

    # Bốn hàng rào (loại code, mệnh giá trong danh sách, mệnh giá đúng loại, ngưỡng
    # duyệt hai người) nằm trong `stock.kiem_lo_nap()`, và panel web gọi CHÍNH hàm đó.
    # Nạp kho là hành động tạo ra tiền — một hàng rào chỉ tồn tại ở một trong hai đường vào
    # thì không phải hàng rào.
    try:
        lo = await stock_service.kiem_lo_nap(
            code_type=args[0].strip().lower(),
            menh_gia_tho=args[1],
            ma_tho=" ".join(args[2:]),
        )
    except stock_service.VuotNguongDuyetHaiNguoi as exc:
        await sender.send_message(
            chat_id,
            f"⛔ Lô này trị giá {texts.format_vnd(exc.batch_value_vnd)}đ "
            f"({exc.so_ma} mã), vượt ngưỡng cần duyệt hai người là "
            f"{texts.format_vnd(exc.threshold_vnd)}đ.\n\n"
            f"Luồng ký thứ hai chưa được xây, nên tạm thời hãy chia thành nhiều lô nhỏ hơn "
            f"ngưỡng. Muốn đổi ngưỡng thì dùng `/setcauhinh {stock_service.DUAL_APPROVAL_KEY}` "
            f"— khoá đó cũng cần duyệt hai người.",
        )
        log.warning(
            "nap_code_vuot_nguong",
            actor_id=tg_user.id,
            batch_value_vnd=exc.batch_value_vnd,
            threshold_vnd=exc.threshold_vnd,
            so_ma=exc.so_ma,
        )
        return
    except stock_service.NapKhoBiTuChoi as exc:
        await sender.send_message(chat_id, f"❌ {exc}")
        return
    except ConfigError as exc:
        log.error("thieu_cau_hinh_menh_gia", detail=str(exc))
        await sender.send_message(
            chat_id,
            "❌ Thiếu cấu hình mệnh giá (code.allowed_values / code.category_values)."
            " Chưa nạp được — báo kỹ thuật.",
        )
        return

    async with transaction() as db:
        result = await stock_service.nap(db, lo, actor_id=tg_user.id)

    log.info(
        "nap_code",
        actor_id=tg_user.id,
        batch_id=result.batch_id,
        code_type=lo.code_type,
        value_vnd=lo.value_vnd,
        added=result.added,
        skipped=result.skipped,
    )
    await sender.send_message(
        chat_id, render_load_result(result, code_type=lo.code_type, value_vnd=lo.value_vnd)
    )


# ── /tonkho ─────────────────────────────────────────────────────────


def render_stock(rows: Sequence[StockRow], *, threshold: int) -> str:
    """Câu chữ cho Telegram. Cách ĐẾM nằm ở `services/stock.py` — panel web đọc chung."""
    if not rows:
        return "📦 TỒN KHO CODE\n\nKho trống — chưa nạp mã nào."

    lines = ["📦 TỒN KHO CODE", texts.SEP_WIDE]
    current_type = ""
    for row in rows:
        if row.code_type != current_type:
            current_type = row.code_type
            lines.append(f"📂 {current_type}")
        detail = (
            f"còn {row.available} · đã phát {row.issued}"
            f" · {texts.format_vnd(row.value_available_vnd)}đ"
        )
        if row.reserved:
            detail += f" · giữ chỗ {row.reserved}"
        dau = "⚠️" if row.low(threshold) else "  "
        lines.append(f"{dau} {texts.value_label(row.value_vnd)}: {detail}")

    tong = summarize(rows, threshold=threshold)
    lines.append(texts.SEP_WIDE)
    lines.append(f"Σ Còn lại: {tong.available} mã · {texts.format_vnd(tong.value_vnd)}đ")
    if tong.low_count:
        lines.append(f"⚠️ {tong.low_count} mệnh giá dưới ngưỡng {threshold} — cần nạp thêm.")
    return "\n".join(lines)


@admin_command(CMD_TONKHO, mutates=False)
async def handle_tonkho(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    sender = context.application.bot_data["sender"]

    threshold = await warn_threshold()
    async with session() as db:
        rows = await read_stock(db)

    await sender.send_message(chat_id, render_stock(rows, threshold=threshold))


# ── /codes và /codes used ───────────────────────────────────────────


_SQL_UNUSED = """
SELECT code_value, code_type, value_vnd
  FROM codes
 WHERE status = 'available'
 ORDER BY code_id DESC
 LIMIT :limit
"""

_SQL_UNUSED_TOTAL = "SELECT count(*) FROM codes WHERE status = 'available'"

#: `ORDER BY g.created_at DESC` khớp đúng index `ix_grants_recent`; đổi sang `delivered_at`
#: là mất index và thành sort toàn bảng `code_grants`.
_SQL_USED = """
SELECT c.code_value,
       c.code_type,
       g.value_vnd,
       g.state,
       g.user_id,
       u.username,
       u.full_name,
       coalesce(g.delivered_at, g.created_at) AS moc
  FROM code_grants g
  JOIN codes c ON c.code_id = g.code_id
  LEFT JOIN users u ON u.user_id = g.user_id
 ORDER BY g.created_at DESC
 LIMIT :limit
"""

_SQL_USED_TOTAL = "SELECT count(*) FROM code_grants WHERE code_id IS NOT NULL"


def render_unused(rows: Sequence[Any], total: int) -> str:
    if not rows:
        return "🗂 KHO CODE\n\nKhông còn mã nào chưa dùng."
    lines = [
        "🗂 KHO CODE — MÃ CHƯA DÙNG",
        texts.SEP_WIDE,
        f"Tổng còn lại: {total} mã · hiện {len(rows)} mã mới nhất",
        texts.SEP_WIDE,
    ]
    lines.extend(
        f"{i}. {row.code_value} — {row.code_type} · {texts.format_vnd(row.value_vnd)}đ"
        for i, row in enumerate(rows, start=1)
    )
    return "\n".join(lines)


def render_used(rows: Sequence[Any], total: int) -> str:
    if not rows:
        return "📤 CODE ĐÃ PHÁT\n\nChưa phát mã nào."
    lines = [
        "📤 CODE ĐÃ PHÁT",
        texts.SEP_WIDE,
        f"Tổng đã phát: {total} · hiện {len(rows)} bản ghi mới nhất",
        texts.SEP_WIDE,
    ]
    for i, row in enumerate(rows, start=1):
        who = texts.display_name(row.username, row.full_name)
        # `reserved` = đã hứa nhưng chưa xác nhận tới tay. Hiện ra để admin không đọc nhầm
        # một suất đang treo thành một suất đã giao.
        state = "" if row.state == "delivered" else f" [{row.state}]"
        lines.append(
            f"{i}. {row.code_value} — {row.code_type}"
            f" · {texts.format_vnd(row.value_vnd)}đ{state}\n"
            f"   → {who} ({row.user_id}) · {_moment(row.moc)}"
        )
    return "\n".join(lines)


@admin_command(CMD_CODES, mutates=False)
async def handle_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    sender = context.application.bot_data["sender"]

    args = _args(context)
    want_used = bool(args) and args[0].strip().lower() == "used"

    async with session() as db:
        if want_used:
            used_rows = (await db.execute(text(_SQL_USED), {"limit": LIST_LIMIT})).all()
            used_total = (await db.execute(text(_SQL_USED_TOTAL))).scalar_one()
            body = render_used(used_rows, used_total)
        else:
            free_rows = (await db.execute(text(_SQL_UNUSED), {"limit": LIST_LIMIT})).all()
            free_total = (await db.execute(text(_SQL_UNUSED_TOTAL))).scalar_one()
            body = render_unused(free_rows, free_total)

    await sender.send_message(chat_id, body)


# ── /del_all_code ───────────────────────────────────────────────────

CMD_DEL_ALL: Final = "/del_all_code"

CB_DEL_ALL_PREFIX: Final = "dac_"
DEL_ALL_CALLBACK_PATTERN: Final = r"^dac_(ok|no)_\d+_[A-Za-z0-9_-]{6,32}$"
_CB_DEL_ALL_RE: Final = re.compile(r"^dac_(ok|no)_(\d+)_([A-Za-z0-9_-]{6,32})$")

#: Vé đề nghị thu hồi giờ do `services/code_issuance.py` phát và tiêu — panel web dùng
#: CHUNG vé đó. Giữ tên cũ ở đây vì câu chữ bên dưới và các bài kiểm đang gọi qua tên này;
#: hai bản sao của cùng một con số là hai bản sẽ lệch nhau.
DEL_ALL_TICKET_PREFIX: Final = code_issuance.DE_NGHI_PREFIX
DEL_ALL_TICKET_TTL_SECONDS: Final = code_issuance.DE_NGHI_TTL_SECONDS

DEL_ALL_USAGE = (
    "📥 CÁCH DÙNG\n"
    "/del_all_code <loại> [mệnh giá]\n"
    "\n"
    "Ví dụ:\n"
    "• /del_all_code event          — toàn bộ mã CHƯA PHÁT loại event\n"
    "• /del_all_code event 88k      — chỉ mệnh giá 88K\n"
    "\n"
    f"Loại hợp lệ: {', '.join(CODE_TYPES)}\n"
    "\n"
    "👉 Lệnh này KHÔNG xoá ngay: bạn sẽ thấy đúng số mã và tổng giá trị, rồi phải bấm\n"
    "xác nhận. Mã ĐÃ PHÁT không bao giờ nằm trong phạm vi lệnh này."
)


def _del_all_keyboard(actor_id: int, ticket: str) -> Any:
    """Hai nút neo vào MỘT đề nghị cụ thể và MỘT người cụ thể.

    `actor_id` nằm trong `callback_data` để kiểm được **trước** khi tiêu vé: người khác
    bấm nhầm nút của đồng nghiệp thì bị từ chối mà không đốt mất đề nghị của người ta.
    """
    hau_to = f"{actor_id}_{ticket}"
    return keyboards.confirm_keyboard(
        ok_data=f"{CB_DEL_ALL_PREFIX}ok_{hau_to}",
        cancel_data=f"{CB_DEL_ALL_PREFIX}no_{hau_to}",
        ok_label="🗑 XOÁ NGAY",
        cancel_label="🛑 HUỶ",
    )


def _nhan_pham_vi(code_type: str, value_vnd: int) -> str:
    """Nhãn phạm vi cho người đọc. Web dùng nhãn riêng của nó."""
    return f"{code_type}" + (f" · {texts.value_label(value_vnd)}" if value_vnd else " · TẤT CẢ")


@admin_command(CMD_DEL_ALL)
async def handle_del_all_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Thu hồi hàng loạt mã CHƯA PHÁT của một loại (§13.4.2 mục 3).

    Như `/del_code`, đây là `status = 'revoked'` chứ không phải `DELETE`: hàng dữ liệu ở
    lại để đối soát vẫn ra số. Khác `/del_code` ở chỗ phải bấm xác nhận — một lệnh chạm
    được cả nghìn mã bằng một dòng chữ thì bước xác nhận là thứ đứng giữa nó và một lần
    gõ nhầm loại code.

    Hàng rào và vé nằm trong `code_issuance`; panel web gọi chính những hàm đó.
    """
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = context.application.bot_data["sender"]

    args = _args(context)
    if not args:
        await sender.send_message(chat_id, DEL_ALL_USAGE)
        return

    try:
        async with transaction() as db:
            pv = await code_issuance.kiem_pham_vi_thu_hoi(
                db,
                code_type_tho=args[0],
                menh_gia_tho=args[1] if len(args) > 1 else code_issuance.MENH_GIA_TAT_CA,
            )
            ticket = await code_issuance.tao_de_nghi_thu_hoi(db, actor_id=tg_user.id, pham_vi=pv)
    except code_issuance.LoaiKhongHopLe:
        await sender.send_message(
            chat_id, f"❌ Loại code không hợp lệ: {args[0]}\nHợp lệ: {', '.join(CODE_TYPES)}"
        )
        return
    except code_issuance.MenhGiaKhongDocDuoc:
        await sender.send_message(
            chat_id, f"❌ Mệnh giá không hợp lệ: {args[1]}\nVí dụ: 10k, 88k, 10000"
        )
        return
    except code_issuance.PhamViRong as exc:
        await sender.send_message(
            chat_id,
            f"ℹ️ Không có mã CHƯA PHÁT nào khớp: "
            f"{_nhan_pham_vi(exc.code_type, exc.value_vnd)}\n\nXem kho: /tonkho",
        )
        return
    except code_issuance.VuotNguongThuHoi as exc:
        # Cùng hàng rào với `/add_giffcode`: thu hồi cũng chạm vào nghĩa vụ tiền, và luồng
        # ký thứ hai chưa được xây nên ở đây fail-closed.
        await sender.send_message(
            chat_id,
            f"⛔ Phạm vi này trị giá {texts.format_vnd(exc.tong_vnd)}đ ({exc.so_ma:,} mã), "
            f"vượt ngưỡng cần duyệt hai người là {texts.format_vnd(exc.threshold_vnd)}đ.\n\n"
            f"Luồng ký thứ hai chưa được xây, nên hãy thu hẹp bằng cách lọc mệnh giá: "
            f"/del_all_code {args[0].strip().lower()} 10k",
        )
        log.warning(
            "xoa_hang_loat_vuot_nguong",
            actor_id=tg_user.id,
            code_type=args[0].strip().lower(),
            tong_vnd=exc.tong_vnd,
            threshold_vnd=exc.threshold_vnd,
        )
        return

    await sender.send_message(
        chat_id,
        (
            "⚠️ XÁC NHẬN XOÁ HÀNG LOẠT — CHƯA XOÁ GÌ CẢ\n"
            f"\n"
            f"📂 Phạm vi: {_nhan_pham_vi(pv.code_type, pv.value_vnd)}\n"
            f"🎁 Số mã CHƯA PHÁT: {pv.so_ma:,}\n"
            f"💰 Tổng giá trị: {texts.format_vnd(pv.tong_vnd)}đ\n"
            f"\n"
            "Mã đã phát cho người dùng KHÔNG nằm trong số này và không bị đụng tới.\n"
            "Mã bị thu hồi chuyển trạng thái `revoked`, hàng dữ liệu vẫn còn để đối soát.\n"
            f"\n"
            f"⏳ Đề nghị này hết hạn sau {DEL_ALL_TICKET_TTL_SECONDS // 60} phút, và chỉ bấm "
            f"được MỘT lần."
        ),
        reply_markup=_del_all_keyboard(tg_user.id, ticket),
    )
    log.info(
        "xoa_hang_loat_cho_xac_nhan",
        actor_id=tg_user.id,
        code_type=pv.code_type,
        value_vnd=pv.value_vnd,
        so_ma=pv.so_ma,
    )


async def handle_del_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vỏ ngoài của hai nút: **trả lời callback trước, kiểm quyền sau**.

    Cùng tư thế với `/broadcast` và `/send_event`, và cùng một lý do: `@admin_command`
    trả về IM LẶNG khi từ chối, nên đặt nó ở ngoài cùng khiến người không có quyền bấm
    vào một nút quay vòng tới khi Telegram tự huỷ — đúng hành vi §13.3.3 cấm. Lưới an
    toàn callback của `main.py` không cứu được: mẫu `dac_` đã khớp mất rồi.
    """
    query = update.callback_query
    if query is None:
        return
    await context.application.bot_data["sender"].answer_callback(query)
    await _del_all_dispatch(update, context)


@admin_command(CMD_DEL_ALL)
async def _del_all_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Thực thi nút xác nhận / huỷ.

    Gác bằng **cùng một quyền** với lệnh: nút này thực thi việc mà lệnh chỉ mới đề nghị,
    nên ai không gõ được lệnh cũng không được bấm nút của nó.
    """
    query = update.callback_query
    tg_user = update.effective_user
    chat_id = _chat_id(update)
    if query is None or tg_user is None or chat_id is None:
        return
    sender = context.application.bot_data["sender"]

    matched = _CB_DEL_ALL_RE.match(query.data or "")
    if matched is None:
        return

    hanh_dong, raw_actor, ticket = matched.groups()

    # Kiểm người bấm TRƯỚC khi tiêu vé: bấm nhầm nút của đồng nghiệp không được phép đốt
    # mất đề nghị của người ta.
    if int(raw_actor) != tg_user.id:
        await sender.send_message(
            chat_id,
            "⚠️ Đây là đề nghị thu hồi của admin khác — chỉ người tạo mới bấm được.\n\n"
            "Muốn thu hồi thì tự gõ /del_all_code để có đề nghị của chính bạn.",
        )
        log.warning("xoa_hang_loat_bam_nut_nguoi_khac", actor_id=tg_user.id, chu_de_nghi=raw_actor)
        return

    try:
        de_nghi = await code_issuance.nhan_de_nghi_thu_hoi(ve=ticket, actor_id=tg_user.id)
    except code_issuance.ThuHoiBiTuChoi:
        # Vé hết hạn, đã bấm, hoặc bị một đề nghị mới của chính người này ghi đè. Đây
        # chính là chỗ một cái nút cũ trong lịch sử chat trở thành vô hại.
        await sender.send_message(
            chat_id,
            "⌛ Đề nghị thu hồi này đã hết hạn hoặc đã được bấm rồi — KHÔNG mã nào bị "
            "đụng tới.\n\n"
            "Kho là thứ đang chạy, nên một đề nghị cũ không còn mô tả đúng kho hiện tại.\n"
            "👉 Gõ lại /del_all_code để xem con số mới nhất.",
        )
        return

    code_type = de_nghi.code_type
    value_vnd = de_nghi.value_vnd
    so_ma_da_hien = de_nghi.so_ma_da_hien
    pham_vi = _nhan_pham_vi(code_type, value_vnd)

    if hanh_dong == "no":
        # Huỷ cũng ĐỐT vé (đã `GETDEL` phía trên). Nói ra, nếu không admin bấm nhầm "HUỶ"
        # rồi bấm "XOÁ NGAY" ngay bên cạnh sẽ nhận câu "đề nghị đã hết hạn" và không hiểu
        # vì sao — cái nút vẫn nằm đó trông như còn dùng được.
        await sender.send_message(
            chat_id,
            f"🛑 Đã huỷ. Không mã nào bị đụng tới: {pham_vi}\n\n"
            f"Đề nghị này đã đóng — hai nút phía trên không còn tác dụng.\n"
            f"👉 Muốn thu hồi thì gõ lại /del_all_code {code_type}"
            f"{' ' + texts.value_label(value_vnd).lower() if value_vnd else ''}",
        )
        return

    # `try` NGOÀI `transaction()`: hai hàng rào dưới đây ném SAU câu `UPDATE` (chúng đo
    # trên chính tập `RETURNING`), nên bắt bên trong nghĩa là mã đã `revoked` mà màn hình
    # báo "từ chối".
    try:
        async with transaction() as db:
            kq = await code_issuance.thu_hoi_hang_loat(db, de_nghi)
    except code_issuance.VuotNguongThuHoi as exc:
        await sender.send_message(
            chat_id,
            f"⛔ TỪ CHỐI — không mã nào bị đụng tới.\n\n"
            f"Kho của phạm vi {pham_vi} hiện là {texts.format_vnd(exc.tong_vnd)}đ "
            f"({exc.so_ma:,} mã), vượt ngưỡng duyệt hai người "
            f"{texts.format_vnd(exc.threshold_vnd)}đ.\n\n"
            f"Lúc tạo đề nghị phạm vi này còn nằm trong ngưỡng ({so_ma_da_hien:,} mã) — "
            f"kho đã lớn lên trong lúc chờ.\n\n"
            f"👉 Thu hẹp bằng cách lọc mệnh giá: /del_all_code {code_type} 10k\n"
            f"👉 Đề nghị cũ đã đóng; hai nút phía trên không còn tác dụng.",
        )
        log.warning(
            "xoa_hang_loat_nut_vuot_nguong",
            actor_id=tg_user.id,
            code_type=code_type,
            value_vnd=value_vnd,
            tong_vnd=exc.tong_vnd,
            threshold_vnd=exc.threshold_vnd,
        )
        return
    except code_issuance.PhamViDaLonLen as exc:
        await sender.send_message(
            chat_id,
            f"⛔ TỪ CHỐI — không mã nào bị đụng tới.\n\n"
            f"Lúc xem thử phạm vi {pham_vi} là {exc.so_ma_da_hien:,} mã "
            f"({texts.format_vnd(exc.tong_vnd_da_hien)}đ); bây giờ là {exc.so_ma_thuc:,} mã "
            f"({texts.format_vnd(exc.tong_vnd_thuc)}đ) — kho đã LỚN LÊN trong lúc chờ.\n\n"
            f"Cái bị xoá không được rộng hơn cái bạn đã nhìn thấy.\n"
            f"👉 Gõ lại /del_all_code {code_type} để xem con số mới nhất.",
        )
        log.warning(
            "xoa_hang_loat_pham_vi_lon_len",
            actor_id=tg_user.id,
            code_type=code_type,
            so_ma_da_hien=exc.so_ma_da_hien,
            so_ma_thuc=exc.so_ma_thuc,
        )
        return

    # Kho VƠI đi giữa hai bước là chuyện bình thường (có người vừa nhận mã). Nói ra con số
    # thật thay vì im lặng — im lặng ở đây nghĩa là admin tin rằng mình vừa xoá đúng cái
    # đã thấy.
    canh_bao = (
        ""
        if not kq.lech
        else f"\n\n⚠️ Lúc xem thử là {so_ma_da_hien:,} mã — kho đã thay đổi giữa hai bước."
    )
    await sender.send_message(
        chat_id,
        (
            f"🗑 Đã thu hồi {kq.so_ma_da_xoa:,} mã chưa sử dụng: {pham_vi}\n"
            f"💰 Tổng giá trị: {texts.format_vnd(kq.tong_vnd)}đ{canh_bao}\n"
            f"\n"
            f"👉 Xem kho: /tonkho"
        ),
    )
    log.warning(
        "xoa_hang_loat_xong",
        actor_id=tg_user.id,
        code_type=code_type,
        value_vnd=value_vnd,
        so_ma=kq.so_ma_da_xoa,
        tong_vnd=kq.tong_vnd,
    )


# ── /del_code ───────────────────────────────────────────────────────


@admin_command(CMD_DEL)
async def handle_del_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Thu hồi MỘT mã chưa phát.

    Hàng rào nằm trong `code_issuance.kiem_ma_thu_hoi()` / `thu_hoi_mot()`, và panel web
    gọi chính hai hàm đó. Ở đây chỉ còn câu chữ.
    """
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = context.application.bot_data["sender"]

    args = _args(context)
    if not args:
        await sender.send_message(chat_id, DEL_USAGE)
        return

    code_value = args[0].strip()
    code_id: int | None = None

    # `try` NGOÀI `transaction()`, không phải trong. Ngoại lệ của lớp thu hồi có thể ném
    # SAU khi câu `UPDATE` đã chạy, và bắt nó bên trong nghĩa là giao dịch vẫn commit: mã
    # đã `revoked`, sổ không ghi, mà màn hình báo "từ chối".
    try:
        async with transaction() as db:
            ma = await code_issuance.kiem_ma_thu_hoi(db, code_value=code_value)
            code_id = await code_issuance.thu_hoi_mot(db, ma, actor_id=tg_user.id)
        reply = (
            f"✅ Đã xóa code: {ma.code_value}\n"
            f"📂 {ma.code_type} · {texts.format_vnd(ma.value_vnd)}đ\n"
            "🧾 Chuyển sang trạng thái revoked, hàng dữ liệu được giữ lại."
        )
    except code_issuance.MaDaPhat as exc:
        # Mã đã vào sổ cái thì thu hồi là một BÚT TOÁN NGƯỢC (`code_ledger.direction = -1`),
        # không phải một lần xoá dòng — và bút toán ngược nằm ngoài phạm vi lệnh này.
        who = texts.display_name(exc.holder_username, exc.holder_name)
        reply = (
            "❌ Code đã phát, không xoá được.\n"
            f"🎁 {exc.code_value} — {exc.code_type} · {texts.format_vnd(exc.value_vnd)}đ\n"
            f"👤 Đang giữ: {who} ({exc.holder_id})\n"
            f"🕒 {_moment(exc.moc)} · trạng thái sổ cái: {exc.grant_state}\n\n"
            "Muốn huỷ suất này thì ghi bút toán ngược, đừng xoá dòng."
        )
    except code_issuance.KhongTimThayMa as exc:
        reply = f"❌ Không tìm thấy code: {exc.code_value}"
    except code_issuance.MaDaThuHoi as exc:
        reply = f"⚠️ Code {exc.code_value} đã bị thu hồi trước đó."
    except code_issuance.MatCuocDua as exc:
        reply = f"❌ Code {exc.code_value} vừa được một luồng khác giữ chỗ. Không thu hồi được."
    except code_issuance.ThuHoiBiTuChoi as exc:
        reply = f"❌ {exc}"

    if code_id is not None:
        log.info("thu_hoi_code", actor_id=tg_user.id, code_id=code_id, code_value=code_value)
    await sender.send_message(chat_id, reply)


# ── /resend_tanthu ──────────────────────────────────────────────────


_SQL_FIND_USER_BY_NAME = "SELECT user_id FROM users WHERE lower(username) = lower(:name)"
_SQL_FIND_USER_BY_ID = "SELECT user_id FROM users WHERE user_id = :uid"

_SQL_TANTHU_GRANT = """
SELECT g.grant_id,
       g.state,
       g.value_vnd,
       c.code_value
  FROM code_grants g
  LEFT JOIN codes c ON c.code_id = g.code_id
 WHERE g.user_id = :uid
   AND g.grant_type = :gt
 ORDER BY g.grant_id DESC
 LIMIT 1
"""


async def resolve_user(db: AsyncSession, raw: str) -> int | None:
    """`@username` hoặc `user_id` → `user_id`. `None` nếu không có ai khớp.

    Tra username qua `lower(...)` để trúng index `uq_users_username_lower`; Telegram không
    phân biệt hoa thường ở username nên admin gõ kiểu nào cũng phải ra đúng người.
    """
    token = raw.strip()
    if token.startswith("@"):
        sql, params = _SQL_FIND_USER_BY_NAME, {"name": token[1:]}
    elif token.lstrip("-").isdecimal():
        sql, params = _SQL_FIND_USER_BY_ID, {"uid": int(token)}
    else:
        # Không có `@` mà cũng không phải số: coi là username, admin hay quên dấu `@`.
        sql, params = _SQL_FIND_USER_BY_NAME, {"name": token}
    row = (await db.execute(text(sql), params)).one_or_none()
    return None if row is None else row.user_id


@admin_command(CMD_RESEND)
async def handle_resend_tanthu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = context.application.bot_data["sender"]

    args = _args(context)
    if not args:
        await sender.send_message(chat_id, RESEND_USAGE)
        return

    target_raw = args[0]
    async with session() as db:
        target_id = await resolve_user(db, target_raw)
        if target_id is None:
            await sender.send_message(chat_id, f"❌ Không tìm thấy người dùng: {target_raw}")
            return
        grant = (
            await db.execute(text(_SQL_TANTHU_GRANT), {"uid": target_id, "gt": GRANT_TYPE_TANTHU})
        ).one_or_none()

    if grant is None:
        # KHÔNG cấp mã mới ở đây. Lệnh này là "gửi lại thứ đã cấp"; muốn cấp mới thì người
        # đó đi qua đúng luồng tân thủ, nơi `uq_grant_once_semantic` canh cửa.
        await sender.send_message(
            chat_id,
            f"❌ {target_id} chưa từng được cấp code tân thủ."
            " Lệnh này chỉ gửi lại mã đã cấp, không cấp mã mới.",
        )
        return

    if grant.state == "revoked":
        await sender.send_message(chat_id, f"⚠️ Suất tân thủ của {target_id} đã bị thu hồi.")
        return

    if grant.code_value is None:
        await sender.send_message(
            chat_id,
            f"⚠️ {target_id} có suất tân thủ nhưng CHƯA gắn được mã (lúc đó kho rỗng)."
            " Nạp code rồi bảo họ bấm lại nút nhận code.",
        )
        return

    game_link = await settings_service.get_str("link.game_bot", "")
    sent = await sender.send_message(
        target_id,
        await text_service.render(
            "code.delivered", code_value=grant.code_value, value_vnd=grant.value_vnd
        ),
        reply_markup=keyboards.enter_code_keyboard(game_link) if game_link else None,
    )
    if sent is None:
        await sender.send_message(
            chat_id,
            f"❌ Không gửi được cho {target_id} — người này đã chặn bot hoặc chưa từng"
            " mở chat riêng với bot.",
        )
        return

    async with transaction() as db:
        # Lần cấp trước gửi hỏng nên grant còn nằm ở `reserved`; giờ Telegram đã xác nhận,
        # đây đúng là pha 2. Grant đã `delivered` thì hàm này không đụng dòng nào.
        await code_issuance.mark_delivered(db, grant_id=grant.grant_id)
        await write_audit(
            db,
            actor_id=tg_user.id,
            action="resend_tanthu",
            entity_type="code_grant",
            entity_id=str(grant.grant_id),
            after={"user_id": target_id, "code_value": grant.code_value},
        )

    log.info("gui_lai_code_tanthu", actor_id=tg_user.id, user_id=target_id, grant_id=grant.grant_id)
    await sender.send_message(
        chat_id,
        f"✅ Đã gửi lại mã tân thủ cho {target_id}\n"
        f"🎁 {grant.code_value} · {texts.format_vnd(grant.value_vnd)}đ",
    )


# ── Đăng ký ─────────────────────────────────────────────────────────

#: Tên lệnh (không có dấu `/`) → handler. `main.build_application()` chỉ cần lặp qua đây;
#: khối này KHÔNG tự sửa `main.py` để không giẫm lên khối khác.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (
    ("add_giffcode", handle_add_giffcode),
    ("tonkho", handle_tonkho),
    ("codes", handle_codes),
    ("del_code", handle_del_code),
    ("del_all_code", handle_del_all_code),
    ("resend_tanthu", handle_resend_tanthu),
)


__all__ = [
    "COMMANDS",
    "LoadResult",
    "StockRow",
    "handle_add_giffcode",
    "handle_codes",
    "handle_del_code",
    "handle_resend_tanthu",
    "handle_tonkho",
    "LoadResult",
    "parse_value_vnd",
    "read_stock",
    "render_load_result",
    "render_stock",
    "render_unused",
    "render_used",
    "resolve_user",
    "warn_threshold",
]
