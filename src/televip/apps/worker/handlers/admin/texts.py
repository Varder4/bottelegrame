"""Lệnh sửa câu chữ — bốn cửa duy nhất để đổi tin nhắn mà không đụng code.

    /noidung [tiền_tố]           liệt kê khoá + 60 ký tự đầu
    /xemnoidung <khoá>           nội dung đầy đủ + biến bắt buộc + bản xem thử
    /suanoidung <khoá> <nội dung> sửa (giữ xuống dòng)
    /resetnoidung <khoá>         về bản mặc định trong domain/texts.py

Hai điều chi phối cả file:

1. **Mọi câu trả lời đều kèm bản XEM THỬ.** Admin gõ xong một template đầy `{ngoặc}` thì
   không có cách nào biết tin thật trông ra sao — trừ khi đợi một người dùng nhận nó.
   `/xemnoidung` và `/suanoidung` điền sẵn bộ giá trị mẫu của khoá để việc kiểm tra xảy ra
   **trước** khi tin được gửi đi, không phải sau.

2. **`/suanoidung` đọc thẳng `message.text`, KHÔNG dùng `context.args`.**
   `python-telegram-bot` cắt `args` theo khoảng trắng, và một chuỗi đã cắt thì không ghép
   lại được: mọi dấu xuống dòng biến mất. Câu chữ của bot toàn tin nhiều dòng, nên đi đường
   `args` là hỏng ngay ở lệnh đầu tiên có ai đó dùng thật.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from telegram import Update
from telegram.ext import ContextTypes

from televip.core.logging import get_logger
from televip.services import text_service
from televip.services.admin import Handler, admin_command
from televip.services.text_service import TemplateError

log = get_logger(__name__)

#: Trần an toàn của một tin Telegram là 4096 ký tự; cắt ở 3.500 để còn chỗ cho phần đầu.
MAX_MESSAGE_CHARS: Final = 3_500

#: Độ dài đoạn trích trong `/noidung`. Đủ để nhận ra câu, ngắn để một màn hình chứa được
#: nhiều khoá — danh sách đầy đủ là việc của `/xemnoidung`.
PREVIEW_CHARS: Final = 60

#: `/suanoidung` hoặc `/suanoidung@ten_bot`, rồi khoảng trắng. Phần còn lại là tham số THÔ
#: — giữ nguyên mọi ký tự xuống dòng.
_COMMAND_HEAD = re.compile(r"^/[A-Za-z_]+(?:@\w+)?[ \t]+")


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


def _actor_id(update: Update) -> int:
    tg_user = update.effective_user
    # `admin_command` đã chặn `effective_user is None`; nhánh này chỉ để type checker không
    # phải tin vào một bất biến nằm ở file khác.
    return tg_user.id if tg_user is not None else 0


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
    """Danh sách dài → nhiều tin, cắt theo ranh giới DÒNG.

    Cắt giữa dòng thì một khoá bị xé làm đôi qua hai tin và không còn đọc được — mà
    `/noidung` không lọc gì thì chắc chắn dài hơn giới hạn của Telegram.
    """
    chat = update.effective_chat
    if chat is None:
        return
    sender = _sender(context)

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


def raw_argument(update: Update) -> str:
    """Phần sau tên lệnh, GIỮ NGUYÊN xuống dòng. Chuỗi rỗng nếu không có gì.

    Xem điểm 2 của docstring module về lý do không dùng `context.args`.
    """
    message = update.effective_message
    body = getattr(message, "text", None) if message is not None else None
    if not body:
        return ""
    head = _COMMAND_HEAD.match(body)
    return body[head.end() :] if head is not None else ""


def split_key_and_content(raw: str) -> tuple[str, str]:
    """`"khoá  phần còn lại"` → `("khoá", "phần còn lại")`.

    Tách ở **cụm khoảng trắng đầu tiên** (kể cả xuống dòng), nên cả hai kiểu gõ đều được:
    khoá rồi nội dung trên cùng dòng, hoặc khoá rồi xuống dòng viết nội dung.
    """
    parts = re.split(r"\s+", raw.strip(), maxsplit=1)
    if not parts or not parts[0]:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def render_vars(required: Sequence[str]) -> str:
    if not required:
        return "🔤 Biến bắt buộc: (không có)"
    return "🔤 Biến bắt buộc: " + " · ".join(f"{{{name}}}" for name in required)


# ── /noidung ────────────────────────────────────────────────────────


@admin_command("/noidung", mutates=False)
async def cmd_noidung(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Liệt kê khoá câu chữ, lọc theo tiền tố (`/noidung tanthu.`)."""
    args = _args(context)
    prefix = args[0].strip() if args else ""

    infos = await text_service.list_keys(prefix or None)
    header = "💬 CÂU CHỮ ĐANG DÙNG" + (f" — lọc `{prefix}`" if prefix else "")

    if not infos:
        await _reply(
            update,
            context,
            f"{header}\n\nKhông có khoá nào khớp tiền tố `{prefix}`.\n"
            "👉 Gõ /noidung không tham số để xem toàn bộ.",
        )
        return

    lines: list[str] = []
    changed = 0
    for info in infos:
        if info.customized:
            changed += 1
        badge = " ✏️" if info.customized else ""
        lines.append(f"• {info.key}{badge}\n  {info.label_vi}\n  {_excerpt(info.content)}")

    await _reply_lines(
        update,
        context,
        header,
        lines,
        footer=(
            f"📊 Tổng: {len(infos)} khoá · ✏️ đã sửa: {changed}\n"
            "👉 Xem đầy đủ: /xemnoidung <khoá>\n"
            "👉 Sửa: /suanoidung <khoá> <nội dung mới>"
        ),
    )


