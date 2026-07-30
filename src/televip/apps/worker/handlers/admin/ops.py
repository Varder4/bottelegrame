"""Lệnh vận hành và cấu hình nóng — `13-dac-ta-bot-moi.md` §13.4.2 (5, 14) và §13.4.3.

    /cauhinh [tiền_tố]        xem tham số đang chạy
    /setcauhinh <khoá> <giá>  đổi tham số nóng, không restart
    /stats                    tổng quan hệ thống
    /user <@u|id>             hồ sơ một người
    /ban <id> [lý do]         ghi user_bans
    /unban <id>               gỡ ban
    /admin_add <id> <vai trò> quản lý danh bạ admin
    /admin_del <id>
    /users [n]                người dùng mới nhất (mặc định 30)
    /huongdan                 hướng dẫn vận hành, ba tin
    /help_admin               lệnh mà CHÍNH người gọi được phép chạy

Bốn điều chi phối cả file:

1. **`/setcauhinh` là lệnh làm cho nguyên tắc N2 có thật** (§13.4.3). Hệ cũ đổi một mệnh
   giá bằng `nano config.py` trên VPS rồi restart, và không ai trả lời được câu "ai đổi tỉ
   lệ event, lúc nào, từ giá trị nào". Ở đây mỗi lần đổi để lại `settings_audit` +
   `audit_log`, và câu trả lời cho admin luôn in **giá trị cũ → giá trị mới**.
2. **Không lệnh nào tự dựng câu trả lời từ một hằng số nghiệp vụ.** Con số đến từ
   database; câu chữ định dạng tiền đi qua `domain/texts.format_vnd`.
3. **`/help_admin` sinh từ `admin_permissions`**, không phải một danh sách cứng. Danh sách
   cứng sẽ trôi khỏi bảng ngay lần đầu ai đó đổi quyền bằng lệnh — và bản in ra là bản
   sai đúng vào lúc người ta cần nó nhất.
4. **Mọi lệnh gửi qua `Sender`.** Không một lời gọi Bot API nào trong file này.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers.admin.guard import admin_command
from televip.core.clock import VN_TZ
from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import admin as admin_service
from televip.services import settings_service, text_service

log = get_logger(__name__)

#: Trần an toàn cho một tin nhắn Telegram là 4096 ký tự. Cắt ở 3.500 để còn chỗ cho phần
#: đầu/cuối mà không phải đếm từng ký tự ở mỗi chỗ dựng chuỗi.
MAX_MESSAGE_CHARS: Final = 3_500

#: Số grant hiển thị trong `/user`. Hồ sơ đầy đủ là việc của truy vấn, không phải của một
#: tin nhắn — in hết là vừa vô dụng vừa vượt giới hạn Telegram.
USER_GRANT_LIMIT: Final = 10

#: Khoá có tên khớp một trong các mẫu này thì **giá trị bị che** trong `/cauhinh`.
#: Bảng `settings` hiện chưa có khoá nào như vậy (bí mật nằm ở biến môi trường, không nằm
#: trong DB) — mẫu ở đây là hàng rào cho ngày mai, để một khoá `*.token` thêm vào sau này
#: không lặng lẽ hiện nguyên văn trong một tin nhắn Telegram.
SECRET_KEY_PATTERNS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "private",
    "pepper",
    "hmac",
)

MASKED: Final = "••• (đã che)"

#: Tên kiểu cho người đọc. Dùng ở thông báo từ chối của `/setcauhinh`, nên phải nói được
#: **cách gõ đúng**, không chỉ tên kiểu.
TYPE_HINTS: Final[dict[str, str]] = {
    "int": "số nguyên (ví dụ: 30)",
    "money_vnd": "số tiền VNĐ nguyên (ví dụ: 10000 hoặc 10.000)",
    "bp": "điểm cơ bản, 100 bp = 1% (ví dụ: 250)",
    "seconds": "số giây (ví dụ: 60)",
    "bool": "true hoặc false",
    "string": "chuỗi văn bản",
    "json": 'JSON hợp lệ (ví dụ: [10000, 20000] hoặc {"a": 1})',
}

#: Cú pháp hiển thị trong `/help_admin`. Đây là **bảng trang trí**, không phải nguồn
#: quyền: lệnh nào được liệt kê do `admin_permissions` quyết định, bảng này chỉ thêm cú
#: pháp cho những lệnh nó biết. Lệnh lạ vẫn hiện, chỉ là không có phần cú pháp.
COMMAND_SYNTAX: Final[dict[str, str]] = {
    "/add_giffcode": "/add_giffcode <category> <value> CODE1 CODE2 … — nạp code vào kho",
    "/del_code": "/del_code CODE — xoá một code chưa phát",
    "/del_all_code": "/del_all_code <category> [value] — xoá toàn bộ code chưa dùng",
    "/resend_tanthu": "/resend_tanthu [@user|user_id] — phát bù code tân thủ",
    "/stats": "/stats — tổng quan hệ thống",
    "/users": "/users [n] — danh sách user mới nhất",
    "/codes": "/codes [used] — kho code còn lại / code đã phát",
    "/broadcast": "/broadcast <nội dung> — tạo đợt bắn tin",
    "/broadcast_pause": "/broadcast_pause <job_id> — tạm dừng một đợt",
    "/broadcast_resume": "/broadcast_resume <job_id> — chạy tiếp một đợt",
    "/broadcast_status": "/broadcast_status <job_id> — tiến độ một đợt",
    "/send_event": "/send_event — tạo event đập hộp",
    "/update_share_event": "/update_share_event — cập nhật event chia sẻ",
    "/show_share_event": "/show_share_event — xem event chia sẻ hiện tại",
    "/done_event": "/done_event <@user|user_id> — trao code event chia sẻ",
    "/checkip": "/checkip <@user|user_id|IP> — tra tín hiệu định danh",
    "/help_admin": "/help_admin — các lệnh bạn được phép dùng",
    "/huongdan": "/huongdan — hướng dẫn chi tiết",
    "/tonkho": "/tonkho — tồn kho theo loại × mệnh giá",
    "/cauhinh": "/cauhinh [tiền_tố] — xem tham số đang chạy",
    "/setcauhinh": "/setcauhinh <khoá> <giá trị> — đổi tham số nóng",
    "/baocao": "/baocao <ngay|tuan|thang> [csv] — xuất báo cáo",
    "/user": "/user <@user|user_id> — hồ sơ một người",
    "/ban": "/ban <user_id> [lý do] — khoá một tài khoản",
    "/unban": "/unban <user_id> — gỡ khoá",
    "/admin_add": "/admin_add <user_id> <owner|admin|cskh|ketoan> — cấp quyền admin",
    "/admin_del": "/admin_del <user_id> — thu hồi quyền admin",
    "/chiendich": "/chiendich <start|end|extend> … — quản lý chiến dịch mời bạn",
    "/broadcast_cancel": "/broadcast_cancel <job_id> — huỷ hẳn một đợt",
    "/noidung": "/noidung [tiền_tố] — danh sách tin nhắn sửa được",
    "/xemnoidung": "/xemnoidung <khoá> — xem bản đang chạy + biến bắt buộc",
    "/suanoidung": "/suanoidung <khoá> <nội dung> — sửa một tin nhắn",
    "/resetnoidung": "/resetnoidung <khoá> — về bản mặc định",
}

#: `12.000.000` → 12000000. Chỉ khớp khi MỌI nhóm sau dấu chấm đúng ba chữ số, nên `1.5`
#: KHÔNG lọt qua và không có cách nào để một số thập phân bị đọc nhầm thành hàng triệu.
_VN_THOUSANDS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")

_TRUE_WORDS: Final[frozenset[str]] = frozenset({"true", "1", "on", "yes", "bat", "bật"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"false", "0", "off", "no", "tat", "tắt"})

_INT_TYPES: Final[frozenset[str]] = frozenset({"int", "money_vnd", "bp", "seconds"})


class ValueParseError(ValueError):
    """Giá trị admin gõ không hợp với `value_type` của khoá.

    Mang theo ``expected`` để tầng handler in ra **kiểu mong đợi**, không phải một câu
    "giá trị không hợp lệ" mà admin phải tự đoán.
    """

    def __init__(self, expected: str) -> None:
        self.expected = expected
        super().__init__(expected)


# ── Tiện ích chung ──────────────────────────────────────────────────


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, body: str) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await _sender(context).send_message(chat.id, body)


async def _reply_lines(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    header: str,
    lines: Sequence[str],
    *,
    footer: str = "",
) -> None:
    """Gửi một danh sách dài thành nhiều tin, cắt theo ranh giới DÒNG.

    Cắt giữa dòng thì một khoá cấu hình bị xé làm đôi qua hai tin nhắn và không còn đọc
    được — mà `/cauhinh` không lọc gì thì đúng là dài hơn giới hạn của Telegram.
    """
    sender = _sender(context)
    chat = update.effective_chat
    if chat is None:
        return

    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if size + len(line) + 1 > MAX_MESSAGE_CHARS and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        parts = [header if index == 1 else f"{header} (tiếp {index}/{total})", ""]
        parts.extend(chunk)
        if footer and index == total:
            parts.extend(["", footer])
        await sender.send_message(chat.id, "\n".join(parts))


def _actor_id(update: Update) -> int:
    tg_user = update.effective_user
    # Decorator `admin_command` đã chặn `effective_user is None`, nên nhánh này không xảy
    # ra; giữ để type checker không phải tin vào một bất biến nằm ở file khác.
    return tg_user.id if tg_user is not None else 0


def _parse_user_id(raw: str) -> int | None:
    cleaned = raw.strip()
    if cleaned.startswith("-"):
        return None
    return int(cleaned) if cleaned.isdigit() else None


def _vn_time(moment: datetime | None) -> str:
    """Mốc thời gian giờ VN, hoặc `—` nếu NULL.

    Tầng dưới lưu và truyền UTC (`core/clock.py`); admin chỉ đọc được giờ VN. Ép
    `astimezone` ngay tại chỗ hiển thị, đúng cái bẫy đã làm streak điểm danh của bot cũ
    lệch 7 tiếng. Không dùng lại hàm của `domain/texts.py` vì hàm đó là chuỗi hiển thị cho
    NGƯỜI DÙNG (§13.2); màn hình admin không nằm trong phạm vi đó.
    """
    return moment.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y") if moment is not None else "—"


# ── /cauhinh ────────────────────────────────────────────────────────

_SQL_SETTINGS_LIST = """
SELECT key, value, value_type, label_vi, sensitive
  FROM settings
 WHERE CAST(:prefix AS text) = '' OR key LIKE CAST(:pattern AS text)
 ORDER BY key
