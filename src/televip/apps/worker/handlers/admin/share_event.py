"""Bộ ba lệnh của chiến dịch chia sẻ — §13.4.2 mục 10, 11, 12.

    /update_share_event [link chia sẻ]   reply MỘT ẢNH để đặt bài
    /show_share_event                     xem đúng thứ người dùng đang thấy
    /done_event <@user|user_id>           trao code sau khi CSKH xác minh bài share

Ba điều chi phối cả file:

1. **`/update_share_event` phải là reply vào một ảnh.** Ảnh và caption đi cùng nhau vì
   đó là một bài đăng, không phải hai trường rời. Nhận ảnh ở một lệnh và caption ở lệnh
   khác thì luôn có một khoảng thời gian bài đăng sai — và bài đó vẫn đang được gửi cho
   người dùng suốt khoảng đó.

2. **`/show_share_event` dựng bản xem thử bằng CHÍNH đường mà người dùng đi.** Nó gọi lại
   handler của người dùng chứ không tự nối chuỗi. Một bản xem thử được dựng riêng sẽ trôi
   khỏi bản thật đúng vào lúc người ta cần nó nhất — lúc kiểm tra trước khi công bố.

3. **`/done_event` trao thưởng qua `code_issuance`, không qua một câu UPDATE riêng.**
   `grant_type='share_event'` có `once_per_life`, nên hàng rào "một lần trọn đời" nằm ở
   ràng buộc `uq_grant_once_semantic` của database, không nằm ở một câu `if` mà CSKH thứ
   hai bấm nhanh có thể chạy song song qua được.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers import misc
from televip.apps.worker.handlers.admin.ops import resolve_user
from televip.core.errors import AlreadyClaimed, OutOfStock
from televip.core.ids import grant_key_share_event
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import code_issuance, settings_service, text_service
from televip.services.admin import Handler, admin_command, write_audit
from televip.telegram import keyboards

log = get_logger(__name__)

CMD_UPDATE: Final = "/update_share_event"
CMD_SHOW: Final = "/show_share_event"
CMD_DONE: Final = "/done_event"

#: Loại trong KHO code. Khác tên với `GRANT_TYPE` một cách cố ý — xem `db/models/codes.py`.
CODE_TYPE: Final = "eventchiase"

#: Loại phát trong SỔ CÁI. `once_per_life = true`, và đó là hàng rào thật.
GRANT_TYPE: Final = "share_event"

VALUE_KEY: Final = "code.eventchiase_value_vnd"
DEFAULT_VALUE_VND: Final = 10_000

#: Trần caption của một tin có ảnh là 1.024 ký tự, KHÔNG phải 4.096 như tin văn bản.
#: Vượt trần là `BadRequest`, và lớp gửi coi đó là lỗi vĩnh viễn — bài đăng sẽ không bao
#: giờ tới được ai, trong khi bảng thì đã ghi nhận là cập nhật thành công.
MAX_CAPTION_CHARS: Final = 1_024

UPDATE_USAGE = (
    "📥 CÁCH DÙNG\n"
    "1. Đăng ảnh bài chia sẻ kèm caption vào chat này\n"
    "2. REPLY vào chính ảnh đó bằng: /update_share_event [link chia sẻ]\n"
    "\n"
    "Ví dụ: /update_share_event https://t.me/kenh/123\n"
    "\n"
    "👉 Caption lấy từ caption của ảnh được reply — ảnh và lời đi cùng nhau vì đó là một\n"
    "bài đăng. Không truyền link thì giữ nguyên link đang dùng.\n"
    "👉 Xem lại bằng /show_share_event trước khi công bố."
)

DONE_USAGE = "📥 Cách dùng: /done_event @username  hoặc  /done_event 123456789"

_SQL_READ_CONFIG = """
SELECT image_file_id, caption, share_link, updated_by, updated_at
  FROM share_event_config WHERE id = 1
"""

#: Bảng là **singleton** (`CHECK (id = 1)`): upsert chứ không insert, nếu không lần cập
#: nhật thứ hai sẽ nổ khoá chính thay vì ghi đè.
_SQL_WRITE_CONFIG = """
INSERT INTO share_event_config (id, image_file_id, caption, share_link, updated_by, updated_at)
     VALUES (1, :file_id, :caption, :share_link, :actor, now())
ON CONFLICT (id) DO UPDATE
        SET image_file_id = EXCLUDED.image_file_id,
            caption       = EXCLUDED.caption,
            -- Không truyền link mới thì giữ link cũ. Ghi đè bằng NULL ở đây nghĩa là hai
            -- nút của người dùng mất đường dẫn chỉ vì admin sửa mỗi cái ảnh.
            share_link    = coalesce(EXCLUDED.share_link, share_event_config.share_link),
            updated_by    = EXCLUDED.updated_by,
            updated_at    = now()
  RETURNING share_link
