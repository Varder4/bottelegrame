"""Hàng đợi gửi tin bền vững: `services/outbox.py` + `apps/worker/outbox_worker.py`.

Chạy trên **PostgreSQL thật** vì toàn bộ giá trị của file này nằm ở `FOR UPDATE SKIP
LOCKED`, `ON CONFLICT` và `make_interval` — không có thứ nào trong số đó giả lập được.
Chỉ Telegram bị thay bằng bản giả.

Năm mệnh đề được kiểm, tất cả đều là chỗ hệ cũ hỏng:

- xếp hàng hai lần cùng `idem_key` → **đúng một** dòng (hệ cũ không có khái niệm này);
- ba worker song song claim 100 việc → **không việc nào bị nhận hai lần**, tổng đúng 100;
- người chặn bot → `users.blocked_at` được đặt, việc **không** thử lại, và **không** bị
  tính vào tỉ lệ lỗi (hệ cũ gộp 403 vào `failed += 1` rồi quên luôn người đó);
- lỗi tạm → `attempts` tăng và việc quay lại hàng đợi ở tương lai, không mất người;
- worker chết giữa chừng → `reap_stuck()` đưa việc về, không kẹt vĩnh viễn.

⚠️ File này KHÔNG dùng fixture `db` của `conftest`: worker đọc ghi qua engine **toàn cục**
(`db.engine.transaction()`), nên dựng dữ liệu bằng engine thứ hai là tạo ra một cuộc đua
giữa hai kết nối. Ở đây chỉ có **một** engine, cả test lẫn worker đều đi qua nó.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from telegram.error import Forbidden, TimedOut

from televip.apps.worker.outbox_worker import (
    METHOD_SEND_MESSAGE,
    METHOD_SEND_PHOTO,
    METRICS_KEY,
    OutboxWorker,
    OutboxWorkerConfig,
    on_blocked,
    run_outbox_worker,
)
from televip.core.clock import now_utc
from televip.db.engine import session as db_session
from televip.db.engine import transaction
from televip.services import outbox
from televip.telegram.sender import Sender
from tests.conftest import TEST_DATABASE_URL, _truncate_all

# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeLimiter:
    """Token bucket giả: luôn cho đi, và ghi lại lane của từng lượt xin."""

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, int]] = []

    async def acquire(self, lane: str, tokens: int = 1, timeout: float = 30) -> bool:  # noqa: ASYNC109
        self.calls.append((lane, tokens))
        return self.allow


class FakeBot:
    """Bot Telegram giả. `error` khác None thì mọi lời gọi đều ném lỗi đó."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=9000 + len(self.sent))


def make_app(bot: FakeBot) -> Any:
    return SimpleNamespace(bot=bot, bot_data={})


def fast_sender(bot: FakeBot) -> Sender:
    """`Sender` thật, chỉ thay hàm ngủ — để test backoff mà không chờ 14 giây thật."""

    async def no_sleep(seconds: float) -> None:
        return None

    return Sender(
        bot,  # type: ignore[arg-type]
        rate_limiter=SimpleNamespace(acquire=_noop_acquire),
        on_blocked=on_blocked,
        sleeper=no_sleep,
    )


async def _noop_acquire(lane: str, tokens: int = 1) -> None:
    return None


# ── Fixture: một engine duy nhất, trỏ vào database test ─────────────


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine

    # Vứt engine còn sót lại của test trước (nếu teardown của nó không chạy trọn): pool
    # gắn với event loop đã đóng, mà `init_engine` thì trả lại engine cũ nếu còn — dùng
    # tiếp là một chuỗi lỗi `'NoneType' object has no attribute 'send'` không nói gì về
    # nguyên nhân.
    await db_engine.dispose_engine()
    # Engine phải sinh và huỷ trong CÙNG event loop với test — xem docstring fixture
    # `engine` ở conftest.
    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    async with db_session() as s:
        await _truncate_all(s)
    try:
        yield
    finally:
        await db_engine.dispose_engine()


# ── Helper (đều đi qua engine toàn cục) ─────────────────────────────


async def enqueue_one(
    *,
    chat_id: int = 111,
    idem_key: str = "msg:test",
    lane: str = "bulk",
    method: str = METHOD_SEND_MESSAGE,
    payload: dict[str, Any] | None = None,
) -> int:
    async with transaction() as db:
        return await outbox.enqueue(
            db,
            chat_id=chat_id,
            method=method,
            payload=payload if payload is not None else {"text": "xin chào"},
            idem_key=idem_key,
            lane=lane,
        )