"""


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


def render_value(value: Any, value_type: str, *, full: bool = False) -> str:
    """Giá trị JSONB → chuỗi cho admin đọc.

    Chỉ `money_vnd` được nhóm hàng nghìn: nhóm cả `seconds` thì `3.600` trông như một số
    tiền, và đó đúng là kiểu nhầm lẫn mà quy ước hiển thị sinh ra để tránh.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value_type == "money_vnd" and isinstance(value, int):
        return f"{texts.format_vnd(value)}đ"
    if isinstance(value, str):
        return value or "(rỗng)"
    if isinstance(value, int | float):
        return str(value)

    blob = json.dumps(value, ensure_ascii=False, separators=(",", ": "))
    if full or len(blob) <= 160:
        return blob
    return f"{blob[:157]}…"


@admin_command("/cauhinh")
async def cmd_cauhinh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Liệt kê khoá cấu hình đang chạy, lọc theo tiền tố (`/cauhinh event.`).

    Lọc trúng **đúng một** khoá thì in giá trị đầy đủ, không cắt: đó là cách xem trọn một
    bảng tỉ lệ event trước khi sửa nó bằng `/setcauhinh`.
    """
    args = _args(context)
    prefix = args[0].strip() if args else ""

    async with session() as db:
        rows = (
            await db.execute(text(_SQL_SETTINGS_LIST), {"prefix": prefix, "pattern": f"{prefix}%"})
        ).all()

    header = "🔧 CẤU HÌNH ĐANG CHẠY" + (f" — lọc `{prefix}`" if prefix else "")

    if not rows:
        await _reply(
            update,
            context,
            f"{header}\n\n"
            f"Không có khoá nào khớp tiền tố `{prefix}`.\n"
            "👉 Gõ /cauhinh không tham số để xem toàn bộ.",
        )
        return

    full = len(rows) == 1
    lines: list[str] = []
    for row in rows:
        badge = " 🔒" if row.sensitive else ""
        shown = (
            MASKED if _is_secret(row.key) else render_value(row.value, row.value_type, full=full)
        )
        lines.append(f"• {row.key} [{row.value_type}]{badge}\n  {row.label_vi}\n  = {shown}")

    await _reply_lines(
        update,
        context,
        header,
        lines,
        footer=(
            f"📊 Tổng: {len(rows)} khoá\n"
            "🔒 = cần duyệt hai người mới đổi được\n"
            "👉 Đổi: /setcauhinh <khoá> <giá trị>"
        ),
    )


# ── /setcauhinh ─────────────────────────────────────────────────────

_SQL_SETTING_ROW = """
SELECT key, value, value_type, label_vi, sensitive
  FROM settings
 WHERE key = :key