"""


# ── Tiện ích ────────────────────────────────────────────────────────


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return None if chat is None else chat.id


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


def photo_from_reply(update: Update) -> tuple[str, str] | None:
    """`(file_id, caption)` của ảnh được reply, hoặc `None` nếu lệnh không reply ảnh.

    Lấy `photo[-1]`: Telegram trả về nhiều cỡ của cùng một ảnh, sắp từ nhỏ tới lớn. Lấy
    phần tử đầu là lưu lại bản thu nhỏ, và bài đăng sẽ vỡ khi hiển thị.
    """
    message = update.effective_message
    replied = getattr(message, "reply_to_message", None) if message is not None else None
    if replied is None:
        return None
    photos = getattr(replied, "photo", None)
    if not photos:
        return None
    return photos[-1].file_id, (getattr(replied, "caption", None) or "")


# ── /update_share_event ─────────────────────────────────────────────


@admin_command(CMD_UPDATE)
async def cmd_update_share_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ghi `share_event_config` từ ảnh được reply (§13.4.2 mục 10)."""
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = _sender(context)

    found = photo_from_reply(update)
    if found is None:
        await sender.send_message(chat_id, UPDATE_USAGE)
        return
    file_id, caption = found

    if not caption.strip():
        await sender.send_message(
            chat_id,
            "❌ Ảnh được reply không có caption.\n\n"
            "Caption CHÍNH LÀ nội dung bài chia sẻ mà người dùng đọc; lưu một bài rỗng "
            "nghĩa là người dùng thấy một tấm ảnh không lời.",
        )
        return

    if len(caption) > MAX_CAPTION_CHARS:
        await sender.send_message(
            chat_id,
            f"❌ Caption dài {len(caption):,} ký tự, trần của một tin CÓ ẢNH là "
            f"{MAX_CAPTION_CHARS:,} (tin văn bản mới là 4.096).\n\n"
            "Lưu bản này thì bảng ghi nhận thành công còn bài đăng không tới được ai.",
        )
        return

    args = _args(context)
    share_link = args[0].strip() if args else None

    async with transaction() as db:
        truoc = (await db.execute(text(_SQL_READ_CONFIG))).one_or_none()
        sau_link = (
            await db.execute(
                text(_SQL_WRITE_CONFIG),
                {
                    "file_id": file_id,
                    "caption": caption,
                    "share_link": share_link,
                    "actor": tg_user.id,
                },
            )
        ).scalar_one()
        await write_audit(
            db,
            actor_id=tg_user.id,
            action="update_share_event",
            entity_type="share_event_config",
            entity_id="1",
            before=(
                None
                if truoc is None
                else {"caption_len": len(truoc.caption or ""), "share_link": truoc.share_link}
            ),
            after={"caption_len": len(caption), "share_link": sau_link},
        )

    await sender.send_message(
        chat_id,
        (
            "✅ ĐÃ CẬP NHẬT EVENT CHIA SẺ\n"
            f"\n"
            f"🖼 Ảnh: đã lưu\n"
            f"📝 Caption: {len(caption):,}/{MAX_CAPTION_CHARS:,} ký tự\n"
            f"🔗 Link chia sẻ: {sau_link or '(chưa đặt)'}\n"
            f"\n"
            f"👉 Xem đúng thứ người dùng sẽ thấy: /show_share_event"
        ),
    )
    log.info("cap_nhat_event_chia_se", actor_id=tg_user.id, caption_len=len(caption))


# ── /show_share_event ───────────────────────────────────────────────


def _render_summary(row: Any) -> str:
    """Tóm tắt cấu hình đang chạy. Dùng ở CẢ hai nhánh của `/show_share_event`.

    Nhánh nhóm không dựng được bản xem thử, nhưng phần tóm tắt thì luôn dựng được — và
    đó là thứ trả lời câu hỏi thường gặp nhất ("bài đã đặt chưa, link nào").
    """
    if row is None:
        return (
            "ℹ️ CHƯA CẤU HÌNH EVENT CHIA SẺ\n"
            "\n"
            "Người dùng đang thấy nội dung MẶC ĐỊNH.\n"
            "Đặt bài bằng /update_share_event (reply vào một ảnh)."
        )
    return (
        "👁 CẤU HÌNH EVENT CHIA SẺ\n"
        f"\n"
        f"🖼 Ảnh: {'đã có' if row.image_file_id else 'CHƯA CÓ'}\n"
        f"📝 Caption: {len(row.caption or ''):,} ký tự\n"
        f"🔗 Link chia sẻ: {row.share_link or '(chưa đặt)'}\n"
        f"✍️ Sửa lần cuối bởi: {row.updated_by or '—'}"
    )


