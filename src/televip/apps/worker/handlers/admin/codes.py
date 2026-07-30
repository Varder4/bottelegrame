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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.clock import VN_TZ
from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.db.models.codes import CODE_TYPES, Code
from televip.domain import texts
from televip.services import code_issuance, settings_service, text_service
from televip.services.admin import Handler, admin_command, write_audit
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

#: Ngưỡng cảnh báo tồn kho thấp.
#:
#: ⚠️ Có HAI khoá cùng nghĩa: yêu cầu của khối này đặt tên `stock.warn_threshold`, còn
#: §13.6.7 đã khai `alert.low_code_threshold` = 50 và migration 0002 **đã seed nó**. Chỉ
#: khoá đã seed mới sửa được bằng `/setcauhinh` (`settings_service.set()` từ chối khoá
#: không tồn tại). Nên thứ tự đọc là: khoá mới → khoá đã seed → 50, để cái nút vặn hiện
#: có vẫn thật sự vặn được. Gộp hai khoá làm một là việc của người chốt đặc tả.
STOCK_WARN_KEY: Final = "stock.warn_threshold"
LOW_CODE_KEY: Final = "alert.low_code_threshold"
DEFAULT_WARN_THRESHOLD: Final = 50

_VALUE_RE: Final = re.compile(r"^(\d+)([kK])?$")


# ── Đọc tham số ─────────────────────────────────────────────────────


def parse_value_vnd(raw: str) -> int | None:
    """`10000` · `10.000` · `10k` · `88K` → số VNĐ. `None` nếu không đọc được.

    Bốn dạng vì §13.4.2 viết `{value_k}k` trong mẫu lệnh còn §13.6.2 viết `10.000` trong
    bảng mệnh giá — admin sẽ gõ cả hai, và một lần gõ đúng-theo-tài-liệu mà bot từ chối là
    lý do người ta quay lại sửa thẳng database.
    """
    cleaned = raw.replace(".", "").replace(",", "").replace("_", "")
    matched = _VALUE_RE.match(cleaned)
    if matched is None:
        return None
    value = int(matched.group(1))
    if matched.group(2):
        value *= 1_000
    return value or None