"""


def parse_setting_value(raw: str, value_type: str) -> Any:
    """Chuỗi admin gõ → giá trị Python đúng `value_type` của khoá.

    Ném:
        ValueParseError: gõ sai kiểu. `expected` là hướng dẫn gõ đúng.
    """
    hint = TYPE_HINTS.get(value_type, value_type)
    cleaned = raw.strip()

    if value_type in _INT_TYPES:
        candidate = cleaned.replace("_", "")
        if _VN_THOUSANDS.match(candidate):
            candidate = candidate.replace(".", "")
        if not re.fullmatch(r"-?\d+", candidate):
            raise ValueParseError(hint)
        return int(candidate)

    if value_type == "bool":
        lowered = cleaned.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValueParseError(hint)

    if value_type == "json":
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueParseError(hint) from exc
        if not isinstance(parsed, dict | list):
            # `settings_service._TYPE_FAMILIES` chỉ nhận dict/list cho `json`. Chặn ở đây
            # để thông báo nói được kiểu mong đợi thay vì ném ConfigError chung chung.
            raise ValueParseError(hint)
        return parsed

    if value_type == "string":
        return cleaned

    raise ValueParseError(hint)


@admin_command("/setcauhinh")
async def cmd_setcauhinh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Đổi một khoá cấu hình nóng, trong **một** giao dịch với `settings_audit`.

    Khoá `sensitive` bị **từ chối** thay vì đổi lặng lẽ: §13.4.1 buộc chúng phải có người
    duyệt thứ hai, và luồng thu chữ ký thứ hai chưa tồn tại. Hỏng theo hướng an toàn.
    """
    args = _args(context)
    if len(args) < 2:
        await _reply(
            update,
            context,
            "⚠️ Cú pháp: /setcauhinh <khoá> <giá trị>\n\n"
            "Ví dụ: /setcauhinh referral.interval 5\n"
            "👉 Xem danh sách khoá: /cauhinh",
        )
        return

    key = args[0].strip()
    raw = " ".join(args[1:]).strip()
    actor = _actor_id(update)

    async with session() as db:
        row = (await db.execute(text(_SQL_SETTING_ROW), {"key": key})).one_or_none()

    if row is None:
        await _reply(
            update,
            context,
            f"❌ Không có khoá cấu hình `{key}`.\n\n👉 Xem danh sách: /cauhinh",
        )
        return

    if row.sensitive:
        await _reply(
            update,
            context,
            f"🔒 Khoá `{key}` cần **duyệt hai người** (§13.4.1) — chưa đổi được bằng lệnh.\n\n"
            f"📌 Giá trị hiện tại: {render_value(row.value, row.value_type, full=True)}",
        )
        return

    try:
        parsed = parse_setting_value(raw, row.value_type)
    except ValueParseError as exc:
        await _reply(
            update,
            context,
            f"❌ Giá trị không đúng kiểu của khoá `{key}`.\n\n"
            f"📐 Kiểu mong đợi: {row.value_type} — {exc.expected}\n"
            f"✍️ Bạn đã gõ: {raw}\n\n"
            f"📌 Giá trị hiện tại giữ nguyên: {render_value(row.value, row.value_type)}",
        )
        return

    old_render = render_value(row.value, row.value_type, full=True)

    try:
        async with transaction() as db:
            # `settings_service.set` ghi `settings` + `settings_audit`, kiểm min/max, và
            # xoá cache của tiến trình này. Ghi `audit_log` trong CÙNG giao dịch để không
            # có đường nào đổi cấu hình mà thiếu một trong hai cuốn sổ.
            await settings_service.set(key, parsed, updated_by=actor, db=db)
            await admin_service.write_audit(
                db,
                actor_id=actor,
                action="setcauhinh",
                entity_type="settings",
                entity_id=key,
                before={"value": row.value},
                after={"value": parsed},
            )
    except ConfigError as exc:
        await _reply(
            update,
            context,
            f"❌ Không đổi được `{key}`: {exc}\n\n📌 Giá trị hiện tại giữ nguyên: {old_render}",
        )
        return

    ttl = int(settings_service.CACHE_TTL_SECONDS)
    await _reply(
        update,
        context,
        "✅ ĐÃ ĐỔI CẤU HÌNH\n"
        "\n"
        f"🔑 {key} — {row.label_vi}\n"
        f"📤 Cũ:  {old_render}\n"
        f"📥 Mới: {render_value(parsed, row.value_type, full=True)}\n"
        "\n"
        f"⏳ Hiệu lực trên MỌI worker trong tối đa {ttl} giây (cache cấu hình).\n"
        "📝 Đã ghi settings_audit và audit_log.",
    )