def _excerpt(content: str) -> str:
    """60 ký tự đầu, xuống dòng đổi thành `⏎` để một khoá chỉ chiếm một dòng."""
    flat = content.replace("\n", " ⏎ ")
    return flat[:PREVIEW_CHARS] + ("…" if len(flat) > PREVIEW_CHARS else "")


# ── /xemnoidung ─────────────────────────────────────────────────────


@admin_command("/xemnoidung", mutates=False)
async def cmd_xemnoidung(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nội dung đầy đủ của một khoá, kèm biến bắt buộc và bản xem thử đã điền biến mẫu."""
    args = _args(context)
    if not args:
        await _reply(
            update,
            context,
            "⚠️ Cú pháp: /xemnoidung <khoá>\n\n"
            "Ví dụ: /xemnoidung code.delivered\n👉 Danh sách khoá: /noidung",
        )
        return

    key = args[0].strip()
    try:
        info = await text_service.get_info(key)
        sample = text_service.preview(key, info.content)
    except TemplateError as exc:
        await _reply(update, context, f"❌ {exc}\n\n👉 Danh sách khoá: /noidung")
        return

    state = "✏️ đã sửa" if info.customized else "📦 đang dùng bản mặc định"
    await _reply(
        update,
        context,
        f"💬 {info.key} — {info.label_vi}\n"
        f"{state}\n"
        f"{render_vars(info.required_vars)}\n"
        "\n"
        "📄 NỘI DUNG:\n"
        f"{info.content}\n"
        "\n"
        "👀 XEM THỬ (đã điền biến mẫu):\n"
        f"{sample}",
    )


# ── /suanoidung ─────────────────────────────────────────────────────


@admin_command("/suanoidung")
async def cmd_suanoidung(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Đổi câu chữ của một khoá. Từ chối nếu thiếu biến bắt buộc hoặc có biến lạ.

    Nội dung cũ **không đổi** khi bị từ chối: `set_content` kiểm trước rồi mới ghi, nên
    không có trạng thái nửa vời nào để dọn.
    """
    key, content = split_key_and_content(raw_argument(update))
    if not key or not content:
        await _reply(
            update,
            context,
            "⚠️ Cú pháp: /suanoidung <khoá> <nội dung mới>\n"
            "\n"
            "Nội dung giữ nguyên xuống dòng — cứ gõ nhiều dòng bình thường.\n"
            "👉 Xem khoá và biến bắt buộc: /xemnoidung <khoá>",
        )
        return

    actor = _actor_id(update)
    try:
        old = (await text_service.get_info(key)).content
    except TemplateError as exc:
        await _reply(update, context, f"❌ {exc}\n\n👉 Danh sách khoá: /noidung")
        return

    try:
        await text_service.set_content(key, content, updated_by=actor)
    except TemplateError as exc:
        required = (
            text_service.TEMPLATES[key].required_vars if key in text_service.TEMPLATES else ()
        )
        await _reply(
            update,
            context,
            f"❌ KHÔNG ĐỔI ĐƯỢC `{key}`\n"
            "\n"
            f"📛 Lý do: {exc}\n"
            f"{render_vars(required)}\n"
            "\n"
            "📌 Nội dung hiện tại giữ nguyên:\n"
            f"{old}",
        )
        return

    ttl = int(text_service.CACHE_TTL_SECONDS)
    await _reply(
        update,
        context,
        f"✅ ĐÃ ĐỔI CÂU CHỮ\n"
        "\n"
        f"💬 {key}\n"
        "\n"
        "👀 XEM THỬ (đã điền biến mẫu):\n"
        f"{text_service.preview(key, content)}\n"
        "\n"
        f"⏳ Hiệu lực trên MỌI worker trong tối đa {ttl} giây.\n"
        "📝 Đã ghi message_templates_audit (dùng để quay lui).\n"
        "↩️ Về bản gốc: /resetnoidung " + key,
    )


# ── /resetnoidung ───────────────────────────────────────────────────


@admin_command("/resetnoidung")
async def cmd_resetnoidung(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trả một khoá về bản mặc định trong `domain/texts.py`."""
    args = _args(context)
    if not args:
        await _reply(update, context, "⚠️ Cú pháp: /resetnoidung <khoá>\n\n👉 Danh sách: /noidung")
        return

    key = args[0].strip()
    actor = _actor_id(update)
    try:
        content = await text_service.reset(key, updated_by=actor)
    except TemplateError as exc:
        await _reply(update, context, f"❌ {exc}\n\n👉 Danh sách khoá: /noidung")
        return

    await _reply(
        update,
        context,
        f"↩️ ĐÃ VỀ BẢN MẶC ĐỊNH\n"
        "\n"
        f"💬 {key}\n"
        "\n"
        "📄 NỘI DUNG:\n"
        f"{content}\n"
        "\n"
        "👀 XEM THỬ (đã điền biến mẫu):\n"
        f"{text_service.preview(key, content)}",
    )


# ── Đăng ký ─────────────────────────────────────────────────────────

#: Tên lệnh (không có dấu `/`) → handler. `main.build_application()` chỉ cần lặp qua đây;
#: khối này KHÔNG tự sửa `main.py` để không giẫm lên khối khác.
COMMANDS: Final[dict[str, Handler]] = {
    "noidung": cmd_noidung,
    "xemnoidung": cmd_xemnoidung,
    "suanoidung": cmd_suanoidung,
    "resetnoidung": cmd_resetnoidung,
}


__all__ = [
    "COMMANDS",
    "MAX_MESSAGE_CHARS",
    "PREVIEW_CHARS",
    "cmd_noidung",
    "cmd_resetnoidung",
    "cmd_suanoidung",
    "cmd_xemnoidung",
    "raw_argument",
    "render_vars",
    "split_key_and_content",
]
