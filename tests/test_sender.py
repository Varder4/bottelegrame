"""Test cho `televip.telegram.sender`.

Không chạm DB, không chạm Redis: `Sender` nhận bot giả, rate limiter giả và một hàm
ngủ giả, nên toàn bộ file chạy trong vài mili-giây.

Ba mệnh đề được kiểm ở đây đúng là ba lỗi đã tốn tiền thật trên hệ cũ:
`RetryAfter` phải được thử lại, `Forbidden` không được thử lại, và `answer_callback`
không được làm đổ luồng chính.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from televip.telegram.sender import Sender, SenderStats


class FakeLimiter:
    """Ghi lại mọi lần xin token và mọi lần bị phạt 429."""

    def __init__(self) -> None:
        self.acquired: list[tuple[str, int]] = []
        self.penalties: list[float] = []

    async def acquire(self, lane: str, tokens: int = 1) -> None:
        self.acquired.append((lane, tokens))

    async def penalize(self, retry_after: float) -> None:
        self.penalties.append(retry_after)


class LimiterWithoutPenalize:
    """Rate limiter chỉ có `acquire` — `penalize` là phần tuỳ chọn của giao diện."""

    def __init__(self) -> None:
        self.acquired: list[tuple[str, int]] = []

    async def acquire(self, lane: str, tokens: int = 1) -> None:
        self.acquired.append((lane, tokens))


def make_message(message_id: int = 4242) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id)


def make_sender(
    *,
    send_message: Any = None,
    send_photo: Any = None,
    limiter: Any = None,
    on_blocked: Any = None,
    **kwargs: Any,
) -> tuple[Sender, Any, Any, list[float]]:
    """Dựng `Sender` với bot giả. Trả về (sender, bot, limiter, danh sách giây đã ngủ)."""
    bot = AsyncMock()
    bot.send_message = send_message or AsyncMock(return_value=make_message())
    bot.send_photo = send_photo or AsyncMock(return_value=make_message(777))
    limiter = limiter if limiter is not None else FakeLimiter()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    sender = Sender(
        bot,
        rate_limiter=limiter,
        on_blocked=on_blocked or AsyncMock(),
        sleeper=fake_sleep,
        **kwargs,
    )
    return sender, bot, limiter, slept


# ── Đường thành công ─────────────────────────────────────────────────


async def test_send_message_returns_message_id() -> None:
    sender, bot, limiter, _ = make_sender()

    assert await sender.send_message(111, "xin chào") == 4242

    assert bot.send_message.await_count == 1
    assert sender.stats.sent == 1
    # Xin token TRƯỚC khi gọi API, đúng một lần cho một tin.
    assert limiter.acquired == [("interactive", 1)]


async def test_send_photo_uses_bulk_lane() -> None:
    sender, bot, limiter, _ = make_sender()

    assert await sender.send_photo(111, "AgACAgUxyz", "chú thích", lane="bulk") == 777

    assert limiter.acquired == [("bulk", 1)]
    assert bot.send_photo.await_args.kwargs["photo"] == "AgACAgUxyz"


# ── RetryAfter: thử lại rồi thành công ───────────────────────────────


async def test_retry_after_is_retried_then_succeeds() -> None:
    """Đây là lỗi đắt nhất của hệ cũ: 429 rơi vào `except Exception` rồi mất người."""
    send = AsyncMock(side_effect=[RetryAfter(5), RetryAfter(3), make_message(99)])
    sender, bot, limiter, slept = make_sender(send_message=send)

    assert await sender.send_message(111, "có code mới") == 99

    assert bot.send_message.await_count == 3
    assert sender.stats.sent == 1
    assert sender.stats.retry_after_hits == 2
    assert sender.stats.retry_after_seconds == 8.0
    assert sender.stats.given_up == 0
    # Chờ đúng số giây Telegram bảo, cộng 1 giây biên an toàn.
    assert slept == [6.0, 4.0]
    # Mỗi lần thử lại đều phải xin token lại — token cũ đã bị tiêu.
    assert len(limiter.acquired) == 3
    # 429 phải được báo ra ngoài để cooldown là TOÀN CỤC, không chỉ tiến trình này.
    assert limiter.penalties == [5.0, 3.0]


async def test_retry_after_exhausted_raises_instead_of_dropping_recipient() -> None:
    """Hết lượt thử thì NÉM, không trả `None`: người này chưa hỏng, chỉ chưa gửi được."""
    send = AsyncMock(side_effect=RetryAfter(2))
    sender, bot, _, _ = make_sender(send_message=send, max_retry_after_attempts=2)

    with pytest.raises(RetryAfter):
        await sender.send_message(111, "có code mới")

    assert bot.send_message.await_count == 3  # 1 lần đầu + 2 lần thử lại
    assert sender.stats.sent == 0
    assert sender.stats.given_up == 1
    assert sender.stats.blocked == 0
    assert sender.stats.failed_permanent == 0


async def test_retry_after_too_long_gives_back_to_queue() -> None:
    send = AsyncMock(side_effect=RetryAfter(600))
    sender, bot, _, slept = make_sender(send_message=send, max_retry_after_wait=300.0)

    with pytest.raises(RetryAfter):
        await sender.send_message(111, "có code mới")

    assert bot.send_message.await_count == 1
    assert slept == []  # không giữ coroutine ngủ 10 phút


async def test_penalize_is_optional() -> None:
    send = AsyncMock(side_effect=[RetryAfter(1), make_message(5)])
    sender, _, _, _ = make_sender(send_message=send, limiter=LimiterWithoutPenalize())

    assert await sender.send_message(111, "x") == 5


# ── Forbidden: người dùng đã chặn bot ────────────────────────────────


async def test_forbidden_returns_none_without_retry() -> None:
    send = AsyncMock(side_effect=Forbidden("Forbidden: bot was blocked by the user"))
    on_blocked = AsyncMock()
    sender, bot, _, _ = make_sender(send_message=send, on_blocked=on_blocked)

    assert await sender.send_message(111, "có code mới") is None

    assert bot.send_message.await_count == 1  # KHÔNG thử lại
    assert sender.stats.blocked == 1
    assert sender.stats.failed_permanent == 0  # KHÔNG tính là lỗi
    on_blocked.assert_awaited_once_with(111)


async def test_forbidden_in_group_does_not_touch_users_table() -> None:
    send = AsyncMock(side_effect=Forbidden("Forbidden: bot was kicked from the group chat"))
    on_blocked = AsyncMock()
    sender, _, _, _ = make_sender(send_message=send, on_blocked=on_blocked)

    assert await sender.send_message(-100123, "báo cáo") is None
    on_blocked.assert_not_awaited()


async def test_blocked_write_failure_does_not_break_send() -> None:
    send = AsyncMock(side_effect=Forbidden("Forbidden: user is deactivated"))
    on_blocked = AsyncMock(side_effect=RuntimeError("DB đang bận"))
    sender, _, _, _ = make_sender(send_message=send, on_blocked=on_blocked)

    assert await sender.send_message(111, "x") is None
    assert sender.stats.blocked == 1


# ── BadRequest: lỗi vĩnh viễn ────────────────────────────────────────


async def test_bad_request_is_permanent() -> None:
    """`BadRequest` là lớp con của `NetworkError` trong PTB — không được thử lại nhầm."""
    send = AsyncMock(side_effect=BadRequest("Chat not found"))
    on_blocked = AsyncMock()
    sender, bot, _, slept = make_sender(send_message=send, on_blocked=on_blocked)

    assert await sender.send_message(111, "x") is None

    assert bot.send_message.await_count == 1
    assert slept == []
    assert sender.stats.failed_permanent == 1
    assert sender.stats.network_retries == 0
    on_blocked.assert_not_awaited()


# ── Lỗi mạng: thử lại có backoff ─────────────────────────────────────


async def test_network_error_retries_with_backoff() -> None:
    send = AsyncMock(side_effect=[TimedOut(), NetworkError("mất kết nối"), make_message(8)])
    sender, bot, _, slept = make_sender(send_message=send)

    assert await sender.send_message(111, "x") == 8

    assert bot.send_message.await_count == 3
    assert slept == [2.0, 4.0]  # 2^1, 2^2
    assert sender.stats.network_retries == 2


async def test_network_error_exhausted_raises() -> None:
    send = AsyncMock(side_effect=TimedOut())
    sender, bot, _, _ = make_sender(send_message=send, max_network_attempts=2)

    with pytest.raises(NetworkError):
        await sender.send_message(111, "x")

    assert bot.send_message.await_count == 3
    assert sender.stats.given_up == 1


async def test_network_backoff_is_capped() -> None:
    send = AsyncMock(side_effect=NetworkError("mất kết nối"))
    sender, _, _, slept = make_sender(send_message=send, max_network_attempts=5, backoff_cap=8.0)

    with pytest.raises(NetworkError):
        await sender.send_message(111, "x")

    assert slept == [2.0, 4.0, 8.0, 8.0, 8.0]


# ── answer_callback: luôn nuốt lỗi ───────────────────────────────────


async def test_answer_callback_swallows_errors() -> None:
    sender, _, limiter, _ = make_sender()
    query = AsyncMock()
    query.answer = AsyncMock(side_effect=BadRequest("Query is too old"))

    await sender.answer_callback(query, "Hết code rồi")  # không được ném ra ngoài

    assert sender.stats.callback_errors == 1
    assert sender.stats.callbacks_answered == 0
    # Không xin token: answerCallbackQuery không tính vào hạn mức tin nhắn.
    assert limiter.acquired == []


async def test_answer_callback_swallows_even_non_telegram_errors() -> None:
    sender, _, _, _ = make_sender()
    query = AsyncMock()
    query.answer = AsyncMock(side_effect=RuntimeError("event loop đang đóng"))

    await sender.answer_callback(query, "x", show_alert=True)
    assert sender.stats.callback_errors == 1


async def test_answer_callback_success_is_counted() -> None:
    sender, _, _, _ = make_sender()
    query = AsyncMock()

    await sender.answer_callback(query)

    assert sender.stats.callbacks_answered == 1
    query.answer.assert_awaited_once_with(text=None, show_alert=False)


# ── Số liệu cho lệnh admin ───────────────────────────────────────────


async def test_stats_are_readable_as_dict() -> None:
    sender, _, _, _ = make_sender()
    await sender.send_message(111, "x")

    data = sender.stats.as_dict()
    assert data["sent"] == 1
    assert set(SenderStats().as_dict()) == set(data)