# ── /stats ──────────────────────────────────────────────────────────

_SQL_SYSTEM_STATS = """
SELECT total_users, codes_available, codes_issued, total_referrals, total_value_vnd, updated_at
  FROM system_stats
 WHERE id = 1
"""

_SQL_VERIFIED_USERS = "SELECT count(*) FROM users WHERE verified_at IS NOT NULL"

_SQL_STATS_DIRECT = """
SELECT (SELECT count(*) FROM users)                                        AS total_users,
       (SELECT count(*) FROM codes WHERE status = 'available')             AS codes_available,
       (SELECT count(*) FROM code_grants WHERE state = 'delivered')        AS codes_issued,
       (SELECT count(*) FROM referrals WHERE qualified_at IS NOT NULL)     AS total_referrals,
       -- `sum(bigint)` trả về NUMERIC trong Postgres; ép về bigint để `format_vnd` nhận
       -- đúng một số nguyên thay vì một Decimal in ra kèm phần thập phân.
       (SELECT coalesce(sum(value_vnd), 0)::bigint
          FROM code_grants WHERE state = 'delivered')                      AS total_value_vnd
"""


@admin_command("/stats")
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tổng quan hệ thống. Ưu tiên `system_stats`, thiếu thì tính trực tiếp và NÓI RÕ.

    Nói rõ nguồn là bắt buộc chứ không phải lịch sự: `system_stats` do scheduler làm mới
    mỗi 30 giây, còn bản tính trực tiếp là ảnh chụp ngay lúc gõ lệnh. Hai con số lệch nhau
    là chuyện bình thường — nhưng chỉ khi người đọc biết mình đang nhìn cái nào.
    """
    async with session() as db:
        row = (await db.execute(text(_SQL_SYSTEM_STATS))).one_or_none()
        # `system_stats` không có cột "đã xác minh", nên con số này LUÔN tính trực tiếp.
        verified = (await db.execute(text(_SQL_VERIFIED_USERS))).scalar_one()
        if row is None:
            row = (await db.execute(text(_SQL_STATS_DIRECT))).one()
            source = "tính TRỰC TIẾP từ database (bảng system_stats chưa có dữ liệu)"
            stamp = ""
        else:
            source = "bảng system_stats (scheduler làm mới mỗi 30 giây)"
            stamp = f"\n⏱ Ảnh chụp lúc: {_vn_time(row.updated_at)}"

    await _reply(
        update,
        context,
        "📊 THỐNG KÊ HỆ THỐNG\n"
        "\n"
        f"👥 Tổng người dùng: {texts.format_vnd(row.total_users)}\n"
        f"✅ Đã xác minh: {texts.format_vnd(verified)}\n"
        f"🎟️ Code khả dụng: {texts.format_vnd(row.codes_available)}\n"
        f"🎁 Code đã phát: {texts.format_vnd(row.codes_issued)}\n"
        f"💰 Tổng giá trị đã phát: {texts.format_vnd(row.total_value_vnd)}đ\n"
        f"🤝 Lượt mời đạt chuẩn: {texts.format_vnd(row.total_referrals)}\n"
        "\n"
        f"{texts.SEP}\n"
        f"📌 Nguồn: {source}{stamp}\n"
        "ℹ️ Riêng số đã xác minh luôn tính trực tiếp — system_stats không có cột này.",
    )


# ── /user ───────────────────────────────────────────────────────────

_SQL_USER_BY_ID = """
SELECT u.user_id, u.username, u.full_name, u.joined_at, u.verified_at,
       u.points_balance, u.checkin_streak, u.risk_score,
       b.reason AS ban_reason, b.banned_at, b.unbanned_at,
       (SELECT count(*) FROM referrals r WHERE r.referrer_id = u.user_id)      AS refs_total,
       (SELECT count(*) FROM referrals r
         WHERE r.referrer_id = u.user_id AND r.qualified_at IS NOT NULL)       AS refs_qualified,
       (SELECT coalesce(sum(g.value_vnd), 0)::bigint FROM code_grants g
         WHERE g.user_id = u.user_id AND g.state = 'delivered')                AS value_total,
       (SELECT count(*) FROM code_grants g WHERE g.user_id = u.user_id)        AS grants_total
  FROM users u
  LEFT JOIN user_bans b ON b.user_id = u.user_id
 WHERE u.user_id = :uid
