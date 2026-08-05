"""Tiến trình bot.

Ở dev chạy polling cho tiện; production chuyển sang webhook bằng cách điền
`TELEVIP_WEBHOOK_BASE_URL` — không đổi một dòng handler nào.

Khởi động theo thứ tự: cấu hình → log → database → redis → bot. Mỗi bước fail-fast:
thà không khởi động được còn hơn chạy với một nửa hạ tầng rồi hỏng lúc có người dùng.

## Đây là nơi DUY NHẤT nối handler vào bot

Ba bảng dữ liệu ở đầu file (`ADMIN_COMMANDS`, `ROUTE_HANDLERS`, `CALLBACK_HANDLERS`) là
toàn bộ sơ đồ đấu nối. Thêm một nút là thêm một dòng, và không có cách nào thêm nhầm nó
vào **sau** lưới an toàn callback ở cuối `register_handlers()`.

Hai bảng sau trỏ tới handler bằng chuỗi `"module:hàm"` chứ không bằng hàm đã import. Lý do
rất cụ thể: các nhóm luồng được viết song song, nên một module có thể chưa nằm trong cây
source lúc file này được nạp. Với chuỗi, module thiếu chỉ làm **một nút** rơi vào lưới an
toàn kèm một dòng WARNING gọi đích danh nó; với `import` ở đầu file, cả tiến trình không
khởi động được. Danh sách nút chưa nối đọc được bằng `unresolved_targets()`.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from televip.apps.worker.handlers import referral as referral_handlers
from televip.apps.worker.handlers.admin import broadcast as admin_broadcast
from televip.apps.worker.handlers.admin import codes as admin_codes
from televip.apps.worker.handlers.admin import event as admin_event
from televip.apps.worker.handlers.admin import ops as admin_ops
from televip.apps.worker.handlers.admin import texts as admin_texts
from televip.apps.worker.outbox_worker import run_outbox_worker
from televip.cache.client import close_redis, init_redis
from televip.core.config import get_settings
from televip.core.logging import get_logger, setup_logging
from televip.db.engine import dispose_engine, init_engine, session, transaction
from televip.services import admin as admin_service
from televip.services import broadcast as broadcast_service
from televip.services import code_issuance, media, settings_service, stats
from televip.services.admin import Handler
from televip.services.membership import handle_chat_member_update
from televip.telegram import keyboards
from televip.telegram.sender import Sender

log = get_logger(__name__)

_HANDLERS_PKG: Final = "televip.apps.worker.handlers"
_ADMIN_PKG: Final = f"{_HANDLERS_PKG}.admin"

#: Lệnh admin → handler. Mỗi handler đã mang `@admin_command(...)`, nên đăng ký ở đây
#: KHÔNG cấp quyền cho ai: người không có quyền vẫn gõ được lệnh và vẫn bị chặn ở
#: decorator. Danh sách này chỉ nói "lệnh này có người xử lý".
#:
#: Cố ý là một bảng dữ liệu chứ không phải 14 dòng `app.add_handler`: thêm một lệnh admin
#: là thêm một dòng, và không có cách nào thêm nhầm nó vào SAU lưới an toàn callback.
ADMIN_COMMANDS: list[tuple[str, Handler]] = [
    # Kho code (§13.4.2)
    ("add_giffcode", admin_codes.handle_add_giffcode),
    ("del_code", admin_codes.handle_del_code),
    ("resend_tanthu", admin_codes.handle_resend_tanthu),
    ("codes", admin_codes.handle_codes),
    ("tonkho", admin_codes.handle_tonkho),
    # Vận hành và cấu hình nóng (§13.4.2 mục 5, 14 và §13.4.3)
    ("cauhinh", admin_ops.cmd_cauhinh),
    ("setcauhinh", admin_ops.cmd_setcauhinh),
    ("stats", admin_ops.cmd_stats),
    ("user", admin_ops.cmd_user),
    ("ban", admin_ops.cmd_ban),
    ("unban", admin_ops.cmd_unban),
    ("admin_add", admin_ops.cmd_admin_add),
    ("admin_del", admin_ops.cmd_admin_del),
    ("help_admin", admin_ops.cmd_help_admin),
    # Nội dung tin nhắn sửa nóng (§13.4.3) — `handlers/admin/texts.py`.
    *admin_texts.COMMANDS.items(),
    # Bắn tin hàng loạt (§13.4.2 mục 8 và §13.4.3). `/broadcast` KHÔNG gửi ngay khi gõ:
    # nó dựng bản xem thử rồi chờ nút xác nhận — xem `handlers/admin/broadcast.py`.
    *admin_broadcast.COMMANDS,
    # Phát event đập hộp (§13.4.2 mục 9), cũng có bước xem thử + xác nhận.
    *admin_event.COMMANDS,
]

#: Nhãn nút ReplyKeyboard → handler, khoá theo tên route của `keyboards.ROUTE_TABLE`.
#: Bảng phải phủ **đúng** tập route đó (có test giữ hai bên không lệch nhau).
ROUTE_HANDLERS: dict[str, str] = {
    "play_game": f"{_HANDLERS_PKG}.misc:handle_play_game",
    "tan_thu": f"{_HANDLERS_PKG}.tanthu:handle_tanthu",
    "moi_ban": f"{_HANDLERS_PKG}.referral:handle_moi_ban",
    "share_event": f"{_HANDLERS_PKG}.misc:handle_share_event",
    "checkin": f"{_HANDLERS_PKG}.checkin:handle_checkin",
    # Nút này chỉ mở MÀN HÌNH đổi code; lượt đổi thật đi qua callback `redeem_<mệnh giá>`.
    "redeem_code": f"{_HANDLERS_PKG}.checkin:handle_redeem_menu",
    "leaderboard": f"{_HANDLERS_PKG}.misc:handle_leaderboard",
    "check_share": f"{_HANDLERS_PKG}.referral:handle_check_share",
    "stats": f"{_HANDLERS_PKG}.misc:handle_stats",
    "support": f"{_HANDLERS_PKG}.misc:handle_support",
}

#: `(mẫu callback_data, "module:hàm")`, đăng ký theo đúng thứ tự này. Mẫu nào cũng phải
#: neo hai đầu (`^...$`): một mẫu lỏng nuốt callback của nút khác và triệu chứng là "bấm
#: nút A thì màn hình B hiện ra".
CALLBACK_HANDLERS: tuple[tuple[str, str], ...] = (
    (f"^{keyboards.CB_OPEN_GIFT}$", f"{_HANDLERS_PKG}.open_gift:handle_open_gift"),
    (f"^{keyboards.CB_CHECK_GROUPS}$", f"{_HANDLERS_PKG}.tanthu:handle_check_groups"),
    (f"^{keyboards.CB_JOIN_REFERRAL}$", f"{_HANDLERS_PKG}.referral:handle_join_referral"),
    (f"^{keyboards.CB_LB_TODAY}$", f"{_HANDLERS_PKG}.misc:handle_leaderboard_today"),
    (f"^{keyboards.CB_LB_ALLTIME}$", f"{_HANDLERS_PKG}.misc:handle_leaderboard_alltime"),
    # Nút "đang xem" của bảng xếp hạng: vẫn phải được answer, nếu không nó quay vòng.
    (f"^{keyboards.CB_NOOP}$", f"{_HANDLERS_PKG}.misc:handle_noop"),
    (rf"^{keyboards.CB_OPEN_BOX_PREFIX}\d+$", f"{_HANDLERS_PKG}.event_box:handle_open_box"),
    (rf"^{keyboards.CB_REDEEM_PREFIX}\d+$", f"{_HANDLERS_PKG}.checkin:handle_redeem"),
    # Nút xác nhận của hai lệnh admin có bước xem thử. Handler tự kiểm quyền
    # (`@admin_command`) — đăng ký ở đây không cấp quyền cho ai, và cả hai phải đứng TRƯỚC
    # lưới an toàn callback.
    (
        admin_broadcast.CALLBACK_PATTERN,
        f"{_ADMIN_PKG}.broadcast:handle_broadcast_callback",
    ),
    (admin_event.CALLBACK_PATTERN, f"{_ADMIN_PKG}.event:handle_send_event_callback"),
    (admin_codes.DEL_ALL_CALLBACK_PATTERN, f"{_ADMIN_PKG}.codes:handle_del_all_callback"),
)

#: Menu lệnh cho người dùng thường (13-dac-ta §13.3.2). Lệnh admin KHÔNG nằm ở đây —
#: chúng chỉ hiện với từng admin qua `BotCommandScopeChat`.
PUBLIC_COMMANDS = [
    BotCommand("start", "🎁 Bắt đầu nhận code"),
    BotCommand("help", "❓ Hướng dẫn sử dụng bot"),
]


#: Khoá giữ task của worker outbox trong `bot_data` — để chỗ tắt tiến trình huỷ được nó.
OUTBOX_TASK_KEY = "outbox_worker_task"

#: Chu kỳ job trả mã giữ chỗ quá hạn về kho. Hằng chỉ là đường lui khi `settings` chưa seed.
REAP_SECONDS_KEY: Final = "jobs.reap_reservations_seconds"
DEFAULT_REAP_SECONDS: Final = 60

#: Chu kỳ job bơm đợt bắn tin đang chạy. Đây là mắt xích duy nhất biến một lần bấm "Gửi"
#: trên panel web thành tin thật bay đi: web không giữ kết nối Telegram nào nên nó chỉ ghi
#: `state='running'` vào bảng, còn việc bơm phải xảy ra TRONG tiến trình này.
PUMP_SECONDS_KEY: Final = "jobs.broadcast_pump_seconds"
DEFAULT_PUMP_SECONDS: Final = 15

#: Chu kỳ job tải ảnh của panel lên Telegram. Tiến trình web không có `Bot` nên nó chỉ ghi
#: bytes vào `media_uploads`; đây là chỗ duy nhất biến bytes đó thành một `file_id`.
MEDIA_SECONDS_KEY: Final = "jobs.media_upload_seconds"
DEFAULT_MEDIA_SECONDS: Final = 10


# ══════════════════════════════════════════════════════════════════════
# Phân giải "module:hàm"
# ══════════════════════════════════════════════════════════════════════

#: Những đích không phân giải được ở lần đăng ký gần nhất. Đọc bằng `unresolved_targets()`.
_unresolved: list[str] = []


def unresolved_targets() -> list[str]:
    """Các `"module:hàm"` chưa nối được — nút của chúng rơi vào lưới an toàn."""
    return list(_unresolved)


def _resolve(target: str) -> Handler | None:
    """`"gói.module:hàm"` → hàm, hoặc `None` nếu module/hàm chưa tồn tại.

    Chỉ nuốt đúng một tình huống: **chính** module đích chưa có mặt. Một `ImportError` từ
    bên trong module (lỗi thật của module đó) vẫn ném ra ngoài — nuốt nó đi là biến một
    lỗi cú pháp thành một nút im lặng không ai giải thích được.
    """
    module_path, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name != module_path:
            raise
        _unresolved.append(target)
        log.warning("handler_chua_co", target=target)
        return None

    handler = getattr(module, attribute, None)
    if handler is None:
        # Module có nhưng tên hàm khác: đây là lệch giao kèo giữa hai khối, không phải
        # "chưa làm xong" — nên là ERROR, không phải WARNING.
        _unresolved.append(target)
        log.error("handler_sai_ten", target=target)
        return None
    return handler


def _discovered_admin_commands(known: set[str]) -> list[tuple[str, Handler]]:
    """Quét gói `handlers/admin`, lấy `COMMANDS` (hoặc `HANDLERS`) của module chưa khai báo.

    Bảng `ADMIN_COMMANDS` ở trên liệt kê tường minh các module đã có; vòng quét này là chỗ
    một module lệnh admin mới **tự** vào bot bằng cách xuất một trong hai tên đó, không cần
    ai nhớ sửa file này. Module không xuất gì cả thì bị bỏ qua kèm một dòng WARNING — im
    lặng ở đây nghĩa là một bộ lệnh admin không tồn tại mà không ai biết.
    """
    package = importlib.import_module(_ADMIN_PKG)
    found: list[tuple[str, Handler]] = []

    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{_ADMIN_PKG}.{info.name}")
        exported = getattr(module, "COMMANDS", None) or getattr(module, "HANDLERS", None)
        if exported is None:
            if info.name not in {"guard"}:
                log.warning("module_admin_khong_xuat_lenh", module=info.name)
            continue

        pairs = exported.items() if isinstance(exported, dict) else exported
        for name, handler in pairs:
            if name in known:
                continue
            known.add(name)
            found.append((name, handler))
            log.info("lenh_admin_tu_phat_hien", module=info.name, command=name)

    return found


def admin_command_handlers() -> list[tuple[str, Handler]]:
    """Bảng tường minh + những gì vòng quét tìm thêm được, không trùng tên lệnh."""
    out = list(ADMIN_COMMANDS)
    return out + _discovered_admin_commands({name for name, _ in out})


# ══════════════════════════════════════════════════════════════════════
# Đấu nối
# ══════════════════════════════════════════════════════════════════════


def register_handlers(app: Application) -> None:
    """Nối toàn bộ handler vào `app`, **đúng thứ tự**.

    Thứ tự là một phần của hành vi, không phải chuyện thẩm mỹ: python-telegram-bot giao
    update cho handler ĐẦU TIÊN khớp trong cùng một nhóm, nên lưới an toàn callback phải
    là dòng cuối cùng của hàm này.
    """
    _unresolved.clear()

    app.add_handler(CommandHandler("start", _require(f"{_HANDLERS_PKG}.start:handle_start")))

    for name, handler in admin_command_handlers():
        app.add_handler(CommandHandler(name, handler))

    # `filters.Text([...])` so khớp BẰNG NHAU TUYỆT ĐỐI với nhãn nút. Bot cũ dùng
    # `"Game" in text` nên mọi tin nhắn chứa chữ đó rơi nhầm handler (`keyboards.py`).
    for label, route in keyboards.ROUTE_TABLE.items():
        target = ROUTE_HANDLERS.get(route)
        if target is None:
            # Nút có trên bàn phím mà không có dòng nào trong bảng: lỗi lập trình, không
            # phải "khối kia chưa xong". Có test chặn, nên tới được đây là bảng vừa bị sửa.
            log.error("nut_khong_co_trong_bang", route=route)
            continue
        handler = _resolve(target)
        if handler is None:
            continue
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Text([label]), handler))

    for pattern, target in CALLBACK_HANDLERS:
        handler = _resolve(target)
        if handler is not None:
            app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    # Nguồn cập nhật `group_memberships`, 0 lời gọi API. Phải đi cùng `ALLOWED_UPDATES`
    # bên dưới — thiếu một trong hai là bảng đóng băng vĩnh viễn mà không có dòng lỗi nào.
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # Lưới an toàn, PHẢI đứng cuối cùng: mọi callback chưa có handler riêng vẫn được
    # `answerCallbackQuery`. Thiếu nó thì nút bấm quay vòng tới khi Telegram tự huỷ query
    # và người dùng bấm lại — đúng hành vi đặc tả §13.3.3 cấm. Handler này chỉ trả lời
    # cho nút, không làm gì khác, nên nút chưa nối vẫn im lặng đúng nghĩa chứ không treo.
    app.add_handler(CallbackQueryHandler(_answer_unhandled_callback))

    if _unresolved:
        log.warning("nut_chua_noi", targets=_unresolved, so_luong=len(_unresolved))


def _require(target: str) -> Handler:
    """Như `_resolve` nhưng bắt buộc phải có — dùng cho `/start`, cửa vào duy nhất của bot."""
    handler = _resolve(target)
    if handler is None:
        raise RuntimeError(f"không nối được handler bắt buộc: {target}")
    return handler


async def _answer_unhandled_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    log.info("callback_chua_noi", data=query.data)
    await context.application.bot_data["sender"].answer_callback(
        query, "Chức năng này đang được hoàn thiện."
    )


# ══════════════════════════════════════════════════════════════════════
# Job định kỳ
# ══════════════════════════════════════════════════════════════════════


async def job_refresh_system_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Làm mới bảng tổng hợp mà màn hình `📊Thống Kê TK` đọc (§13.2.5)."""
    async with transaction() as db:
        snapshot = await stats.refresh_system_stats(db)
    log.debug("system_stats_da_lam_moi", total_users=snapshot.total_users)