def dedupe(raw_codes: Sequence[str]) -> list[str]:
    """Bỏ khoảng trắng, bỏ token rỗng, bỏ trùng **trong chính lô** — giữ thứ tự gõ vào.

    So khớp phân biệt hoa thường vì `uq_codes_value` cũng vậy: gộp `abc` với `ABC` ở đây
    sẽ báo "đã bỏ qua vì trùng" cho một mã mà database vẫn nhận.
    """
    seen: set[str] = set()
    result: list[str] = []
    for token in raw_codes:
        code = token.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


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


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Kết quả một lần nạp kho.

    ``skipped`` gộp cả hai loại trùng — trùng với kho và trùng ngay trong lô vừa dán — vì
    admin chỉ cần biết "gõ N mã, vào kho M".
    """

    #: `None` khi không mã nào vào kho (cả lô đều trùng) — không có lô nào để thu hồi.
    batch_id: int | None
    added: int
    skipped: int
    samples: list[str]
    stock_after: int


async def load_codes(
    db: AsyncSession,
    *,
    code_type: str,
    value_vnd: int,
    code_values: Sequence[str],
    actor_id: int,
    extra_skipped: int = 0,
) -> LoadResult:
    """Nạp một lô vào kho và ghi `audit_log`. Gọi trong `transaction()`.

    `ON CONFLICT (code_value) DO NOTHING` là toàn bộ cơ chế chống trùng: hỏi trước rồi
    chèn sau để lại một khe giữa hai câu lệnh, và hai admin dán cùng một danh sách trong
    khe đó sẽ cùng thấy "chưa có".

    ``extra_skipped`` là số mã đã bị loại vì trùng ngay trong lô, đếm trước khi gọi hàm.
    """
    stmt = (
        pg_insert(Code)
        .values(
            [
                {
                    "code_value": value,
                    "code_type": code_type,
                    "value_vnd": value_vnd,
                    "created_by": actor_id,
                }
                for value in code_values
            ]
        )
        .on_conflict_do_nothing(index_elements=["code_value"])
        .returning(Code.code_id, Code.code_value)
    )
    inserted = (await db.execute(stmt)).all()

    added = len(inserted)
    skipped = len(code_values) - added + extra_skipped

    # `batch_id` = code_id nhỏ nhất của lô — xem docstring module. Gán ở câu thứ hai vì
    # giá trị đó chỉ biết được SAU khi `ON CONFLICT` đã lọc xong mã trùng.
    batch_id = min(row.code_id for row in inserted) if inserted else None
    if batch_id is not None:
        await db.execute(
            update(Code)
            .where(Code.code_id.in_([row.code_id for row in inserted]))
            .values(batch_id=batch_id)
        )

    stock_after = await code_issuance.available_count(db, code_type=code_type, value_vnd=value_vnd)

    await write_audit(
        db,
        actor_id=actor_id,
        action="add_giffcode",
        entity_type="code_batch",
        entity_id=None if batch_id is None else str(batch_id),
        after={
            "code_type": code_type,
            "value_vnd": value_vnd,
            "requested": len(code_values) + extra_skipped,
            "added": added,
            "skipped": skipped,
            "total_value_vnd": added * value_vnd,
        },
    )

    return LoadResult(
        batch_id=batch_id,
        added=added,
        skipped=skipped,
        samples=[row.code_value for row in inserted][:SAMPLE_LIMIT],
        stock_after=stock_after,
    )


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

    code_type = args[0].strip().lower()
    if code_type not in CODE_TYPES:
        await sender.send_message(
            chat_id,
            f"❌ Loại code không hợp lệ: {args[0]}\nHợp lệ: {', '.join(CODE_TYPES)}",
        )
        return

    value_vnd = parse_value_vnd(args[1])
    if value_vnd is None:
        await sender.send_message(
            chat_id,
            f"❌ Mệnh giá không đọc được: {args[1]}\nDùng dạng 10k hoặc 10000.",
        )
        return

    # Hai khoá này do migration seed và KHÔNG có default viết trong code (nguyên tắc N2):
    # một giá trị mặc định ở đây sẽ âm thầm thay thế luật thật khi seed hỏng, và chỗ nó
    # thay thế là câu trả lời "mệnh giá nào được nạp" — tức là tiền.
    try:
        allowed_values = await settings_service.get_list("code.allowed_values")
        category_values = await settings_service.get_dict("code.category_values")
    except ConfigError as exc:
        log.error("thieu_cau_hinh_menh_gia", detail=str(exc))
        await sender.send_message(
            chat_id,
            "❌ Thiếu cấu hình mệnh giá (code.allowed_values / code.category_values)."
            " Chưa nạp được — báo kỹ thuật.",
        )
        return

    if value_vnd not in allowed_values:
        await sender.send_message(
            chat_id,
            f"❌ Mệnh giá {texts.format_vnd(value_vnd)}đ không nằm trong danh sách cho phép.\n"
            f"Hợp lệ: {', '.join(texts.value_label(v) for v in allowed_values)}",
        )
        return

    per_type = category_values.get(code_type)
    if per_type is not None and value_vnd not in per_type:
        await sender.send_message(
            chat_id,
            f"❌ Loại {code_type} không dùng mệnh giá {texts.format_vnd(value_vnd)}đ.\n"
            f"Hợp lệ cho loại này: {', '.join(texts.value_label(v) for v in per_type)}",
        )
        return

    raw_codes = args[2:]
    code_values = dedupe(raw_codes)
    if not code_values:
        await sender.send_message(chat_id, "❌ Không có mã nào để nạp.")
        return

    # ── Ngưỡng duyệt hai người (§13.4.1) ────────────────────────────
    # Nạp kho là hành động tạo ra tiền: 1.000 mã mệnh giá 500.000đ là nửa tỉ đồng nghĩa vụ
    # sinh ra bằng một tin nhắn. Ngưỡng này tồn tại trong `settings` ngay từ migration đầu
    # nhưng trước đó không có dòng code nào đọc nó — tức là luật hai chữ ký chưa từng chạy.
    # Chưa có luồng ký thứ hai nên ở đây fail-closed: từ chối và nói rõ phải chia nhỏ lô.
    batch_value = len(code_values) * value_vnd
    threshold = await settings_service.get_int("admin.dual_approval_threshold_vnd", 1_000_000)
    if batch_value > threshold:
        await sender.send_message(
            chat_id,
            f"⛔ Lô này trị giá {texts.format_vnd(batch_value)}đ "
            f"({len(code_values)} mã × {texts.format_vnd(value_vnd)}đ), "
            f"vượt ngưỡng cần duyệt hai người là {texts.format_vnd(threshold)}đ.\n\n"
            f"Luồng ký thứ hai chưa được xây, nên tạm thời hãy chia thành nhiều lô nhỏ hơn "
            f"ngưỡng. Muốn đổi ngưỡng thì dùng `/setcauhinh admin.dual_approval_threshold_vnd` "
            f"— khoá đó cũng cần duyệt hai người.",
        )
        log.warning(
            "nap_code_vuot_nguong",
            actor_id=tg_user.id,
            batch_value_vnd=batch_value,
            threshold_vnd=threshold,
            so_ma=len(code_values),
        )
        return

    async with transaction() as db:
        result = await load_codes(
            db,
            code_type=code_type,
            value_vnd=value_vnd,
            code_values=code_values,
            actor_id=tg_user.id,
            extra_skipped=len(raw_codes) - len(code_values),
        )

    log.info(
        "nap_code",
        actor_id=tg_user.id,
        batch_id=result.batch_id,
        code_type=code_type,
        value_vnd=value_vnd,
        added=result.added,
        skipped=result.skipped,
    )
    await sender.send_message(
        chat_id, render_load_result(result, code_type=code_type, value_vnd=value_vnd)
    )


# ── /tonkho ─────────────────────────────────────────────────────────


#: Đếm thẳng trên `codes` chứ KHÔNG đọc `code_pool_stats`: bảng đếm sẵn đó chưa có trigger
#: nào nuôi (`\d codes` cho 0 trigger), nên đọc nó là báo cáo toàn số 0 cho admin. Khi
#: trigger về, đổi đúng câu này thành `SELECT … FROM code_pool_stats`.
_SQL_STOCK = """
SELECT code_type,
       value_vnd,
       count(*) FILTER (WHERE status = 'available') AS con_lai,
       count(*) FILTER (WHERE status = 'issued')    AS da_phat,
       count(*) FILTER (WHERE status = 'reserved')  AS giu_cho
  FROM codes
 GROUP BY code_type, value_vnd
 ORDER BY code_type, value_vnd