"""

_SQL_USER_BY_USERNAME = "SELECT user_id FROM users WHERE lower(username) = lower(:un)"

_SQL_USER_BY_PK = "SELECT user_id FROM users WHERE user_id = :uid"

_SQL_USER_GRANTS = """
SELECT g.grant_type, g.value_vnd, g.state, g.created_at, c.code_value
  FROM code_grants g
  LEFT JOIN codes c ON c.code_id = g.code_id
 WHERE g.user_id = :uid
 ORDER BY g.created_at DESC
 LIMIT :lim
"""

_GRANT_STATE_VI: Final[dict[str, str]] = {
    "reserved": "đang giữ chỗ",
    "delivered": "đã giao",
    "revoked": "đã thu hồi",
}


async def resolve_user(db: AsyncSession, token: str) -> int | None:
    """`@username` hoặc `user_id` → `user_id`. None khi không tìm thấy.

    Tra username bằng `lower()` để khớp index `uq_users_username_lower`: username của
    Telegram không phân biệt hoa thường, và CSKH gõ lại tên theo trí nhớ.
    """
    cleaned = token.strip()
    if cleaned.startswith("@"):
        params = {"un": cleaned[1:]}
        return (await db.execute(text(_SQL_USER_BY_USERNAME), params)).scalar_one_or_none()

    parsed = _parse_user_id(cleaned)
    if parsed is None:
        return None
    result = await db.execute(text(_SQL_USER_BY_PK), {"uid": parsed})
    return result.scalar_one_or_none()


@admin_command("/user")
async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hồ sơ một người — thay câu SQL mà CSKH hiện phải nhờ kỹ thuật viên chạy."""
    args = _args(context)
    if not args:
        await _reply(
            update,
            context,
            "⚠️ Cú pháp: /user <@username|user_id>\n\nVí dụ: /user @nguoidung  ·  /user 123456789",
        )
        return

    token = args[0]
    async with session() as db:
        user_id = await resolve_user(db, token)
        if user_id is None:
            await _reply(update, context, f"❌ Không tìm thấy người dùng `{token}`.")
            return
        profile = (await db.execute(text(_SQL_USER_BY_ID), {"uid": user_id})).one()
        grants = (
            await db.execute(text(_SQL_USER_GRANTS), {"uid": user_id, "lim": USER_GRANT_LIMIT})
        ).all()

    banned = profile.banned_at is not None and profile.unbanned_at is None
    if banned:
        ban_line = f"🚫 ĐANG BỊ KHOÁ từ {_vn_time(profile.banned_at)} — {profile.ban_reason}"
    elif profile.banned_at is not None:
        ban_line = f"✅ Không bị khoá (đã gỡ lúc {_vn_time(profile.unbanned_at)})"
    else:
        ban_line = "✅ Không bị khoá"

    verified_line = (
        f"✅ Đã xác minh lúc {_vn_time(profile.verified_at)}"
        if profile.verified_at is not None
        else "❌ Chưa xác minh"
    )

    if grants:
        shown = [
            f"• {g.grant_type} · {texts.format_vnd(g.value_vnd)}đ · "
            f"{g.code_value or '(chưa gắn mã)'} · {_GRANT_STATE_VI.get(g.state, g.state)} · "
            f"{_vn_time(g.created_at)}"
            for g in grants
        ]
        grant_block = (
            f"🎁 MÃ ĐÃ NHẬN ({len(grants)}/{profile.grants_total} gần nhất):\n" + "\n".join(shown)
        )
    else:
        grant_block = "🎁 MÃ ĐÃ NHẬN: chưa có"

    await _reply(
        update,
        context,
        "👤 HỒ SƠ NGƯỜI DÙNG\n"
        "\n"
        f"🆔 {profile.user_id} · {texts.display_name(profile.username, profile.full_name)}\n"
        f"📅 Vào bot: {_vn_time(profile.joined_at)}\n"
        f"{verified_line}\n"
        f"{ban_line}\n"
        "\n"
        f"🤝 Đã mời: {profile.refs_total} người ({profile.refs_qualified} đạt chuẩn)\n"
        f"💰 Tổng giá trị đã nhận: {texts.format_vnd(profile.value_total)}đ\n"
        f"⭐ Điểm: {texts.format_vnd(profile.points_balance)}đ · "
        f"🔥 Streak: {profile.checkin_streak} ngày · 🎯 Rủi ro: {profile.risk_score}\n"
        "\n"
        f"{texts.SEP}\n"
        f"{grant_block}",
    )