async def job_reap_reservations(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trả mã giữ chỗ quá hạn về kho.

    Không có job này thì mỗi lần gửi thất bại là một mã bị treo ở `reserved` vĩnh viễn:
    không ai nhận được nó và kho cũng không lấy lại được.
    """
    async with transaction() as db:
        released = await code_issuance.reap_reservations(db)
    if released:
        log.info("ma_giu_cho_qua_han_da_tra_ve_kho", so_luong=released)


async def job_bom_dot_bantin(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bơm tiếp mọi đợt bắn tin đang ở `running` — kể cả đợt do panel web bắt đầu.

    Thân hàm đúng một dòng, và đó là chủ ý: `resume_running_jobs()` ĐÃ là "quét bảng tìm
    việc chưa làm". Viết một vòng quét thứ hai ở đây là dựng bản sao của một luật.

    Không có job này thì một đợt do web bắt đầu sẽ đứng im: tệp đích đầy, KHÔNG một dòng
    `outbox_messages` nào, KHÔNG một tin nào bay, và KHÔNG một dòng log lỗi nào — cho tới
    lần khởi động lại bot kế tiếp, lúc đó cả đợt bỗng bắn đi. Đó là loại hỏng tệ nhất:
    hỏng im lặng, và tự sửa vào một thời điểm không ai chọn.

    `start_pump` tự bỏ qua đợt đã có vòng bơm sống trong tiến trình này, và một khoá tư vấn
    cấp database chặn hai TIẾN TRÌNH cùng bơm một đợt — nên gọi lặp lại là vô hại.
    """
    await broadcast_service.resume_running_jobs()


async def job_nap_anh(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tải ảnh panel vừa nhận lên Telegram và lấy `file_id`.

    Chạy trong tiến trình BOT vì đây là nơi duy nhất có `Sender`. Ảnh gửi vào nhóm admin —
    tin đó đồng thời là vết kiểm toán nhìn được bằng mắt: mỗi tấm ảnh vào hệ để lại đúng
    một tin trong nhóm.

    Ba nhánh hỏng, và phân biệt chúng là bắt buộc:

    * `Sender` **ném** (429 kéo dài, mạng chết) — tạm thời, lùi lại và giữ nguyên bytes.
    * `Sender` trả `None` — Telegram từ chối vĩnh viễn. Bỏ cuộc, nhưng GIỮ bytes để người
      vận hành xem lại mà không phải tải lên lần nữa.

    Lẫn hai loại này là hoặc bỏ cuộc trên một sự cố mạng thoáng qua, hoặc thử lại năm lần
    một tấm ảnh chắc chắn hỏng — và câu hiện cho người vận hành sai theo cùng cách.
    """
    sender = context.application.bot_data.get("sender")
    if sender is None:  # pragma: no cover - chỉ xảy ra nếu thứ tự khởi động đổi
        return

    async with transaction() as db:
        viec = await media.nhan_viec(db)
    if not viec:
        return

    group_id = get_settings().admin_group_id
    for v in viec:
        try:
            ket_qua = await sender.upload_photo(
                group_id,
                v.du_lieu,
                filename=v.ten_tep,
                caption=f"panel upload #{v.upload_id} · {v.sha256[:12]}",
                idem_key=f"media:{v.upload_id}",
            )
        except Exception as exc:  # noqa: BLE001 — mọi lỗi ném đều là TẠM THỜI, xem docstring
            async with transaction() as db:
                await media.danh_dau_hong(
                    db, upload_id=v.upload_id, attempts=v.attempts, loi=str(exc), vinh_vien=False
                )
            continue

        if ket_qua is None:
            async with transaction() as db:
                await media.danh_dau_hong(
                    db,
                    upload_id=v.upload_id,
                    attempts=v.attempts,
                    loi="Telegram từ chối tấm ảnh này, hoặc bot không ở trong nhóm admin.",
                    vinh_vien=True,
                )
            continue

        async with transaction() as db:
            await media.danh_dau_xong(
                db,
                upload_id=v.upload_id,
                sha256=v.sha256,
                file_id=ket_qua.file_id,
                width=ket_qua.width,
                height=ket_qua.height,
            )


async def job_award_referral_tiers(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phát bù mọi mốc mời bạn đã đạt mà chưa nhận.

    **Vì sao là job định kỳ chứ không gọi thẳng lúc xác minh:** người được mời xác minh
    ở tiến trình **web** (`/api/verify`), nơi đó chốt quan hệ giới thiệu vào database
    nhưng không có `Sender` để nhắn cho người mời. Việc gửi thuộc về tiến trình **worker**.

    Tách như vậy còn một cái lợi nữa: một lỗi khi gửi tin không kéo theo việc mất quan hệ
    giới thiệu vừa chốt — lượt chạy sau của job này sẽ phát lại, và `referral_rewards`
    có khoá chính `(user_id, tier_no)` nên không bao giờ phát trùng.
    """
    sender = context.application.bot_data["sender"]

    async with session() as db:
        candidates = (
            (
                await db.execute(
                    text("""
                SELECT r.referrer_id
                  FROM referrals r
                 WHERE r.qualified_at IS NOT NULL
                 GROUP BY r.referrer_id
                HAVING count(*) / GREATEST(:interval, 1)
                       > COALESCE(
                           (SELECT count(*) FROM referral_rewards w
                             WHERE w.user_id = r.referrer_id), 0)
                 LIMIT :limit
                """),
                    {
                        "interval": await settings_service.get_int("referral.interval", 5),
                        "limit": 200,
                    },
                )
            )
            .scalars()
            .all()
        )

    if not candidates:
        return

    awarded = 0
    for referrer_id in candidates:
        try:
            async with transaction() as db:
                tiers = await referral_handlers.award_pending_tiers(sender, db, referrer_id)
            awarded += len(tiers)
        except Exception:  # noqa: BLE001 - một người hỏng không được làm dừng cả lượt
            log.exception("phat_moc_that_bai", referrer_id=referrer_id)

    if awarded:
        log.info("da_phat_moc_moi_ban", so_moc=awarded, so_nguoi=len(candidates))


async def job_refresh_user_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Làm mới `user_stats` — bảng mà bảng xếp hạng toàn thời gian và màn hình hồ sơ đọc.

    Không có job này thì bảng rỗng vĩnh viễn: `📊Thống Kê TK` in "0 người đã mời" cho mọi
    người và `👑 BXH` toàn thời gian không bao giờ có tên ai, kể cả người đã mời 50 bạn.
    """
    async with transaction() as db:
        n = await stats.refresh_user_stats(db)
    log.debug("user_stats_da_lam_moi", so_dong=n)


async def schedule_jobs(app: Application) -> None:
    """Đặt các job định kỳ. Chu kỳ đọc từ `settings`, không viết cứng."""
    queue = app.job_queue
    if queue is None:  # pragma: no cover - chỉ xảy ra khi thiếu extra `job-queue`
        log.error("khong_co_job_queue")
        return

    stats_seconds = await settings_service.get_int(
        stats.REFRESH_SECONDS_KEY, stats.DEFAULT_REFRESH_SECONDS
    )
    reap_seconds = await settings_service.get_int(REAP_SECONDS_KEY, DEFAULT_REAP_SECONDS)
    pump_seconds = await settings_service.get_int(PUMP_SECONDS_KEY, DEFAULT_PUMP_SECONDS)
    media_seconds = await settings_service.get_int(MEDIA_SECONDS_KEY, DEFAULT_MEDIA_SECONDS)

    # `first=` bằng đúng chu kỳ: lúc khởi động, việc cần làm là phục vụ update chứ không
    # phải chạy hai truy vấn tổng hợp toàn bảng.
    queue.run_repeating(
        job_refresh_system_stats,
        interval=stats_seconds,
        first=stats_seconds,
        name="refresh_system_stats",
    )
    queue.run_repeating(
        job_reap_reservations,
        interval=reap_seconds,
        first=reap_seconds,
        name="reap_reservations",
    )
    # Phát mốc mời bạn: chu kỳ ngắn vì đây là phần thưởng người dùng đang chờ. 60 giây là
    # độ trễ chấp nhận được giữa lúc bạn mình xác minh xong và lúc mình nhận được mã.
    queue.run_repeating(
        job_award_referral_tiers,
        interval=60,
        first=30,
        name="award_referral_tiers",
    )
    queue.run_repeating(
        job_bom_dot_bantin,
        interval=pump_seconds,
        first=pump_seconds,
        name="bom_dot_bantin",
    )
    queue.run_repeating(
        job_nap_anh,
        interval=media_seconds,
        first=media_seconds,
        name="nap_anh",
    )
    queue.run_repeating(
        job_refresh_user_stats,
        interval=stats_seconds,
        first=stats_seconds + 5,  # lệch với job kia để hai truy vấn tổng hợp không đụng nhau
        name="refresh_user_stats",
    )
    log.info(
        "job_dinh_ky_da_dat",
        stats_giay=stats_seconds,
        reap_giay=reap_seconds,
        bom_giay=pump_seconds,
        anh_giay=media_seconds,
        so_job=6,
    )


# ══════════════════════════════════════════════════════════════════════
# Vòng đời tiến trình
# ══════════════════════════════════════════════════════════════════════


def _start_outbox_worker(app: Application) -> asyncio.Task[None]:
    """Chạy vòng lặp gửi outbox như một task nền **trong chính tiến trình bot**.

    Cùng tiến trình vì cả hai dùng chung một `Sender`, và token bucket 30 tin/giây chỉ có
    nghĩa khi mọi tin đi qua cùng một cửa. Không có nó thì mọi thứ vẫn "chạy": lệnh vẫn
    trả lời, `broadcast_targets` vẫn đầy lên — chỉ là không một tin nào rời hàng đợi.
    """
    task = asyncio.create_task(run_outbox_worker(app), name="outbox-worker")
    app.bot_data[OUTBOX_TASK_KEY] = task
    return task


async def _stop_outbox_worker(app: Application) -> None:
    """Huỷ task worker và **chờ nó dừng hẳn** trước khi đóng pool database.

    Không chờ thì `dispose_engine()` chạy trong khi worker còn đang giữ một session, và
    triệu chứng là một traceback vô nghĩa lúc tắt che mất lỗi thật (nếu có).
    """
    task = app.bot_data.pop(OUTBOX_TASK_KEY, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("outbox_worker_da_dung")


#: Trần của Telegram cho mô tả một lệnh trong menu. Vượt là `BadRequest` cho CẢ danh sách.
_MENU_DESC_MAX: Final = 256

_SQL_ADMIN_MENU = """
SELECT a.user_id, a.role
  FROM admin_users a
 WHERE a.revoked_at IS NULL
"""


def _menu_description(command: str) -> str:
    """Mô tả ngắn cho menu, lấy từ bảng cú pháp của `/help_admin`.

    Bảng ấy viết dạng `"/users [n] — danh sách user mới nhất"`; menu Telegram đã hiện sẵn
    tên lệnh nên chỉ lấy phần sau dấu gạch. Lệnh chưa có dòng nào trong bảng vẫn vào được
    menu, chỉ là mô tả rỗng — thà thế còn hơn biến mất khỏi menu.
    """
    raw = admin_ops.COMMAND_SYNTAX.get(command, "")
    _, sep, tail = raw.partition("—")
    return (tail if sep else raw).strip()[:_MENU_DESC_MAX]


async def _menu_cua_vai_tro(db: AsyncSession, role: str) -> list[BotCommand]:
    """Menu của một vai trò, đọc từ `admin_permissions`."""
    names = await admin_service.commands_for_role(db, role)
    return [
        BotCommand(name.lstrip("/"), _menu_description(name) or name.lstrip("/"))
        for name in sorted(names)
    ]


async def set_menu_for_user(app: Application, user_id: int) -> bool:
    """Đặt (hoặc xoá) menu lệnh của ĐÚNG một người theo quyền hiện tại của họ.

    Gọi ngay sau `/admin_add` và `/admin_del`. Không có nó thì người vừa được cấp quyền
    nhìn vào một menu trống cho tới lần restart kế tiếp, còn người vừa bị THU HỒI quyền
    vẫn thấy nguyên 33 lệnh trong menu — bấm vào thì bot im lặng (đúng luật), nhưng cái
    menu đó vẫn đang quảng cáo những thứ họ không còn được dùng.

    Trả `False` khi Telegram từ chối (người này chưa từng mở chat riêng với bot) — đó là
    chuyện thường, không phải lỗi, nên nơi gọi không cần xử lý gì.
    """
    async with session() as db:
        role = await admin_service.get_role(db, user_id)
        menu = await _menu_cua_vai_tro(db, role) if role else []

    try:
        # Không còn quyền ⇒ đặt danh sách RỖNG cho scope này. Telegram khi đó rơi về menu
        # mặc định (`/start`, `/help`), đúng thứ một người dùng thường phải thấy.
        await app.bot.set_my_commands(menu, scope=BotCommandScopeChat(user_id))
    except TelegramError as exc:
        log.warning("menu_admin_that_bai", user_id=user_id, loi=str(exc))
        return False
    return True


async def refresh_admin_menus(app: Application) -> int:
    """Đặt menu lệnh RIÊNG cho từng admin qua `BotCommandScopeChat`. Trả số admin đã đặt.

    Trước bản này, ghi chú của `PUBLIC_COMMANDS` hứa "lệnh admin chỉ hiện với từng admin
    qua `BotCommandScopeChat`" nhưng **không có dòng code nào làm việc đó** — menu của
    admin chỉ có `/start` và `/help`, còn 33 lệnh vận hành thì phải thuộc lòng mới gõ được.
    Một lệnh không ai tìm thấy thì cũng như chưa xây.

    Danh sách lấy từ `admin_permissions` theo vai trò — cùng một nguồn với `/help_admin`,
    nên menu không thể trôi khác bản in ra.

    Chạy lúc khởi động cho TẤT CẢ admin. `/admin_add` và `/admin_del` gọi
    `set_menu_for_user()` để cập nhật ngay đúng một người, không phải chờ restart.
    """
    async with session() as db:
        admins = (await db.execute(text(_SQL_ADMIN_MENU))).all()
        theo_vai_tro: dict[str, list[BotCommand]] = {
            role: await _menu_cua_vai_tro(db, role) for role in {row.role for row in admins}
        }

    da_dat = 0
    for row in admins:
        menu = theo_vai_tro.get(row.role, [])
        if not menu:
            continue
        try:
            await app.bot.set_my_commands(menu, scope=BotCommandScopeChat(row.user_id))
        except TelegramError as exc:
            # Admin chưa từng mở chat riêng với bot ⇒ Telegram từ chối đặt scope. Đó là
            # chuyện bình thường, không phải lỗi khởi động: nuốt và đi tiếp, nếu không thì
            # MỘT admin chưa /start làm cả menu của những người còn lại không được đặt.
            log.warning("menu_admin_that_bai", user_id=row.user_id, loi=str(exc))
            continue
        da_dat += 1
    return da_dat


async def _on_startup(app: Application) -> None:
    await app.bot.set_my_commands(PUBLIC_COMMANDS)
    so_menu = await refresh_admin_menus(app)
    log.info("menu_admin_da_dat", so_admin=so_menu)
    me = await app.bot.get_me()
    _start_outbox_worker(app)
    # Chu kỳ job đọc từ `settings`, tức từ database — nên phải đặt ở đây chứ không ở
    # `build_application()`, chỗ chưa được phép `await`.
    await schedule_jobs(app)
    # Đợt broadcast còn dở sau một lần restart: trạng thái nằm trong `broadcast_targets`
    # nên "chạy tiếp" chỉ là dựng lại vòng bơm. Thiếu bước này thì đợt đứng im vĩnh viễn ở
    # trạng thái `running` mà không có dòng lỗi nào.
    resumed = await broadcast_service.resume_running_jobs()
    log.info("bot_san_sang", username=me.username, bot_id=me.id, broadcast_chay_tiep=len(resumed))


def build_application() -> Application:
    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.env != "dev")

    init_engine(settings)
    init_redis(settings)

    app = (
        Application.builder()
        .token(settings.bot_token)
        # Nhiều update được xử lý song song. Ở bot cũ đây là nguồn của lỗi phát trùng
        # code; giờ an toàn vì ràng buộc chống trùng nằm ở tầng database chứ không
        # dựa vào việc chỉ có một update chạy tại một thời điểm.
        .concurrent_updates(True)
        .post_init(_on_startup)
        .build()
    )

    app.bot_data["settings"] = settings
    app.bot_data["sender"] = Sender(app.bot)

    register_handlers(app)
    return app


#: `chat_member` phải khai tường minh: Telegram KHÔNG gửi loại update này nếu không xin.
#: Đây là thứ thay cho vòng lặp `getChatMember` N+1 của bot cũ, vốn chiếm 90% lưu lượng API.
ALLOWED_UPDATES = [
    Update.MESSAGE,
    Update.CALLBACK_QUERY,
    Update.CHAT_MEMBER,
    Update.MY_CHAT_MEMBER,
]


async def run_forever(app: Application) -> None:
    """Vòng chạy chính.

    Cố ý KHÔNG dùng `app.run_polling()`: hàm đó gọi `asyncio.get_event_loop()`, thứ
    Python 3.14 đã bỏ, nên nó ném `RuntimeError: There is no current event loop`.
    Tự quản lý vòng đời cũng cho ta chỗ để tắt sạch (đóng pool DB và Redis) và là
    đúng khuôn cần dùng khi chuyển sang webhook.
    """
    await app.initialize()
    # `Application.initialize()` KHÔNG gọi `post_init` — thư viện chỉ gọi nó từ
    # `run_polling()`/`run_webhook()` (xem `_application.py`, docstring của `initialize`).
    # Vì ta tự quản lý vòng đời nên phải tự gọi, nếu không menu lệnh sẽ không bao giờ
    # được đăng ký và triệu chứng là "bot chạy nhưng không có lệnh nào trong menu".
    if app.post_init:
        await app.post_init(app)
    await app.start()
    await app.updater.start_polling(
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=False,
    )
    log.info("dang_lang_nghe")

    stop = asyncio.Event()
    try:
        await stop.wait()  # chạy tới khi bị Ctrl+C hoặc bị kill
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if app.updater.running:
            await app.updater.stop()
        await _stop_outbox_worker(app)
        await app.stop()
        await app.shutdown()


def main() -> None:
    app = build_application()
    settings = get_settings()

    log.info("khoi_dong", env=settings.env, che_do="webhook" if settings.use_webhook else "polling")

    try:
        asyncio.run(run_forever(app))
    except KeyboardInterrupt:
        log.info("nhan_tin_hieu_dung")
    finally:
        asyncio.run(_shutdown())


async def _shutdown() -> None:
    await close_redis()
    await dispose_engine()
    log.info("da_dong_ket_noi")


if __name__ == "__main__":
    main()