"""


@dataclass(frozen=True, slots=True)
class StockRow:
    code_type: str
    value_vnd: int
    available: int
    issued: int
    reserved: int


async def read_stock(db: AsyncSession) -> list[StockRow]:
    rows = (await db.execute(text(_SQL_STOCK))).all()
    return [
        StockRow(
            code_type=row.code_type,
            value_vnd=row.value_vnd,
            available=row.con_lai,
            issued=row.da_phat,
            reserved=row.giu_cho,
        )
        for row in rows
    ]


async def warn_threshold() -> int:
    """Ngưỡng cảnh báo tồn kho thấp — xem ghi chú ở `STOCK_WARN_KEY`."""
    seeded = await settings_service.get_int(LOW_CODE_KEY, DEFAULT_WARN_THRESHOLD)
    return await settings_service.get_int(STOCK_WARN_KEY, seeded)


def render_stock(rows: Sequence[StockRow], *, threshold: int) -> str:
    if not rows:
        return "📦 TỒN KHO CODE\n\nKho trống — chưa nạp mã nào."

    lines = ["📦 TỒN KHO CODE", texts.SEP_WIDE]
    current_type = ""
    low_count = 0
    for row in rows:
        if row.code_type != current_type:
            current_type = row.code_type
            lines.append(f"📂 {current_type}")
        low = row.available < threshold
        if low:
            low_count += 1
        detail = (
            f"còn {row.available} · đã phát {row.issued}"
            f" · {texts.format_vnd(row.available * row.value_vnd)}đ"
        )
        if row.reserved:
            detail += f" · giữ chỗ {row.reserved}"
        lines.append(f"{'⚠️' if low else '  '} {texts.value_label(row.value_vnd)}: {detail}")

    total_available = sum(row.available for row in rows)
    total_value = sum(row.available * row.value_vnd for row in rows)
    lines.append(texts.SEP_WIDE)
    lines.append(f"Σ Còn lại: {total_available} mã · {texts.format_vnd(total_value)}đ")
    if low_count:
        lines.append(f"⚠️ {low_count} mệnh giá dưới ngưỡng {threshold} — cần nạp thêm.")
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
DEL_ALL_CALLBACK_PATTERN: Final = r"^dac_(ok|no)_[a-z]+_\d+_\d+$"
_CB_DEL_ALL_RE: Final = re.compile(r"^dac_(ok|no)_([a-z]+)_(\d+)_(\d+)$")

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

_SQL_COUNT_AVAILABLE = """
SELECT count(*)::int                        AS so_ma,
       coalesce(sum(value_vnd), 0)::bigint  AS tong_vnd
  FROM codes
 WHERE code_type = :code_type
   AND status = 'available'
   AND (:value_vnd = 0 OR value_vnd = :value_vnd)