# ── /ban · /unban ───────────────────────────────────────────────────

DEFAULT_BAN_REASON: Final = "Không ghi lý do"

_SQL_BAN = """
INSERT INTO user_bans (user_id, reason, banned_by, banned_at)
     VALUES (:uid, :reason, :by, now())
ON CONFLICT (user_id) DO UPDATE
        SET reason     = EXCLUDED.reason,
            banned_by  = EXCLUDED.banned_by,
            banned_at  = now(),
            unbanned_at = NULL
RETURNING (xmax = 0) AS is_new
"""

_SQL_UNBAN = """
UPDATE user_bans
   SET unbanned_at = now()
 WHERE user_id = :uid
   AND unbanned_at IS NULL
RETURNING reason
"""


@admin_command("/ban")
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Khoá một tài khoản bằng `user_bans` — **KHÔNG** xoá dòng `users`.

    `DELETE FROM users` làm thủng sổ cái (`code_ledger` còn bút toán trỏ tới người không
    còn tồn tại) và xoá luôn bằng chứng điều tra (`07-db.md` §F).
    """
    args = _args(context)
    if not args:
        await _reply(update, context, "⚠️ Cú pháp: /ban <user_id> [lý do]")
        return

    user_id = _parse_user_id(args[0])
    if user_id is None:
        await _reply(update, context, f"❌ `{args[0]}` không phải user_id hợp lệ.")
        return

    reason = " ".join(args[1:]).strip() or DEFAULT_BAN_REASON
    actor = _actor_id(update)

    # Giao dịch chỉ bọc phần chạm database. Gửi tin Telegram **bên ngoài**: một lời gọi
    # mạng nằm trong giao dịch giữ khoá hàng suốt thời gian chờ Telegram trả lời.
    async with transaction() as db:
        exists = await admin_service.user_exists(db, user_id)
        if exists:
            is_new = (
                await db.execute(text(_SQL_BAN), {"uid": user_id, "reason": reason, "by": actor})
            ).scalar_one()
            await admin_service.write_audit(
                db,
                actor_id=actor,
                action="ban",
                entity_type="user",
                entity_id=str(user_id),
                after={"reason": reason},
            )

    if not exists:
        await _reply(
            update,
            context,
            f"❌ Không có user `{user_id}` trong bảng users — người này chưa từng /start.",
        )
        return

    note = "" if is_new else "\nℹ️ Người này đã có hồ sơ khoá trước đó — hồ sơ được cập nhật."
    await _reply(
        update,
        context,
        f"🚫 ĐÃ KHOÁ TÀI KHOẢN {user_id}\n\n📝 Lý do: {reason}\n"
        f"👮 Người thao tác: {actor}\n"
        f"ℹ️ Dòng `users` KHÔNG bị xoá — chỉ ghi `user_bans`.{note}",
    )


@admin_command("/unban")
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gỡ khoá — điền `unbanned_at`, không xoá hồ sơ khoá."""
    args = _args(context)
    if not args:
        await _reply(update, context, "⚠️ Cú pháp: /unban <user_id>")
        return

    user_id = _parse_user_id(args[0])
    if user_id is None:
        await _reply(update, context, f"❌ `{args[0]}` không phải user_id hợp lệ.")
        return

    actor = _actor_id(update)
    async with transaction() as db:
        reason = (await db.execute(text(_SQL_UNBAN), {"uid": user_id})).scalar_one_or_none()
        if reason is not None:
            await admin_service.write_audit(
                db,
                actor_id=actor,
                action="unban",
                entity_type="user",
                entity_id=str(user_id),
                before={"reason": reason},
            )

    if reason is None:
        await _reply(update, context, f"ℹ️ User {user_id} hiện không bị khoá.")
        return
    await _reply(
        update,
        context,
        f"✅ ĐÃ GỠ KHOÁ TÀI KHOẢN {user_id}\n\n📝 Lý do khoá trước đó: {reason}\n"
        "ℹ️ Hồ sơ khoá vẫn được giữ để tra cứu.",
    )


# ── /admin_add · /admin_del ─────────────────────────────────────────


