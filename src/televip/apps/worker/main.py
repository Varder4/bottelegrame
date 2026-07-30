"""Tiến trình bot.

Ở dev chạy polling cho tiện; production chuyển sang webhook bằng cách điền
`TELEVIP_WEBHOOK_BASE_URL` — không đổi một dòng handler nào.

Khởi động theo thứ tự: cấu hình → log → database → redis → bot. Mỗi bước fail-fast:
thà không khởi động được còn hơn chạy với một nửa hạ tầng rồi hỏng lúc có người dùng.
"""

from __future__ import annotations

import asyncio

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from televip.apps.worker.handlers.admin import broadcast as admin_broadcast
from televip.apps.worker.handlers.admin import codes as admin_codes
from televip.apps.worker.handlers.admin import ops as admin_ops
from televip.apps.worker.handlers.admin import texts as admin_texts
from televip.apps.worker.handlers.open_gift import handle_open_gift
from televip.apps.worker.handlers.start import handle_start
from televip.apps.worker.handlers.tanthu import handle_check_groups, handle_tanthu
from televip.apps.worker.outbox_worker import run_outbox_worker
from televip.cache.client import close_redis, init_redis
from televip.core.config import get_settings
from televip.core.logging import get_logger, setup_logging
from televip.db.engine import dispose_engine, init_engine
from televip.services import broadcast as broadcast_service
from televip.services.membership import handle_chat_member_update
from televip.telegram import keyboards
from televip.telegram.sender import Sender

log = get_logger(__name__)

#: Lệnh admin → handler. Mỗi handler đã mang `@admin_command(...)`, nên đăng ký ở đây
#: KHÔNG cấp quyền cho ai: người không có quyền vẫn gõ được lệnh và vẫn bị chặn ở
#: decorator. Danh sách này chỉ nói "lệnh này có người xử lý".
#:
#: Cố ý là một bảng dữ liệu chứ không phải 14 dòng `app.add_handler`: thêm một lệnh admin
#: là thêm một dòng, và không có cách nào thêm nhầm nó vào SAU lưới an toàn callback.
ADMIN_COMMANDS: list[tuple[str, object]] = [
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
]

#: Menu lệnh cho người dùng thường (13-dac-ta §13.3.2). Lệnh admin KHÔNG nằm ở đây —
#: chúng chỉ hiện với từng admin qua `BotCommandScopeChat`.
PUBLIC_COMMANDS = [
    BotCommand("start", "🎁 Bắt đầu nhận code"),
    BotCommand("help", "❓ Hướng dẫn sử dụng bot"),
]


#: Khoá giữ task của worker outbox trong `bot_data` — để chỗ tắt tiến trình huỷ được nó.
OUTBOX_TASK_KEY = "outbox_worker_task"


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


async def _on_startup(app: Application) -> None:
    await app.bot.set_my_commands(PUBLIC_COMMANDS)
    me = await app.bot.get_me()
    _start_outbox_worker(app)
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

    app.add_handler(CommandHandler("start", handle_start))

    for name, handler in ADMIN_COMMANDS:
        app.add_handler(CommandHandler(name, handler))

    # `filters.Text([...])` so khớp BẰNG NHAU TUYỆT ĐỐI với nhãn nút. Bot cũ dùng
    # `"Game" in text` nên mọi tin nhắn chứa chữ đó rơi nhầm handler (`keyboards.py`).
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Text([keyboards.BTN_CODE_TAN_THU]),
            handle_tanthu,
        )
    )
    app.add_handler(
        CallbackQueryHandler(handle_check_groups, pattern=f"^{keyboards.CB_CHECK_GROUPS}$")
    )
    app.add_handler(CallbackQueryHandler(handle_open_gift, pattern=f"^{keyboards.CB_OPEN_GIFT}$"))
    # Hai nút xác nhận của `/broadcast`. Handler tự kiểm quyền (`@admin_command`) — đăng ký
    # ở đây không cấp quyền cho ai, và nó phải đứng TRƯỚC lưới an toàn callback bên dưới.
    app.add_handler(
        CallbackQueryHandler(
            admin_broadcast.handle_broadcast_callback,
            pattern=admin_broadcast.CALLBACK_PATTERN,
        )
    )
    # Nguồn cập nhật `group_memberships`, 0 lời gọi API. Phải đi cùng `ALLOWED_UPDATES`
    # bên dưới — thiếu một trong hai là bảng đóng băng vĩnh viễn mà không có dòng lỗi nào.
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # Lưới an toàn, PHẢI đứng cuối cùng: mọi callback chưa có handler riêng vẫn được
    # `answerCallbackQuery`. Thiếu nó thì nút bấm quay vòng tới khi Telegram tự huỷ query
    # và người dùng bấm lại — đúng hành vi đặc tả §13.3.3 cấm. Handler này chỉ trả lời
    # cho nút, không làm gì khác, nên nút chưa nối vẫn im lặng đúng nghĩa chứ không treo.
    app.add_handler(CallbackQueryHandler(_answer_unhandled_callback))

    return app


async def _answer_unhandled_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    log.info("callback_chua_noi", data=query.data)
    await context.application.bot_data["sender"].answer_callback(
        query, "Chức năng này đang được hoàn thiện."
    )


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
