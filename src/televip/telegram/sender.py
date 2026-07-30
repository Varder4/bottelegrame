"""Lớp gửi tin — **CỬA DUY NHẤT của toàn dự án được phép gọi Telegram Bot API**.

Không một file nào khác được gọi `bot.send_message`, `bot.send_photo` hay
`query.answer`. Nếu bạn đang định thêm một lời gọi Bot API ở chỗ khác: đừng — thêm
phương thức vào đây.

Vì sao luật này tồn tại (đo được trên hệ cũ, `02-broadcast.md` §2.1):

- Hệ cũ rải `bot.send_message` khắp `main.py` và `handlers/admin.py`, nên **không có
  một chỗ nào đặt được rate limit**. Token bucket dùng chung chỉ có nghĩa khi mọi tin
  đều đi qua cùng một cửa.
- Không đếm được: admin chỉ nhìn thấy `❌ Lỗi: 2.631`, không biết 2.631 đó là ai, vì
  cái gì, có gửi lại được không.
- Không retry được: `telegram.error.RetryAfter` rơi vào `except Exception` chung rồi
  cộng `failed += 1` (`admin.py:524`). Mỗi đợt phát mất **5-20% người nhận**, tức
  960-3.830 người trên 19.151 đích — mất vĩnh viễn, không có danh sách để gửi bù.

## Hợp đồng trả về của `send_message` / `send_photo`

| Kết quả | Nghĩa | Việc của caller |
|---|---|---|
| `int` | đã gửi, đây là `message_id` | `state=1`, lưu `tg_message_id` |
| `None` | người nhận này **vĩnh viễn không gửi được** (chặn bot, tài khoản xoá, chat không tồn tại, nội dung sai) | `state=2`/`state=3`, **không** thử lại |
| ném `RetryAfter` / `NetworkError` | **chưa** gửi được nhưng **không phải lỗi của người nhận** | trả target về `state=0` để gửi lại — với `RetryAfter` thì **không tăng `attempts`** |

Sự phân biệt cột 2 và cột 3 chính là chỗ hệ cũ làm sai: nó gộp cả ba vào `failed += 1`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import update
from telegram import Bot, CallbackQuery, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from televip.core.clock import now_utc
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.db.models.identity import User

log = get_logger(__name__)

#: Ba lane của token bucket toàn cục (`02-broadcast.md` §2.3.4). Chúng KHÔNG có xô
#: riêng — chỉ khác nhau ở lượng token phải chừa lại cho lane ưu tiên hơn.
Lane = Literal["interactive", "bulk", "janitor"]

#: Callback đánh dấu người dùng đã chặn bot. Tách ra khỏi `Sender` để test không cần DB.
BlockedCallback = Callable[[int], Awaitable[None]]

#: Hàm ngủ. Tách ra để test đo được nhịp retry mà không phải chờ thật.
Sleeper = Callable[[float], Awaitable[None]]


class RateLimiter(Protocol):
    """Cửa xin token trước mỗi lời gọi Bot API.

    Bản thật nằm ở `televip.cache.ratelimit` (khối 1): token bucket Lua trên Redis,
    dùng chung cho **mọi tiến trình** — hạn mức 30 tin/giây của Telegram tính theo bot
    token chứ không theo tiến trình, nên một bộ đếm trong RAM là vô nghĩa.
    """

    async def acquire(self, lane: str, tokens: int = 1) -> None:
        """Chặn cho tới khi được phép gửi. Không trả gì; hết chờ là đi."""
        ...


class _UnlimitedRateLimiter:
    """Bản dự phòng khi `televip.cache.ratelimit` chưa tồn tại.

    Cố ý **không** giới hạn gì: một bản giả tự chế sẽ che mất việc khối 1 chưa được
    nối vào, và tệ hơn là ru ngủ bằng cảm giác an toàn sai. Log cảnh báo lúc khởi tạo
    là thứ duy nhất bản này làm.
    """

    async def acquire(self, lane: str, tokens: int = 1) -> None:
        return None


def _default_rate_limiter() -> RateLimiter:
    try:
        from televip.cache import ratelimit
    except ImportError:
        log.warning(
            "ratelimit_module_missing",
            detail="televip.cache.ratelimit chưa có — Sender chạy KHÔNG có rate limit",
        )
        return _UnlimitedRateLimiter()
    return ratelimit  # type: ignore[return-value]


async def mark_user_blocked(user_id: int) -> None:
    """Ghi `users.blocked_at` — mặc định của `Sender(on_blocked=...)`.

    `WHERE blocked_at IS NULL` giữ lại **lần chặn đầu tiên**: đó là mốc trả lời được
    câu "người này rời đi sau đợt nào", và nó cũng làm câu lệnh không ghi lại hàng đã
    đúng. Hệ cũ bắt `Forbidden` rồi vứt đi, nên đợt sau vẫn bắn vào đúng người đó —
    tín hiệu xấu nhất có thể gửi cho hệ chống spam của Telegram.
    """
    async with transaction() as db:
        await db.execute(
            update(User)
            .where(User.user_id == user_id, User.blocked_at.is_(None))
            .values(blocked_at=now_utc())
        )


@dataclass(slots=True)
class SenderStats:
    """Bộ đếm cho lệnh admin đọc. Đây là số liệu của **tiến trình hiện tại**.

    Số liệu bền của một đợt broadcast nằm ở bảng `broadcast_targets`, không ở đây —
    bài học Lỗi 3 của hệ cũ là đừng bao giờ để tiến độ sống trong biến Python.
    """

    sent: int = 0
    #: Trả về `None` vì người dùng đã chặn bot / xoá tài khoản. KHÔNG phải lỗi.
    blocked: int = 0
    #: Trả về `None` vì lỗi vĩnh viễn phía nội dung hoặc chat (`BadRequest`).
    failed_permanent: int = 0
    retry_after_hits: int = 0
    retry_after_seconds: float = 0.0
    last_retry_after_at: datetime | None = None
    network_retries: int = 0
    #: Số lần hết lượt thử và ném lỗi ra cho caller xếp hàng lại. KHÔNG phải mất người.
    given_up: int = 0
    callbacks_answered: int = 0
    callback_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Sender:
    """Một `Sender` cho mỗi tiến trình, dùng chung cho cả ba lane."""

    def __init__(
        self,
        bot: Bot,
        *,
        rate_limiter: RateLimiter | None = None,
        on_blocked: BlockedCallback | None = None,
        max_retry_after_attempts: int = 3,
        max_network_attempts: int = 3,
        max_retry_after_wait: float = 300.0,
        backoff_cap: float = 60.0,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._rate_limiter = rate_limiter if rate_limiter is not None else _default_rate_limiter()
        self._on_blocked = on_blocked if on_blocked is not None else mark_user_blocked
        self._max_retry_after_attempts = max_retry_after_attempts
        self._max_network_attempts = max_network_attempts
        self._max_retry_after_wait = max_retry_after_wait
        self._backoff_cap = backoff_cap
        self._sleep = sleeper
        self._stats = SenderStats()

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def stats(self) -> SenderStats:
        return self._stats

    # ── API công khai ────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        lane: Lane = "interactive",
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
        idem_key: str | None = None,
    ) -> int | None:
        """Gửi một tin. Xem bảng hợp đồng trả về ở đầu file."""

        async def call() -> Message:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )

        return await self._deliver(
            call, chat_id=chat_id, lane=lane, op="send_message", idem_key=idem_key
        )

    async def send_photo(
        self,
        chat_id: int,
        photo: str | bytes,
        caption: str | None = None,
        *,
        lane: Lane = "interactive",
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
        idem_key: str | None = None,
    ) -> int | None:
        """Gửi ảnh. `photo` **nên là `file_id`**, không phải bytes.

        Hệ cũ mở và upload lại `images/2.jpg` (727.592 byte) cho **từng** người nhận:
        13,9 GB băng thông cho một đợt 19.151 người (`02-broadcast.md` Lỗi 5). Với
        `file_id` thì Telegram chỉ copy tham chiếu và tổng băng thông là 0.
        """

        async def call() -> Message:
            return await self._bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )

        return await self._deliver(
            call, chat_id=chat_id, lane=lane, op="send_photo", idem_key=idem_key
        )

    async def send_document(
        self,
        chat_id: int,
        document: bytes,
        *,
        filename: str,
        caption: str | None = None,
        lane: Lane = "interactive",
        idem_key: str | None = None,
    ) -> int | None:
        """Gửi một tệp dựng trong bộ nhớ (báo cáo CSV).

        Khác `send_photo`, ở đây `bytes` là **đúng**: tệp báo cáo sinh ra cho một lần gõ
        lệnh và không có `file_id` nào để dùng lại. Cũng vì vậy nó chỉ dành cho lane
        `interactive` — không có đường nào để một tệp dựng tại chỗ đi vào đợt bắn hàng
        loạt, và đó là điều nên giữ nguyên.
        """

        async def call() -> Message:
            return await self._bot.send_document(
                chat_id=chat_id,
                document=document,
                filename=filename,
                caption=caption,
            )

        return await self._deliver(
            call, chat_id=chat_id, lane=lane, op="send_document", idem_key=idem_key
        )

    async def answer_callback(
        self,
        query: CallbackQuery,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        """Trả lời nút bấm — **nuốt mọi lỗi, không bao giờ ném ra ngoài**.

        Hai lý do, cả hai đều là chủ ý:

        1. Đặc tả buộc answer trong **2 giây**; quá hạn thì Telegram tự huỷ query và
           lỗi phát sinh ở đây không nói lên điều gì về luồng nghiệp vụ đang chạy.
        2. Đây là bước trang trí. Nếu nó ném lỗi thì cả luồng cấp code phía sau đổ
           theo — hỏng một thứ thật vì một thứ không thật.

        Cũng **không xin token**: `answerCallbackQuery` không tính vào hạn mức tin
        nhắn (`02-broadcast.md` §2.2), và bắt nó xếp hàng sau broadcast là cách chắc
        chắn nhất để vượt mốc 2 giây.
        """
        try:
            await query.answer(text=text or None, show_alert=show_alert)
        except Exception as exc:
            self._stats.callback_errors += 1
            log.debug("answer_callback_failed", detail=str(exc))
        else:
            self._stats.callbacks_answered += 1

    # ── Ruột ─────────────────────────────────────────────────────────

    async def _deliver(
        self,
        call: Callable[[], Awaitable[Message]],
        *,
        chat_id: int,
        lane: Lane,
        op: str,
        idem_key: str | None,
    ) -> int | None:
        # `idem_key` đi vào log chứ không vào một bộ nhớ đệm trong RAM: chống gửi trùng
        # là việc của hai lớp trong DB (`02-broadcast.md` §2.3.7 — PRIMARY KEY
        # (job_id, user_id) và UNIQUE idem_key). Ở đây nó là sợi chỉ để lần ra một tin
        # cụ thể trong log khi có khiếu nại "tôi nhận hai lần".
        bound = log.bind(op=op, chat_id=chat_id, lane=lane, idem_key=idem_key)
        retry_after_used = 0
        network_used = 0

        while True:
            await self._rate_limiter.acquire(lane=lane, tokens=1)
            try:
                message = await call()
            except RetryAfter as exc:
                wait = float(exc.retry_after)
                retry_after_used += 1
                self._stats.retry_after_hits += 1
                self._stats.retry_after_seconds += wait
                self._stats.last_retry_after_at = now_utc()
                await self._penalize(wait)

                if retry_after_used > self._max_retry_after_attempts:
                    self._stats.given_up += 1
                    bound.warning("send_retry_after_exhausted", wait=wait, tries=retry_after_used)
                    raise
                if wait > self._max_retry_after_wait:
                    # Telegram phạt hàng phút: giữ coroutine ngủ lâu như vậy là chiếm
                    # một chỗ trong pool 48 coroutine mà không làm gì. Trả về hàng đợi
                    # thì người này vẫn được gửi, chỉ là ở lượt claim sau.
                    self._stats.given_up += 1
                    bound.warning("send_retry_after_too_long", wait=wait)
                    raise
                bound.info("send_retry_after", wait=wait, tries=retry_after_used)
                # +1 giây biên an toàn, khớp với TTL của khoá `rl:cooldown` (§2.3.6).
                await self._sleep(wait + 1.0)
                continue
            except Forbidden as exc:
                self._stats.blocked += 1
                bound.info("send_forbidden", detail=str(exc))
                await self._mark_blocked(chat_id)
                return None
            except BadRequest as exc:
                # PHẢI đứng trước `NetworkError`: trong PTB 21, `BadRequest` là lớp CON
                # của `NetworkError`. Đảo thứ tự thì mọi lỗi nội dung vĩnh viễn sẽ được
                # thử lại 3 lần — 3 lần sai giống hệt nhau.
                self._stats.failed_permanent += 1
                bound.warning("send_bad_request", detail=str(exc))
                return None
            except (TimedOut, NetworkError) as exc:
                network_used += 1
                self._stats.network_retries += 1
                if network_used > self._max_network_attempts:
                    self._stats.given_up += 1
                    bound.warning("send_network_exhausted", detail=str(exc), tries=network_used)
                    raise
                delay = min(self._backoff_cap, float(2**network_used))
                bound.info("send_network_retry", detail=str(exc), delay=delay)
                await self._sleep(delay)
                continue
            else:
                self._stats.sent += 1
                return message.message_id

    async def _penalize(self, retry_after: float) -> None:
        """Báo 429 cho rate limiter: cooldown toàn cục + hạ ngân sách theo AIMD.

        Bắt buộc phải là **toàn cục** chứ không phải "tiến trình này ngủ": nếu chỉ
        tiến trình gặp lỗi dừng lại thì các tiến trình còn lại vẫn bắn tiếp và
        Telegram phạt nặng hơn (`02-broadcast.md` §2.3.6, điểm 1).

        `penalize` là phần **tuỳ chọn** của giao diện rate limiter — thiếu thì bỏ qua,
        vì chờ `e.retry_after` giây tại chỗ đã là hành vi đúng tối thiểu.
        """
        penalize = getattr(self._rate_limiter, "penalize", None)
        if penalize is None:
            return
        try:
            await penalize(retry_after)
        except Exception as exc:
            log.warning("ratelimit_penalize_failed", detail=str(exc))

    async def _mark_blocked(self, chat_id: int) -> None:
        if chat_id <= 0:
            # chat_id âm là nhóm/kênh: bot bị kick chứ không phải user chặn, và không
            # có hàng nào trong `users` để đánh dấu.
            return
        try:
            await self._on_blocked(chat_id)
        except Exception as exc:
            # Không để việc ghi sổ làm hỏng kết quả gửi: người này dù sao cũng đã chặn
            # bot, mất một lần ghi `blocked_at` thì đợt sau ghi lại.
            log.warning("mark_blocked_failed", chat_id=chat_id, detail=str(exc))