@admin_command("/admin_add")
async def cmd_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cấp hoặc đổi vai trò admin. Quyền của lệnh này do `admin_permissions` quyết định.

    Cố ý **không** có `if role != 'owner'` ở đây: D14 nói bản đồ lệnh → vai trò nằm trong
    bảng. Một điều kiện cứng trong code sẽ là nguồn sự thật thứ hai, và hai nguồn thì sớm
    muộn cũng nói khác nhau.
    """
    args = _args(context)
    if len(args) < 2:
        await _reply(
            update,
            context,
            "⚠️ Cú pháp: /admin_add <user_id> <vai_trò>\n\n"
            f"Vai trò hợp lệ: {' · '.join(admin_service.ROLES)}",
        )
        return

    user_id = _parse_user_id(args[0])
    if user_id is None:
        await _reply(update, context, f"❌ `{args[0]}` không phải user_id hợp lệ.")
        return

    role = args[1].strip().lower()
    if role not in admin_service.ROLES:
        await _reply(
            update,
            context,
            f"❌ Vai trò `{args[1]}` không hợp lệ.\n\n"
            f"Chọn một trong: {' · '.join(admin_service.ROLES)}",
        )
        return

    actor = _actor_id(update)
    # `grant_role` tự ghi `audit_log` (action `admin.grant_role`) — không ghi thêm dòng thứ
    # hai ở đây, hai bản ghi cho một hành động làm sổ kiểm toán đếm sai.
    async with transaction() as db:
        exists = await admin_service.user_exists(db, user_id)
        if exists:
            change = await admin_service.grant_role(db, user_id=user_id, role=role, added_by=actor)

    if not exists:
        await _reply(
            update,
            context,
            f"❌ Không có user `{user_id}` trong bảng users — "
            "người này phải /start với bot ít nhất một lần trước.",
        )
        return

    await _cap_nhat_menu(context, user_id)

    if change.old_role is None:
        body = f"👑 ĐÃ CẤP QUYỀN\n\n🆔 {user_id}\n🎖 Vai trò: {role}"
    else:
        body = f"👑 ĐÃ ĐỔI VAI TRÒ\n\n🆔 {user_id}\n📤 Cũ: {change.old_role}\n📥 Mới: {role}"
    await _reply(update, context, f"{body}\n👮 Người thao tác: {actor}")


async def _cap_nhat_menu(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Đặt lại menu lệnh của một người ngay sau khi quyền của họ đổi.

    Nhập cục bộ để tránh vòng nhập: `apps/worker/main.py` đã nhập module này ở đầu file.

    Không có bước này thì người vừa được cấp quyền nhìn vào một menu trống cho tới lần
    restart kế tiếp, còn người vừa bị THU HỒI vẫn thấy nguyên 33 lệnh — bấm vào thì bot im
    lặng (đúng luật), nhưng menu vẫn đang quảng cáo những thứ họ không còn được dùng.

    Nuốt mọi lỗi: quyền đã ghi vào database rồi, và một lời gọi Telegram hỏng không được
    phép làm `/admin_add` trông như đã thất bại.
    """
    from televip.apps.worker import main as worker_main

    try:
        await worker_main.set_menu_for_user(context.application, user_id)
    except Exception as exc:  # noqa: BLE001 — xem docstring
        log.warning("cap_nhat_menu_that_bai", user_id=user_id, loi=str(exc))


@admin_command("/admin_del")
async def cmd_admin_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Thu hồi quyền admin bằng `revoked_at` — dòng `admin_users` được giữ lại."""
    args = _args(context)
    if not args:
        await _reply(update, context, "⚠️ Cú pháp: /admin_del <user_id>")
        return

    user_id = _parse_user_id(args[0])
    if user_id is None:
        await _reply(update, context, f"❌ `{args[0]}` không phải user_id hợp lệ.")
        return

    actor = _actor_id(update)
    # `revoke` tự ghi `audit_log` (action `admin.revoke`) và tự xoá cache vai trò.
    async with transaction() as db:
        change = await admin_service.revoke(db, user_id=user_id, revoked_by=actor)

    if change.unchanged:
        await _reply(update, context, f"ℹ️ User {user_id} hiện không phải admin đang hoạt động.")
        return

    await _cap_nhat_menu(context, user_id)
    await _reply(
        update,
        context,
        f"🔻 ĐÃ THU HỒI QUYỀN\n\n🆔 {user_id}\n🎖 Vai trò cũ: {change.old_role}\n"
        f"👮 Người thao tác: {actor}\n"
        "ℹ️ Dòng `admin_users` được giữ lại (đặt `revoked_at`), không xoá.",
    )


# ── /users ──────────────────────────────────────────────────────────

#: Mặc định của `/users` theo §13.4.2 mục 6. Trần cứng để một cú `/users 100000` không
#: kéo cả bảng lên rồi cắt bỏ ở tầng hiển thị — công việc nặng nhất nằm ở truy vấn.
USERS_DEFAULT: Final = 30
USERS_MAX: Final = 100

_SQL_USERS_RECENT = """
SELECT u.user_id, u.username, u.full_name, u.joined_at, u.verified_at,
       (b.user_id IS NOT NULL) AS dang_khoa
  FROM users u
  LEFT JOIN user_bans b ON b.user_id = u.user_id AND b.unbanned_at IS NULL
 ORDER BY u.joined_at DESC
 LIMIT :lim
"""

_SQL_USERS_TOTAL = """
SELECT count(*)                                              AS tong,
       count(*) FILTER (WHERE verified_at IS NOT NULL)       AS da_xac_minh,
       count(*) FILTER (WHERE joined_at >= now() - interval '24 hours') AS moi_24h
  FROM users