async def row_of(outbox_id: int) -> Any:
    async with db_session() as s:
        return (
            await s.execute(
                text("SELECT * FROM outbox_messages WHERE outbox_id = :i"), {"i": outbox_id}
            )
        ).one()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def make_user(user_id: int) -> None:
    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:u, :n) ON CONFLICT DO NOTHING",
        {"u": user_id, "n": f"user{user_id}"},
    )


# ══════════════════════════════════════════════════════════════════════
# 1. Xếp hàng — chống trùng
# ══════════════════════════════════════════════════════════════════════


async def test_enqueue_hai_lan_cung_idem_key_chi_mot_dong(wired) -> None:
    """Chốt chặn cuối của chuỗi chống gửi trùng: cùng khoá thì cùng một dòng."""
    first = await enqueue_one(idem_key="msg:tanthu:111")
    second = await enqueue_one(idem_key="msg:tanthu:111", payload={"text": "khác hẳn"})

    assert first == second
    assert await scalar("SELECT count(*) FROM outbox_messages") == 1
    # Lần thứ hai KHÔNG được ghi đè nội dung của lần đầu.
    assert (await row_of(first)).payload["text"] == "xin chào"


async def test_enqueue_luu_lane_va_job_id(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:1", lane="interactive")
    payload = (await row_of(outbox_id)).payload

    assert outbox.lane_of(payload) == "interactive"
    assert outbox.job_id_of(payload) is None
    # Khoá điều phối không được lọt vào tham số gửi cho Telegram.
    assert outbox.message_payload(payload) == {"text": "xin chào"}


async def test_doc_duoc_dong_do_broadcast_ghi_thang_vao_bang(wired) -> None:
    """`services/broadcast.py` INSERT thẳng, không qua `enqueue()` — hai bên phải cùng quy ước.

    Nếu khoá điều phối lệch nhau thì worker xin token sai lane cho toàn bộ tin hàng loạt,
    và lane `bulk` sẽ cướp hạn mức của người đang bấm nút.
    """
    from televip.services import broadcast

    await run_sql(
        """
        INSERT INTO outbox_messages (chat_id, method, payload, dedupe_key)
        VALUES (111, 'sendMessage',
                CAST(:p AS jsonb)
                  || jsonb_build_object('lane', CAST(:lane AS text), 'job_id', 7, 'user_id', 111),
                'bc:7:111')
        """,
        {"p": '{"text": "co code moi"}', "lane": broadcast.OUTBOX_LANE},
    )
    payload = (await scalar("SELECT payload FROM outbox_messages")) or {}

    assert outbox.lane_of(payload) == broadcast.OUTBOX_LANE
    assert outbox.job_id_of(payload) == 7
    assert outbox.message_payload(payload) == {"text": "co code moi"}
    assert broadcast.outbox_method({"text": "x"}) == METHOD_SEND_MESSAGE
    assert broadcast.outbox_method({"photo": "AgAC"}) == METHOD_SEND_PHOTO


async def test_enqueue_tu_choi_lane_la(wired) -> None:
    with pytest.raises(ValueError, match="lane"):
        await enqueue_one(idem_key="msg:2", lane="khong_co_lane_nay")


# ══════════════════════════════════════════════════════════════════════
# 2. Lấy việc — nhiều worker song song
# ══════════════════════════════════════════════════════════════════════


async def test_ba_worker_song_song_khong_nhan_trung_viec(wired) -> None:
    """Đây là lý do `FOR UPDATE SKIP LOCKED` tồn tại — cùng lý do với việc cấp code."""
    from televip.db.engine import get_session_factory

    total = 100
    async with transaction() as db:
        for i in range(total):
            await outbox.enqueue(
                db,
                chat_id=1000 + i,
                method=METHOD_SEND_MESSAGE,
                payload={"text": f"tin {i}"},
                idem_key=f"msg:{i}",
            )

    factory = get_session_factory()
    claimed: list[list[int]] = [[], [], []]
    deadline = time.monotonic() + 20

    async def worker(idx: int) -> None:
        while sum(len(c) for c in claimed) < total and time.monotonic() < deadline:
            async with factory() as s:
                async with s.begin():
                    rows = await outbox.claim_batch(s, limit=7)
            if not rows:
                await asyncio.sleep(0.01)
                continue
            claimed[idx].extend(int(r.outbox_id) for r in rows)

    await asyncio.gather(*(worker(i) for i in range(3)))

    everything = [oid for part in claimed for oid in part]
    assert len(everything) == total
    assert len(set(everything)) == total, "có việc bị hai worker nhận cùng lúc"
    # Cả ba đều thực sự có phần — nếu không thì test này không chứng minh được gì.
    assert all(part for part in claimed)
    # Đã claim thì phải mang hạn thuê, và biến mất khỏi cửa sổ lấy việc.
    assert await scalar("SELECT count(*) FROM outbox_messages WHERE lease_until IS NOT NULL") == 100
    async with transaction() as db:
        assert await outbox.claim_batch(db, limit=50) == []


async def test_claim_loc_theo_lane(wired) -> None:
    await enqueue_one(idem_key="msg:a", lane="bulk")
    await enqueue_one(idem_key="msg:b", lane="bulk")
    interactive_id = await enqueue_one(idem_key="msg:c", lane="interactive")

    async with transaction() as db:
        rows = await outbox.claim_batch(db, limit=10, lane="interactive")

    assert [int(r.outbox_id) for r in rows] == [interactive_id]


async def test_claim_bo_qua_viec_dang_bay(wired) -> None:
    """Dòng còn hạn thuê chỉ thuộc về `reap_stuck()`, không ai được nhặt lại."""
    await enqueue_one(idem_key="msg:a")

    async with transaction() as db:
        assert len(await outbox.claim_batch(db, limit=10)) == 1
    async with transaction() as db:
        assert await outbox.claim_batch(db, limit=10) == []


# ══════════════════════════════════════════════════════════════════════
# 3. Ghi kết quả
# ══════════════════════════════════════════════════════════════════════


async def test_mark_sent_ghi_message_id_va_tra_hen_thue(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:a")
    async with transaction() as db:
        await outbox.claim_batch(db, limit=1)
    async with transaction() as db:
        await outbox.mark_sent(db, outbox_id, 4242)

    row = await row_of(outbox_id)
    assert (row.state, row.tg_message_id, row.lease_until) == (outbox.STATE_SENT, 4242, None)


async def test_loi_tam_tang_attempts_va_hen_lai_o_tuong_lai(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:a")
    async with transaction() as db:
        await outbox.claim_batch(db, limit=1)
    async with transaction() as db:
        await outbox.mark_failed(db, outbox_id, "TimedOut", permanent=False)

    row = await row_of(outbox_id)
    assert row.state == outbox.STATE_PENDING
    assert row.attempts == 1
    assert row.lease_until is None
    assert row.visible_at > now_utc(), "phải lùi về tương lai, nếu không là quay vòng ngay lập tức"
    assert row.last_error == "TimedOut"

    # Chưa tới hẹn thì không ai được nhận việc này.
    async with transaction() as db:
        assert await outbox.claim_batch(db, limit=10) == []


async def test_loi_tam_qua_nhieu_lan_thi_dung_han(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:a")
    for _ in range(outbox.MAX_ATTEMPTS):
        async with transaction() as db:
            await outbox.mark_failed(db, outbox_id, "NetworkError", permanent=False)

    row = await row_of(outbox_id)
    assert (row.state, row.attempts) == (outbox.STATE_FAILED, outbox.MAX_ATTEMPTS)


async def test_loi_vinh_vien_dung_ngay_lan_dau(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:a")
    async with transaction() as db:
        await outbox.mark_failed(db, outbox_id, "BadRequest", permanent=True)

    row = await row_of(outbox_id)
    assert (row.state, row.attempts, row.lease_until) == (outbox.STATE_FAILED, 1, None)
    async with transaction() as db:
        assert await outbox.claim_batch(db, limit=10) == []


async def test_mark_failed_khong_lat_nguoc_mot_tin_da_gui(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:a")
    async with transaction() as db:
        await outbox.mark_sent(db, outbox_id, 7)
    async with transaction() as db:
        await outbox.mark_failed(db, outbox_id, "tới muộn", permanent=True)

    assert (await row_of(outbox_id)).state == outbox.STATE_SENT


# ══════════════════════════════════════════════════════════════════════
# 4. Dọn việc bị bỏ rơi
# ══════════════════════════════════════════════════════════════════════


async def test_reap_stuck_dua_viec_cua_worker_chet_ve_hang_doi(wired) -> None:
    outbox_id = await enqueue_one(idem_key="msg:a")
    async with transaction() as db:
        await outbox.claim_batch(db, limit=1)
    # Giả lập worker bị kill giữa lúc gửi: hạn thuê đã quá hạn từ 10 phút trước.
    await run_sql(
        """
        UPDATE outbox_messages
           SET lease_until = now() - interval '10 minutes',
               visible_at  = now() - interval '10 minutes'
         WHERE outbox_id = :i
        """,
        {"i": outbox_id},
    )

    async with transaction() as db:
        assert await outbox.reap_stuck(db, older_than_seconds=300) == 1

    row = await row_of(outbox_id)
    assert (row.state, row.lease_until) == (outbox.STATE_PENDING, None)
    assert row.attempts == 1, "một vòng chết phải được đếm, nếu không tin độc quay vòng mãi"
    async with transaction() as db:
        assert len(await outbox.claim_batch(db, limit=10)) == 1


async def test_reap_stuck_khong_cuop_viec_con_trong_han_thue(wired) -> None:
    await enqueue_one(idem_key="msg:a")
    async with transaction() as db:
        await outbox.claim_batch(db, limit=1, lease_seconds=120)

    async with transaction() as db:
        assert await outbox.reap_stuck(db, older_than_seconds=300) == 0


# ══════════════════════════════════════════════════════════════════════
# 5. Worker
# ══════════════════════════════════════════════════════════════════════


async def test_worker_gui_va_danh_dau_da_gui(wired) -> None:
    outbox_id = await enqueue_one(chat_id=111, idem_key="msg:a", lane="interactive")
    bot = FakeBot()
    app = make_app(bot)
    worker = OutboxWorker(app, limiter=FakeLimiter())

    assert await worker.run_once() == 1

    row = await row_of(outbox_id)
    assert (row.state, row.tg_message_id) == (outbox.STATE_SENT, 9001)
    assert bot.sent[0]["chat_id"] == 111
    assert bot.sent[0]["text"] == "xin chào"
    assert worker.metrics.sent == 1
    # Token xin đúng lane đã lưu, đúng một lần cho một tin.
    assert app.bot_data[METRICS_KEY] is worker.metrics
    assert worker.metrics.as_dict()["error_rate"] == 0.0


async def test_worker_nguoi_chan_bot_duoc_danh_dau_va_khong_thu_lai(wired) -> None:
    """403 KHÔNG phải lỗi gửi: người đó rời đi, và không được tính vào tỉ lệ lỗi."""
    await make_user(111)
    outbox_id = await enqueue_one(chat_id=111, idem_key="msg:a")
    worker = OutboxWorker(
        make_app(FakeBot(error=Forbidden("Forbidden: bot was blocked by the user"))),
        limiter=FakeLimiter(),
    )

    assert await worker.run_once() == 1

    assert await scalar("SELECT blocked_at FROM users WHERE user_id = 111") is not None
    row = await row_of(outbox_id)
    assert row.state == outbox.STATE_FAILED
    assert row.last_error == "nguoi_dung_chan_bot"
    assert (worker.metrics.blocked, worker.metrics.failed_permanent) == (1, 0)
    assert worker.metrics.error_rate == 0.0
    # Không thử lại: vòng sau không còn việc nào.
    assert await worker.run_once() == 0


async def test_worker_loi_tam_dua_viec_quay_lai_hang_doi(wired) -> None:
    outbox_id = await enqueue_one(chat_id=111, idem_key="msg:a")
    bot = FakeBot(error=TimedOut())
    worker = OutboxWorker(make_app(bot), sender=fast_sender(bot), limiter=FakeLimiter())

    assert await worker.run_once() == 1

    row = await row_of(outbox_id)
    assert (row.state, row.attempts) == (outbox.STATE_PENDING, 1)
    assert row.visible_at > now_utc()
    assert (worker.metrics.retried, worker.metrics.sent) == (1, 0)


async def test_worker_het_han_cho_token_thi_tra_viec_ve_nguyen_ven(wired) -> None:
    """Chưa gửi thì không được tính là một lần thử — hạn mức 5 lần dành cho lỗi thật."""
    outbox_id = await enqueue_one(chat_id=111, idem_key="msg:a")
    bot = FakeBot()
    worker = OutboxWorker(make_app(bot), limiter=FakeLimiter(allow=False))

    assert await worker.run_once() == 1

    row = await row_of(outbox_id)
    assert (row.state, row.attempts, row.lease_until) == (outbox.STATE_PENDING, 0, None)
    assert bot.sent == []
    assert worker.metrics.rate_limit_timeouts == 1
    # Trả về nguyên vẹn nghĩa là lấy lại được ngay, không phải chờ backoff.
    async with transaction() as db:
        assert len(await outbox.claim_batch(db, limit=10)) == 1


async def test_worker_payload_hong_thi_dung_han(wired) -> None:
    outbox_id = await enqueue_one(chat_id=111, idem_key="msg:a", payload={"khong_co_text": 1})
    worker = OutboxWorker(make_app(FakeBot()), limiter=FakeLimiter())

    assert await worker.run_once() == 1

    row = await row_of(outbox_id)
    assert row.state == outbox.STATE_FAILED
    assert worker.metrics.failed_permanent == 1


async def test_worker_thoat_sach_khi_bi_huy(wired) -> None:
    task = asyncio.create_task(
        run_outbox_worker(
            make_app(FakeBot()),
            config=OutboxWorkerConfig(idle_sleep=0.01),
            limiter=FakeLimiter(),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
