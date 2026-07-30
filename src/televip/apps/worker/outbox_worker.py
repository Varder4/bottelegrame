"""Vòng lặp rút việc khỏi `outbox_messages` và gửi đi.

Đây là **tiêu thụ** của hàng đợi ở `services/outbox.py`; nó không quyết định gửi gì cho
ai, chỉ làm đúng bốn việc và làm cho đúng:

    lấy một lô  →  xin token  →  gọi `Sender`  →  ghi kết quả

Bốn điều đáng nói, cả bốn đều là chỗ hệ cũ trả giá (`02-broadcast.md` §2.1):

1. **Xin token trước mỗi tin, và tôn trọng câu trả lời.** Hệ cũ bắn 30 tin song song rồi
   `sleep(1.0)`: trung bình 12-23 tin/giây (dưới hạn mức) nhưng tức thời 100 tin/giây
   (gấp ba hạn mức) — chậm mà vẫn ăn 429. Ở đây hết hạn chờ token là **trả việc về hàng
   đợi**, tuyệt đối không gửi liều.
2. **`Sender` trả `None` không đồng nghĩa với "lỗi".** Người đã chặn bot là người rời đi,
   không phải một lần gửi hỏng: đánh dấu `users.blocked_at`, dừng hẳn tin đó, và **không
   tính vào tỉ lệ lỗi**. Gộp hai thứ này lại là cách hệ cũ biến 403 thành `❌ Lỗi: 2.631`
   và không ai biết tệp thật còn bao nhiêu người.
3. **`Sender` ném là "chưa gửi được", không phải "hỏng".** Việc quay lại hàng đợi kèm
   backoff; hạn mức 5 lần thử là của lỗi thật.
4. **Một tin đang bay mà tiến trình chết thì không kẹt vĩnh viễn.** `reap_stuck()` chạy
   định kỳ ngay trong vòng lặp này.

Số coroutine gửi song song **không** làm tăng tốc vượt trần — token bucket mới quyết định
nhịp. Nó chỉ để hấp thụ độ trễ mạng (định luật Little: 28 tin/giây × RTT đuôi 1,5 s ≈ 42
việc đang bay), tránh cảnh bot ngồi chờ mạng trong khi token vẫn còn.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import MutableMapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from telegram import InlineKeyboardMarkup

from televip.cache import ratelimit
from televip.core.clock import now_utc
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.services import outbox
from televip.telegram.sender import Sender, mark_user_blocked

log = get_logger(__name__)

#: Tên method trong cột `outbox_messages.method` — đúng tên của Bot API để nhìn một dòng
#: trong bảng là biết nó sẽ thành lời gọi gì.
METHOD_SEND_MESSAGE: Final = "sendMessage"
METHOD_SEND_PHOTO: Final = "sendPhoto"

#: Chỗ worker treo bộ đếm lên `app.bot_data` để lệnh admin đọc được.
METRICS_KEY: Final = "outbox_metrics"

#: Cờ "người nhận này vừa chặn bot", đặt bởi hook `on_blocked` của `Sender`.
#:
#: Dùng `ContextVar` chứ không phải thuộc tính của worker vì mỗi tin chạy trong một Task
#: riêng do `asyncio.gather` tạo, và Task sao chép context của riêng nó — một biến dùng
#: chung sẽ bị 16 coroutine ghi đè lẫn nhau, gán nhầm "đã chặn bot" cho người bên cạnh.
_blocked_flag: ContextVar[bool] = ContextVar("outbox_blocked", default=False)


async def on_blocked(user_id: int) -> None:
    """Hook `Sender(on_blocked=…)` của worker: ghi `users.blocked_at` và bật cờ.

    Cần cả hai vì `Sender` trả `None` cho **hai** tình huống khác nhau — người dùng chặn
    bot (`Forbidden`) và nội dung sai vĩnh viễn (`BadRequest`). Chỉ nhìn giá trị trả về
    thì không phân biệt được, mà hai thứ đó phải vào hai cột thống kê khác nhau.
    """
    _blocked_flag.set(True)
    await mark_user_blocked(user_id)


class TokenSource(Protocol):
    """Cửa xin token. Bản thật là chính module `televip.cache.ratelimit`."""

    async def acquire(self, lane: str, tokens: int = 1, timeout: float = 30) -> bool: ...  # noqa: ASYNC109


class _TokenHeldByCaller:
    """Rate limiter rỗng cho `Sender` **do worker này dựng**.

    Token đã được xin ở vòng lặp bên dưới. Để `Sender` xin thêm một lần nữa là tiêu hai
    token cho một tin, tức biến trần 30 tin/giây thành 15 — một lỗi đắt và hoàn toàn vô
    hình.
    """

    async def acquire(self, lane: str, tokens: int = 1) -> None:
        return None


class OutboxApp(Protocol):
    """Phần duy nhất của `telegram.ext.Application` mà worker cần."""

    bot: Any
    bot_data: MutableMapping[str, Any]


@dataclass(frozen=True, slots=True)
class OutboxWorkerConfig:
    #: Lô nhỏ vì thiệt hại tối đa khi chết ngang tỉ lệ với kích thước lô
    #: (`02-broadcast.md` §2.3.3, hiệu chỉnh 29/07: 50 khi tệp dưới 50.000 user).
    batch_size: int = 50
    #: None = nhận việc của mọi lane.
    lane: str | None = None
    concurrency: int = 16
    lease_seconds: float = outbox.DEFAULT_LEASE_SECONDS
    #: Hàng đợi rỗng thì ngủ ngắn — đủ ngắn để tin trả code không ai thấy chậm, đủ dài để
    #: không biến vòng lặp thành một máy quét bảng.
    idle_sleep: float = 1.0
    #: Ngủ sau một vòng lỗi (database sập, Redis sập) để không quay vòng đốt CPU và log.
    error_sleep: float = 5.0
    reap_every_seconds: float = 60.0
    #: Phải LỚN HƠN `lease_seconds`: dọn một dòng còn trong hạn thuê là cướp việc của
    #: worker đang gửi nó, và người dùng nhận tin hai lần.
    reap_older_than_seconds: float = 300.0
    acquire_timeout: float = 30.0


@dataclass(slots=True)
class OutboxMetrics:
    """Bộ đếm của **tiến trình hiện tại**, cho lệnh admin đọc.

    Số liệu bền nằm ở chính bảng `outbox_messages`; đây chỉ là ảnh chụp từ lúc worker
    khởi động. Bài học Lỗi 3 của hệ cũ là đừng bao giờ để tiến độ sống trong biến Python.
    """

    claimed: int = 0
    sent: int = 0
    #: Người nhận đã chặn bot. **Không phải lỗi** — xem `error_rate`.
    blocked: int = 0
    failed_permanent: int = 0
    #: Số lần trả việc về hàng đợi kèm backoff.
    retried: int = 0
    #: Số lần trả việc về vì hết hạn chờ token — chưa hề gửi, không tính là một lần thử.
    rate_limit_timeouts: int = 0
    reaped: int = 0
    batches: int = 0
    loop_errors: int = 0
    last_batch_at: datetime | None = None
    started_at: datetime = field(default_factory=now_utc)

    @property
    def error_rate(self) -> float:
        """Tỉ lệ lỗi thật: `blocked` cố ý KHÔNG nằm ở tử số lẫn mẫu số."""
        total = self.sent + self.failed_permanent
        return self.failed_permanent / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "error_rate": self.error_rate}


def get_metrics(app: OutboxApp) -> OutboxMetrics | None:
    """Bộ đếm của worker đang chạy trong tiến trình này, `None` nếu chưa khởi động."""
    return app.bot_data.get(METRICS_KEY)


class OutboxWorker:
    """Một worker. Chạy nhiều bản song song là an toàn — claim dùng `SKIP LOCKED`."""

    def __init__(
        self,
        app: OutboxApp,
        *,
        config: OutboxWorkerConfig | None = None,
        sender: Any | None = None,
        limiter: TokenSource | None = None,
    ) -> None:
        self._app = app
        self._cfg = config or OutboxWorkerConfig()
        self._limiter: TokenSource = limiter if limiter is not None else ratelimit
        self._sender = (
            sender
            if sender is not None
            else Sender(
                app.bot,
                rate_limiter=_TokenHeldByCaller(),
                on_blocked=on_blocked,
            )
        )
        self.metrics = OutboxMetrics()
        app.bot_data[METRICS_KEY] = self.metrics
        self._next_reap = 0.0

    # ── Vòng lặp ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Chạy tới khi bị huỷ. Không bao giờ tự thoát vì một lỗi lẻ."""
        log.info(
            "outbox_worker_khoi_dong",
            lane=self._cfg.lane or "moi_lane",
            batch_size=self._cfg.batch_size,
            concurrency=self._cfg.concurrency,
        )
        try:
            while True:
                try:
                    await self._maybe_reap()
                    handled = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Database hoặc Redis sập. Việc đang bay nằm lại với `lease_until` và
                    # `reap_stuck()` sẽ cứu — mất một vòng lặp còn hơn mất cả worker.
                    self.metrics.loop_errors += 1
                    log.error("outbox_worker_vong_loi", detail=str(exc))
                    await asyncio.sleep(self._cfg.error_sleep)
                    continue

                if handled == 0:
                    await asyncio.sleep(self._cfg.idle_sleep)
        except asyncio.CancelledError:
            log.info("outbox_worker_dung", **self.metrics.as_dict())
            raise

    async def run_once(self) -> int:
        """Một lượt: lấy một lô, gửi hết lô đó. Trả về số tin đã xử lý."""
        async with transaction() as db:
            rows = await outbox.claim_batch(
                db,
                limit=self._cfg.batch_size,
                lane=self._cfg.lane,
                lease_seconds=self._cfg.lease_seconds,
            )

        if not rows:
            return 0

        self.metrics.claimed += len(rows)
        self.metrics.batches += 1
        self.metrics.last_batch_at = now_utc()

        sem = asyncio.Semaphore(self._cfg.concurrency)

        async def guarded(row: Any) -> None:
            async with sem:
                await self._deliver_one(row)

        await asyncio.gather(*(guarded(row) for row in rows))
        return len(rows)

    async def _maybe_reap(self) -> None:
        now = time.monotonic()
        if now < self._next_reap:
            return
        self._next_reap = now + self._cfg.reap_every_seconds
        async with transaction() as db:
            saved = await outbox.reap_stuck(
                db, older_than_seconds=self._cfg.reap_older_than_seconds
            )
        self.metrics.reaped += saved

    # ── Gửi một tin ──────────────────────────────────────────────────

    async def _deliver_one(self, row: Any) -> None:
        payload: dict[str, Any] = dict(row.payload)
        lane = outbox.lane_of(payload)

        if not await self._limiter.acquire(lane, 1, self._cfg.acquire_timeout):
            # Hết hạn chờ mà chưa tới lượt. Tin CHƯA rời tiến trình, nên đây không phải
            # một lần thử hỏng — trả về hàng đợi nguyên vẹn.
            self.metrics.rate_limit_timeouts += 1
            async with transaction() as db:
                await outbox.release(db, row.outbox_id)
            return

        try:
            call = self._build_call(row, payload, lane)
        except ValueError as exc:
            # Payload sai cấu trúc: thử lại mười lần vẫn sai đúng như vậy.
            log.error("outbox_payload_hong", outbox_id=row.outbox_id, detail=str(exc))
            await self._fail(row, str(exc), permanent=True)
            self.metrics.failed_permanent += 1
            return

        _blocked_flag.set(False)
        try:
            message_id = await call()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # `Sender` chỉ ném khi đã hết lượt thử của nó (429 kéo dài, mạng chết). Người
            # nhận không có lỗi gì: việc quay lại hàng đợi kèm backoff.
            self.metrics.retried += 1
            log.warning("outbox_gui_hoan", outbox_id=row.outbox_id, detail=str(exc))
            await self._fail(row, f"{type(exc).__name__}: {exc}", permanent=False)
            return

        if message_id is not None:
            async with transaction() as db:
                await outbox.mark_sent(db, row.outbox_id, message_id)
            self.metrics.sent += 1
            return

        if _blocked_flag.get():
            # `on_blocked` đã ghi `users.blocked_at`. Không thử lại, và KHÔNG đếm vào lỗi.
            self.metrics.blocked += 1
            await self._fail(row, "nguoi_dung_chan_bot", permanent=True)
            log.info("outbox_nguoi_dung_chan_bot", chat_id=row.chat_id)
            return

        self.metrics.failed_permanent += 1
        await self._fail(row, "loi_vinh_vien_phia_telegram", permanent=True)

    def _build_call(self, row: Any, payload: dict[str, Any], lane: str) -> Any:
        """Dựng lời gọi `Sender` từ một dòng outbox. Ném `ValueError` nếu payload sai."""
        args = outbox.message_payload(payload)
        markup = self._markup(args.get("reply_markup"))

        if row.method == METHOD_SEND_MESSAGE:
            body = args.get("text")
            if not isinstance(body, str) or not body:
                raise ValueError("thiếu 'text' cho sendMessage")

            async def send_message() -> int | None:
                return await self._sender.send_message(
                    row.chat_id,
                    body,
                    lane=lane,
                    reply_markup=markup,
                    parse_mode=args.get("parse_mode"),
                    idem_key=row.dedupe_key,
                )

            return send_message

        if row.method == METHOD_SEND_PHOTO:
            photo = args.get("photo")
            if not isinstance(photo, str) or not photo:
                # Cố ý chỉ nhận `file_id`: hệ cũ mở lại `images/2.jpg` (727 KB) cho TỪNG
                # người nhận — 13,9 GB băng thông cho một đợt 19.151 người.
                raise ValueError("thiếu 'photo' (file_id) cho sendPhoto")

            async def send_photo() -> int | None:
                return await self._sender.send_photo(
                    row.chat_id,
                    photo,
                    args.get("caption"),
                    lane=lane,
                    reply_markup=markup,
                    parse_mode=args.get("parse_mode"),
                    idem_key=row.dedupe_key,
                )

            return send_photo

        raise ValueError(f"method không hỗ trợ: {row.method!r}")

    def _markup(self, raw: Any) -> InlineKeyboardMarkup | None:
        if raw is None:
            return None
        if isinstance(raw, InlineKeyboardMarkup):
            return raw
        if isinstance(raw, dict):
            return InlineKeyboardMarkup.de_json(raw, self._app.bot)
        raise ValueError("reply_markup phải là JSON object")

    async def _fail(self, row: Any, error: str, *, permanent: bool) -> None:
        async with transaction() as db:
            await outbox.mark_failed(db, row.outbox_id, error, permanent=permanent)


async def run_outbox_worker(
    app: OutboxApp,
    *,
    config: OutboxWorkerConfig | None = None,
    sender: Any | None = None,
    limiter: TokenSource | None = None,
) -> None:
    """Điểm vào: chạy vòng lặp outbox cho tới khi task bị huỷ."""
    await OutboxWorker(app, config=config, sender=sender, limiter=limiter).run()


__all__ = [
    "METHOD_SEND_MESSAGE",
    "METHOD_SEND_PHOTO",
    "METRICS_KEY",
    "OutboxMetrics",
    "OutboxWorker",
    "OutboxWorkerConfig",
    "get_metrics",
    "on_blocked",
    "run_outbox_worker",
]