"""

#: Thu hồi hàng loạt. `status = 'available'` trong `WHERE` là hàng rào thật, không phải
#: bộ lọc cho đẹp: mã đã giữ chỗ hoặc đã phát nằm ngoài phạm vi câu này, nên không có
#: đường nào để một lần gõ nhầm chạm vào mã đang thuộc về một người thật.
_SQL_REVOKE_BULK = """
UPDATE codes
   SET status = 'revoked'
 WHERE code_type = :code_type
   AND status = 'available'
   AND (:value_vnd = 0 OR value_vnd = :value_vnd)
RETURNING code_id, value_vnd
"""


def _del_all_keyboard(code_type: str, value_vnd: int, so_ma: int) -> Any:
    """Hai nút, mang theo ĐÚNG phạm vi vừa đếm.

    `so_ma` nằm trong `callback_data` để lúc bấm còn so lại được: kho là thứ đang chạy,
    một lượt nạp hoặc một lượt phát xen vào giữa hai bước làm phạm vi khác đi, và xoá một
    phạm vi khác với phạm vi đã hiện ra là đúng nghĩa xoá nhầm.
    """
    hau_to = f"{code_type}_{value_vnd}_{so_ma}"
    return keyboards.confirm_keyboard(
        ok_data=f"{CB_DEL_ALL_PREFIX}ok_{hau_to}",
        cancel_data=f"{CB_DEL_ALL_PREFIX}no_{hau_to}",
        ok_label="🗑 XOÁ NGAY",
        cancel_label="🛑 HUỶ",
    )


@admin_command(CMD_DEL_ALL)
async def handle_del_all_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Thu hồi hàng loạt mã CHƯA PHÁT của một loại (§13.4.2 mục 3).

    Như `/del_code`, đây là `status = 'revoked'` chứ không phải `DELETE`: hàng dữ liệu ở
    lại để đối soát vẫn ra số. Khác `/del_code` ở chỗ phải bấm xác nhận — một lệnh chạm
    được cả nghìn mã bằng một dòng chữ thì bước xác nhận là thứ đứng giữa nó và một lần
    gõ nhầm loại code.
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

    code_type = args[0].strip().lower()
    if code_type not in CODE_TYPES:
        await sender.send_message(
            chat_id,
            f"❌ Loại code không hợp lệ: {args[0]}\nHợp lệ: {', '.join(CODE_TYPES)}",
        )
        return

    # `0` = không lọc mệnh giá. Dùng số thay vì `None` để cùng một tham số đi qua được cả
    # SQL lẫn `callback_data` mà không cần hai đường mã hoá.
    value_vnd = 0
    if len(args) > 1:
        parsed = parse_value_vnd(args[1])
        if parsed is None:
            await sender.send_message(
                chat_id, f"❌ Mệnh giá không hợp lệ: {args[1]}\nVí dụ: 10k, 88k, 10000"
            )
            return
        value_vnd = parsed

    async with session() as db:
        row = (
            await db.execute(
                text(_SQL_COUNT_AVAILABLE), {"code_type": code_type, "value_vnd": value_vnd}
            )
        ).one()

    pham_vi = f"{code_type}" + (f" · {texts.value_label(value_vnd)}" if value_vnd else " · TẤT CẢ")
    if row.so_ma == 0:
        await sender.send_message(
            chat_id, f"ℹ️ Không có mã CHƯA PHÁT nào khớp: {pham_vi}\n\nXem kho: /tonkho"
        )
        return

    # Cùng hàng rào với `/add_giffcode`: thu hồi cũng là một hành động chạm vào nghĩa vụ
    # tiền, và luồng ký thứ hai chưa được xây nên ở đây fail-closed.
    threshold = await settings_service.get_int("admin.dual_approval_threshold_vnd", 1_000_000)
    if row.tong_vnd > threshold:
        await sender.send_message(
            chat_id,
            f"⛔ Phạm vi này trị giá {texts.format_vnd(row.tong_vnd)}đ ({row.so_ma:,} mã), "
            f"vượt ngưỡng cần duyệt hai người là {texts.format_vnd(threshold)}đ.\n\n"
            f"Luồng ký thứ hai chưa được xây, nên hãy thu hẹp bằng cách lọc mệnh giá: "
            f"/del_all_code {code_type} 10k",
        )
        log.warning(
            "xoa_hang_loat_vuot_nguong",
            actor_id=tg_user.id,
            code_type=code_type,
            value_vnd=value_vnd,
            tong_vnd=row.tong_vnd,
            threshold_vnd=threshold,
        )
        return

    await sender.send_message(
        chat_id,
        (
            "⚠️ XÁC NHẬN XOÁ HÀNG LOẠT — CHƯA XOÁ GÌ CẢ\n"
            f"\n"
            f"📂 Phạm vi: {pham_vi}\n"
            f"🎁 Số mã CHƯA PHÁT: {row.so_ma:,}\n"
            f"💰 Tổng giá trị: {texts.format_vnd(row.tong_vnd)}đ\n"
            f"\n"
            "Mã đã phát cho người dùng KHÔNG nằm trong số này và không bị đụng tới.\n"
            "Mã bị thu hồi chuyển trạng thái `revoked`, hàng dữ liệu vẫn còn để đối soát."
        ),
        reply_markup=_del_all_keyboard(code_type, value_vnd, row.so_ma),
    )
    log.info(
        "xoa_hang_loat_cho_xac_nhan",
        actor_id=tg_user.id,
        code_type=code_type,
        value_vnd=value_vnd,
        so_ma=row.so_ma,
    )


@admin_command(CMD_DEL_ALL)
async def handle_del_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nút xác nhận / huỷ của `/del_all_code`.

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
        await sender.answer_callback(query)
        return

    hanh_dong, code_type, raw_value, raw_count = matched.groups()
    value_vnd, so_ma_da_hien = int(raw_value), int(raw_count)
    pham_vi = f"{code_type}" + (f" · {texts.value_label(value_vnd)}" if value_vnd else " · TẤT CẢ")

    if hanh_dong == "no":
        await sender.answer_callback(query, "Đã huỷ")
        await sender.send_message(chat_id, f"🛑 Đã huỷ. Không mã nào bị đụng tới: {pham_vi}")
        return

    await sender.answer_callback(query, "Đang xoá...")

    async with transaction() as db:
        rows = (
            await db.execute(
                text(_SQL_REVOKE_BULK), {"code_type": code_type, "value_vnd": value_vnd}
            )
        ).all()
        tong_vnd = sum(r.value_vnd for r in rows)
        if rows:
            await write_audit(
                db,
                actor_id=tg_user.id,
                action="del_all_code",
                entity_type="codes",
                entity_id=f"{code_type}:{value_vnd or 'all'}",
                before={"so_ma_da_hien": so_ma_da_hien},
                after={"so_ma_da_xoa": len(rows), "tong_vnd": tong_vnd},
            )

    # Kho đổi giữa hai bước là chuyện bình thường (có người vừa nhận mã, hoặc admin khác
    # vừa nạp). Nói ra con số thật thay vì im lặng — im lặng ở đây nghĩa là admin tin rằng
    # mình vừa xoá đúng cái đã thấy.
    canh_bao = (
        ""
        if len(rows) == so_ma_da_hien
        else f"\n\n⚠️ Lúc xem thử là {so_ma_da_hien:,} mã — kho đã thay đổi giữa hai bước."
    )
    await sender.send_message(
        chat_id,
        (
            f"🗑 Đã thu hồi {len(rows):,} mã chưa sử dụng: {pham_vi}\n"
            f"💰 Tổng giá trị: {texts.format_vnd(tong_vnd)}đ{canh_bao}\n"
            f"\n"
            f"👉 Xem kho: /tonkho"
        ),
    )
    log.warning(
        "xoa_hang_loat_xong",
        actor_id=tg_user.id,
        code_type=code_type,
        value_vnd=value_vnd,
        so_ma=len(rows),
        tong_vnd=tong_vnd,
    )


# ── /del_code ───────────────────────────────────────────────────────


_SQL_FIND_CODE = """
SELECT c.code_id,
       c.status,
       c.code_type,
       c.value_vnd,
       g.grant_id,
       g.state     AS grant_state,
       g.user_id   AS holder_id,
       u.username  AS holder_username,
       u.full_name AS holder_name,
       coalesce(g.delivered_at, g.created_at) AS moc
  FROM codes c
  LEFT JOIN code_grants g ON g.code_id = c.code_id
  LEFT JOIN users u ON u.user_id = g.user_id
 WHERE c.code_value = :code