"""


@admin_command("/users", mutates=False)
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Danh sách người dùng mới nhất — §13.4.2 mục 6.

    Sắp theo `joined_at DESC` chứ không theo `last_active`: câu hỏi mà lệnh này trả lời là
    "ai vừa vào", và một tài khoản cũ mở lại bot không phải người mới. `/user <id>` là chỗ
    xem một người cụ thể.
    """
    args = _args(context)
    limit = USERS_DEFAULT
    if args:
        # `isdecimal` chứ không `isdigit`: `"²".isdigit()` là True nhưng `int("²")` ném.
        if not args[0].isdecimal() or int(args[0]) == 0:
            await _reply(
                update,
                context,
                f"⚠️ Cú pháp: /users [n]   (n là số nguyên dương, mặc định {USERS_DEFAULT}, "
                f"tối đa {USERS_MAX})",
            )
            return
        limit = min(int(args[0]), USERS_MAX)

    async with session() as db:
        tong = (await db.execute(text(_SQL_USERS_TOTAL))).one()
        rows = (await db.execute(text(_SQL_USERS_RECENT), {"lim": limit})).all()

    lines = [
        f"{'🚫' if row.dang_khoa else ('✅' if row.verified_at is not None else '⬜')} "
        f"{texts.display_name(row.username, row.full_name)} ({row.user_id}) "
        f"· {_vn_time(row.joined_at)}"
        for row in rows
    ]
    await _reply_lines(
        update,
        context,
        (
            f"👥 {len(rows)} NGƯỜI DÙNG MỚI NHẤT\n"
            f"📊 Tổng {tong.tong:,} · đã xác minh {tong.da_xac_minh:,} · "
            f"vào trong 24h {tong.moi_24h:,}"
        ),
        lines or ["(chưa có người dùng nào)"],
        footer="✅ đã xác minh · ⬜ chưa · 🚫 đang bị khoá",
    )


# ── /huongdan ───────────────────────────────────────────────────────

#: Ba phần của `/huongdan`, gửi thành ba tin (§13.4.2 mục 15). Là **khoá câu chữ** chứ
#: không phải chuỗi cứng: đây chính là loại nội dung admin muốn sửa mà không nhờ ai deploy.
GUIDE_KEYS: Final[tuple[str, ...]] = ("guide.codes", "guide.broadcast", "guide.config")


@admin_command("/huongdan", mutates=False)
async def cmd_huongdan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hướng dẫn vận hành, ba tin nhắn.

    Ba tin riêng chứ không một tin dài: mỗi phần vốn đã sát trần 4.096 ký tự của Telegram,
    và gộp lại thì phần cuối bị nuốt mất mà không báo lỗi gì.
    """
    for key in GUIDE_KEYS:
        await _reply(update, context, await text_service.render(key))


# ── /help_admin ─────────────────────────────────────────────────────


@admin_command("/help_admin")
async def cmd_help_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Liệt kê **đúng những lệnh mà chính người gọi được phép chạy**.

    Đọc `admin_permissions` theo vai trò của người gọi. Một `cskh` không bao giờ nhìn thấy
    `/broadcast` ở đây — không phải vì nó bị lọc ra, mà vì nó chưa từng nằm trong danh
    sách được đọc lên.
    """
    actor = _actor_id(update)

    async with session() as db:
        role = await admin_service.get_role(db, actor)
        if role is None:
            # Decorator đã cho qua, nên chuyện này chỉ xảy ra khi quyền bị thu hồi đúng
            # giữa hai truy vấn. Trả lời trung thực thay vì in một danh sách rỗng khó hiểu.
            await _reply(update, context, "ℹ️ Quyền của bạn vừa thay đổi, hãy thử lại.")
            return
        commands = await admin_service.commands_for_role(db, role)

    if not commands:
        await _reply(
            update,
            context,
            f"🎖 Vai trò của bạn: {role}\n\n"
            "⚠️ Vai trò này chưa được gán lệnh nào trong `admin_permissions`.",
        )
        return

    lines = [f"• {COMMAND_SYNTAX.get(cmd, cmd)}" for cmd in commands]
    await _reply_lines(
        update,
        context,
        f"📖 LỆNH BẠN ĐƯỢC PHÉP DÙNG\n🎖 Vai trò: {role}",
        lines,
        footer=f"📊 Tổng: {len(commands)} lệnh (đọc từ bảng admin_permissions)",
    )


#: Đăng ký ở `apps/worker/main.py`. Tên lệnh KHÔNG kèm dấu gạch chéo — `CommandHandler`
#: tự thêm. Giữ ở đây thay vì rải trong `main.py` để thêm một lệnh là sửa đúng một file.
HANDLERS: Final[tuple[tuple[str, Any], ...]] = (
    ("cauhinh", cmd_cauhinh),
    ("setcauhinh", cmd_setcauhinh),
    ("stats", cmd_stats),
    ("user", cmd_user),
    ("ban", cmd_ban),
    ("unban", cmd_unban),
    ("admin_add", cmd_admin_add),
    ("admin_del", cmd_admin_del),
    ("help_admin", cmd_help_admin),
    ("users", cmd_users),
    ("huongdan", cmd_huongdan),
)


__all__ = [
    "COMMAND_SYNTAX",
    "HANDLERS",
    "TYPE_HINTS",
    "ValueParseError",
    "cmd_admin_add",
    "cmd_admin_del",
    "cmd_ban",
    "cmd_cauhinh",
    "cmd_help_admin",
    "cmd_huongdan",
    "cmd_setcauhinh",
    "cmd_stats",
    "cmd_unban",
    "cmd_user",
    "cmd_users",
    "parse_setting_value",
    "render_value",
    "resolve_user",
]