@admin_command(CMD_SHOW, mutates=False)
async def cmd_show_share_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bản xem thử — dựng bằng CHÍNH handler của người dùng (§13.4.2 mục 11).

    Gọi `misc.handle_share_event` thay vì tự nối chuỗi: bản xem thử tự dựng sẽ trôi khỏi
    bản thật, và nó trôi đúng vào lúc người ta cần nó nhất. Đổi lại, mọi thứ người dùng
    thấy — ảnh chết thì rơi về text, thiếu link thì mất nút — đều hiện ra ở đây y hệt.
    """
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    sender = _sender(context)

    async with session() as db:
        row = (await db.execute(text(_SQL_READ_CONFIG))).one_or_none()

    # `handle_share_event` đi qua `misc._enter`, thứ trả `None` ngay khi `chat.type` không
    # phải `private` — nó là màn hình của người dùng, và người dùng chỉ gặp nó trong chat
    # riêng. Không kiểm ở đây thì lệnh in "Bên dưới là ĐÚNG thứ người dùng thấy 👇" rồi
    # **không gửi gì cả**: không ảnh, không lỗi, không dấu hiệu nào. Mà việc admin gõ lệnh
    # trong nhóm điều hành là trường hợp THƯỜNG, không phải ngoại lệ — và cả lệnh này tồn
    # tại để kiểm tra trước khi công bố, nên im lặng là kiểu hỏng tệ nhất có thể ở đây.
    if chat.type != "private":
        await sender.send_message(
            chat_id,
            (
                "ℹ️ Bản xem thử chỉ dựng được trong CHAT RIÊNG với bot.\n"
                "\n"
                "Lệnh này cố ý chạy lại đúng đường mà người dùng đi, và đường đó chặn mọi\n"
                "thứ không phải chat riêng — nên trong nhóm nó sẽ không hiện được gì.\n"
                "\n"
                f"{_render_summary(row)}\n"
                "\n"
                "👉 Nhắn /show_share_event cho bot trong chat riêng để xem bản đầy đủ."
            ),
        )
        return

    await sender.send_message(
        chat_id,
        f"{_render_summary(row)}\n\nBên dưới là ĐÚNG thứ người dùng thấy khi bấm 📢 EVENT 👇",
    )

    # `handle_share_event` có cooldown riêng của luồng người dùng. Đi qua nó nguyên vẹn là
    # cố ý: admin bấm hai lần liên tiếp cũng gặp đúng thứ người dùng gặp — kể cả câu "bạn
    # bấm hơi nhanh". Đó vẫn là một phản hồi, khác hẳn với im lặng.
    await misc.handle_share_event(update, context)


# ── /done_event ─────────────────────────────────────────────────────


@admin_command(CMD_DONE)
async def cmd_done_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trao code event chia sẻ sau khi CSKH đã xác minh bài share (§13.4.2 mục 12)."""
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = _sender(context)

    args = _args(context)
    if not args:
        await sender.send_message(chat_id, DONE_USAGE)
        return

    target_raw = args[0]
    async with session() as db:
        target_id = await resolve_user(db, target_raw)
    if target_id is None:
        await sender.send_message(chat_id, f"❌ Không tìm thấy người dùng: {target_raw}")
        return

    value_vnd = await settings_service.get_int(VALUE_KEY, DEFAULT_VALUE_VND)
    support_link = await settings_service.get_str("link.support", "")

    try:
        async with transaction() as db:
            grant = await code_issuance.reserve(
                db,
                user_id=target_id,
                grant_type=GRANT_TYPE,
                grant_key=grant_key_share_event(target_id),
                code_type=CODE_TYPE,
                value_vnd=value_vnd,
                reason={"duyet_boi": tg_user.id},
            )
    except AlreadyClaimed:
        # Sau khi `reserve()` biết gắn lại mã cho grant mồ côi, nhánh này chỉ còn nghĩa
        # một điều: grant đang ở trạng thái KHÔNG được tự sửa (`revoked`). Đó là dấu vết
        # của một lần can thiệp bằng tay, và đoán ý ở đây là ghi đè quyết định của con
        # người trên một đường tiêu tiền.
        await sender.send_message(
            chat_id,
            f"⛔ Suất event chia sẻ của {target_id} đang ở trạng thái đã bị can thiệp "
            f"(thu hồi), không tự trao lại được.\n\n"
            f"👉 Xem lịch sử: /user {target_id}",
        )
        log.warning("done_event_grant_bi_thu_hoi", actor_id=tg_user.id, target_id=target_id)
        return

    except OutOfStock:
        await sender.send_message(
            chat_id,
            f"❌ Kho code {CODE_TYPE} mệnh giá {texts.format_vnd(value_vnd)}đ đang RỖNG — "
            f"chưa trao được cho {target_id}.\n\n"
            f"👉 Nạp: /add_giffcode {CODE_TYPE} {texts.value_label(value_vnd).lower()} MA1 MA2\n"
            f"👉 Nạp xong chạy lại đúng lệnh này.",
        )
        log.error("done_event_het_kho", actor_id=tg_user.id, target_id=target_id)
        return

    if grant.was_existing and grant.state == "delivered":
        # `reserve()` idempotent theo `grant_key`: chạy lại KHÔNG cấp mã thứ hai, nó trả về
        # đúng grant cũ. Không phân biệt ở đây thì CSKH đọc được "đã trao" như thể vừa trao
        # mới, và `audit_log` có hai dòng cho một lần trao — đúng loại sai lệch làm sổ mất
        # giá trị đối chiếu. Đây là chỗ "một lần trọn đời" hiện ra với người vận hành;
        # hàng rào thật nằm ở `uq_grant_once_semantic` của database.
        #
        # Điều kiện là `state == 'delivered'`, KHÔNG phải `was_existing` một mình. Một
        # grant `reserved` nghĩa là lần gửi trước THẤT BẠI: người dùng chưa cầm gì cả, và
        # báo "đã nhận rồi" kèm số hiệu mã cho CSKH là đóng hồ sơ trên một lời không đúng.
        # Rơi xuống dưới thì lệnh gửi lại đúng mã đó — đúng việc CSKH đang muốn làm.
        await sender.send_message(
            chat_id,
            (
                f"⚠️ {target_id} ĐÃ NHẬN code event chia sẻ trước đó — không trao lại.\n"
                f"\n"
                f"🎁 Mã đã trao: {grant.code_value}\n"
                f"💰 Giá trị: {texts.format_vnd(grant.value_vnd)}đ\n"
                f"\n"
                f"Mỗi tài khoản chỉ nhận một lần trọn đời.\n"
                f"👉 Xem lịch sử: /user {target_id}"
            ),
        )
        log.info("done_event_da_nhan", actor_id=tg_user.id, target_id=target_id)
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
        # KHÔNG `mark_delivered`: grant nằm lại ở `reserved` và `reap_reservations()` trả
        # mã về kho. Đánh dấu đã giao một mã chưa ai nhận là làm sổ cái nói dối.
        await sender.send_message(
            chat_id,
            f"❌ Không gửi được cho {target_id} — người này đã chặn bot hoặc chưa từng mở "
            f"chat riêng.\n\nMã chưa bị tiêu; bảo họ mở chat với bot rồi chạy lại lệnh.",
        )
        log.warning("done_event_gui_that_bai", target_id=target_id, grant_id=grant.grant_id)
        return

    async with transaction() as db:
        await code_issuance.mark_delivered(db, grant_id=grant.grant_id)
        await write_audit(
            db,
            actor_id=tg_user.id,
            action="done_event",
            entity_type="code_grants",
            entity_id=str(grant.grant_id),
            after={"target_id": target_id, "value_vnd": grant.value_vnd},
        )

    await sender.send_message(
        chat_id,
        (
            f"✅ ĐÃ TRAO CODE EVENT CHIA SẺ\n"
            f"\n"
            f"👤 Người nhận: {target_id}\n"
            f"🎁 Mã: {grant.code_value}\n"
            f"💰 Giá trị: {texts.format_vnd(grant.value_vnd)}đ\n"
            f"\n"
            f"{'💁 Hỗ trợ: ' + support_link if support_link else ''}"
        ).rstrip(),
    )
    log.info(
        "done_event_xong",
        actor_id=tg_user.id,
        target_id=target_id,
        grant_id=grant.grant_id,
        value_vnd=grant.value_vnd,
    )


#: Vòng quét của `main.py` nhặt bảng này — thêm lệnh là sửa đúng một file.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (
    ("update_share_event", cmd_update_share_event),
    ("show_share_event", cmd_show_share_event),
    ("done_event", cmd_done_event),
)


__all__ = [
    "CMD_DONE",
    "CMD_SHOW",
    "CMD_UPDATE",
    "CODE_TYPE",
    "COMMANDS",
    "GRANT_TYPE",
    "MAX_CAPTION_CHARS",
    "cmd_done_event",
    "cmd_show_share_event",
    "cmd_update_share_event",
    "photo_from_reply",
]