"""

_SQL_REVOKE = """
UPDATE codes
   SET status = 'revoked'
 WHERE code_id = :cid
   AND status = 'available'
RETURNING code_id
"""


@admin_command(CMD_DEL)
async def handle_del_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    # Dựng câu trả lời BÊN TRONG giao dịch rồi gửi BÊN NGOÀI: một lời gọi Telegram giữa
    # `BEGIN` và `COMMIT` là giữ một kết nối trong pool suốt độ trễ mạng, và pool chỉ có
    # `db_pool_size` chỗ.
    async with transaction() as db:
        reply, revoked_code_id = await _revoke_code(db, code_value=code_value, actor_id=tg_user.id)

    if revoked_code_id is not None:
        log.info(
            "thu_hoi_code", actor_id=tg_user.id, code_id=revoked_code_id, code_value=code_value
        )
    await sender.send_message(chat_id, reply)


async def _revoke_code(
    db: AsyncSession, *, code_value: str, actor_id: int
) -> tuple[str, int | None]:
    """Thu hồi một mã chưa phát. Trả về (câu trả lời cho admin, code_id đã thu hồi)."""
    row = (await db.execute(text(_SQL_FIND_CODE), {"code": code_value})).one_or_none()

    if row is None:
        return f"❌ Không tìm thấy code: {code_value}", None

    if row.grant_id is not None:
        # Hàng rào chính của lệnh này. Mã đã vào sổ cái thì thu hồi là một BÚT TOÁN NGƯỢC
        # (`code_ledger.direction = -1`), không phải một lần xoá dòng — và bút toán ngược
        # nằm ngoài phạm vi lệnh này.
        who = texts.display_name(row.holder_username, row.holder_name)
        return (
            "❌ Code đã phát, không xoá được.\n"
            f"🎁 {code_value} — {row.code_type} · {texts.format_vnd(row.value_vnd)}đ\n"
            f"👤 Đang giữ: {who} ({row.holder_id})\n"
            f"🕒 {_moment(row.moc)} · trạng thái sổ cái: {row.grant_state}\n\n"
            "Muốn huỷ suất này thì ghi bút toán ngược, đừng xoá dòng."
        ), None

    if row.status == "revoked":
        return f"⚠️ Code {code_value} đã bị thu hồi trước đó.", None

    revoked = (await db.execute(text(_SQL_REVOKE), {"cid": row.code_id})).scalar_one_or_none()
    if revoked is None:
        # `status` không còn `available` giữa lượt đọc và lượt ghi — một luồng khác vừa giữ
        # chỗ mã này. Điều kiện trong câu UPDATE là thứ chặn, không phải câu `if` phía trên.
        return f"❌ Code {code_value} vừa được một luồng khác giữ chỗ. Không thu hồi được.", None

    await write_audit(
        db,
        actor_id=actor_id,
        action="del_code",
        entity_type="code",
        entity_id=str(row.code_id),
        before={"code_value": code_value, "status": row.status},
        after={"code_value": code_value, "status": "revoked"},
    )

    return (
        f"✅ Đã xóa code: {code_value}\n"
        f"📂 {row.code_type} · {texts.format_vnd(row.value_vnd)}đ\n"
        "🧾 Chuyển sang trạng thái revoked, hàng dữ liệu được giữ lại."
    ), row.code_id


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
    elif token.lstrip("-").isdigit():
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
    "dedupe",
    "handle_add_giffcode",
    "handle_codes",
    "handle_del_code",
    "handle_resend_tanthu",
    "handle_tonkho",
    "load_codes",
    "parse_value_vnd",
    "read_stock",
    "render_load_result",
    "render_stock",
    "render_unused",
    "render_used",
    "resolve_user",
    "warn_threshold",
]
