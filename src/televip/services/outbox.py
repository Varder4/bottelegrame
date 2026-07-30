"""Hàng đợi gửi tin bền vững — "hộp thư đi" của cả hệ thống.

Thay vì gọi thẳng Bot API, mọi tin có thể chờ được đều **ghi thành một dòng trong
database rồi mới có worker rút ra gửi**. Ba tính chất tốt đều đến từ đúng chỗ đó
(`02-broadcast.md` §2.3):

- **Không mất tiến độ khi restart.** Toàn bộ trạng thái nằm trong PostgreSQL, không có
  một biến Python nào. Hệ cũ giữ tiến độ trong ba biến cục bộ + một list trong RAM, nên
  một lần `systemd` khởi động lại là ~12.000 người nhận tin hai lần.
- **Không đốt code.** Dòng outbox được ghi **trong cùng transaction** với việc giữ chỗ
  mã (`code_issuance.reserve`). Chết ngay sau `COMMIT` thì tin vẫn còn đó để gửi; nếu
  transaction cuộn lại thì mã chưa hề rời kho.
- **Không gửi trùng.** `dedupe_key` là UNIQUE và mọi lối vào đều đi qua
  `INSERT … ON CONFLICT DO NOTHING`.

## Vòng đời một dòng

    enqueue()                     state=0  lease_until=NULL   ← chờ tới lượt
      └ claim_batch()             state=0  lease_until=now+L  ← đang bay (có hạn thuê)
          ├ mark_sent()           state=1                     ← xong
          ├ mark_failed(perm)     state=2                     ← thôi hẳn
          ├ mark_failed(tạm)      state=0  visible_at=now+backoff
          ├ release()             state=0  visible_at=now     ← chưa hề gửi, trả ngay
          └ (worker chết)         → reap_stuck() đưa về state=0, attempts+1

**Vì sao `claim_batch` bỏ qua dòng còn `lease_until`:** dòng bị bỏ rơi giữa chừng chỉ
được `reap_stuck()` đưa về hàng đợi, và **chỉ đường đó mới tăng `attempts`**. Nếu vòng
claim thường nhặt lại chúng thì một tin độc (payload làm worker chết) sẽ quay vòng mãi
mãi mà bộ đếm `attempts` không bao giờ nhúc nhích — đúng kiểu hỏng im lặng mà cả thiết
kế này sinh ra để loại bỏ.

## Ghi chú về schema (đọc trước khi sửa)

Bảng `outbox_messages` (migration `0001`) **không có cột `lane` và `job_id`**, nên hai
giá trị đó nằm trong chính `payload` dưới các khoá dành riêng ở `RESERVED_KEYS`; các
khoá còn lại là tham số Bot API đúng như `07-db.md` mô tả. Quy ước này **phải khớp với
`services/broadcast.py`** — nó ghi thẳng vào bảng bằng `jsonb_build_object('lane', …,
'job_id', …, 'user_id', …)` chứ không đi qua `enqueue()`, và hai quy ước khác nhau trên
cùng một bảng nghĩa là worker xin token sai lane cho một nửa số tin.

Tên cột thật cũng khác tên trong đặc tả một chút: `dedupe_key` (≡ idem_key),
`visible_at` (≡ next_attempt_at), `lease_until` (≡ claimed_at). Không có migration nào ở
đây — thêm cột `lane` là việc của một đợt migration riêng, và khi có nó thì chỉ ba câu
SQL trong file này phải đổi.
"""

from __future__ import annotations

import random
from typing import Any, Final

from sqlalchemy import Row, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from televip.cache.ratelimit import LANE_RESERVE
from televip.core.logging import get_logger
from televip.db.models.delivery import OutboxMessage

log = get_logger(__name__)

#: 0 chờ · 1 đã gửi · 2 hỏng vĩnh viễn. Đang bay KHÔNG phải một trạng thái riêng: nó là
#: `state = 0` cộng `lease_until` còn hạn — nhờ vậy partial index `ix_outbox_claim`
#: (`WHERE state = 0`) phục vụ được cả việc lấy việc lẫn việc dọn hạn thuê.
STATE_PENDING: Final = 0
STATE_SENT: Final = 1
STATE_FAILED: Final = 2

#: Quá số này thì thôi hẳn (`02-broadcast.md` §2.3.1: ">5 lần → state=2").
MAX_ATTEMPTS: Final = 5
#: Trần của backoff luỹ thừa. Chờ lâu hơn một phút không cứu được gì mà chỉ làm hàng đợi
#: trông như bị treo.
BACKOFF_CAP_SECONDS: Final = 60.0
#: Hạn thuê mặc định: đủ dài để một lần `RetryAfter` nặng đi qua, đủ ngắn để việc của
#: worker chết không nằm im quá lâu.
DEFAULT_LEASE_SECONDS: Final = 120.0

#: Khoá điều phối nằm chung trong `payload` (xem ghi chú schema ở đầu file). Không khoá
#: nào trùng tên với một tham số của `sendMessage`/`sendPhoto`, nên chúng không bao giờ
#: lẫn vào lời gọi Bot API.
RESERVED_KEYS: Final = frozenset({"lane", "job_id", "user_id"})

_LANES: Final = frozenset(LANE_RESERVE)


# ── Đóng gói payload ────────────────────────────────────────────────


def message_payload(stored: dict[str, Any]) -> dict[str, Any]:
    """Phần tham số Bot API của một dòng outbox, đã bỏ các khoá điều phối."""
    return {k: v for k, v in stored.items() if k not in RESERVED_KEYS}


def lane_of(stored: dict[str, Any], default: str = "bulk") -> str:
    """Lane của một dòng. Giá trị lạ rơi về `default` thay vì làm hỏng cả lô."""
    lane = stored.get("lane")
    return lane if lane in _LANES else default


def job_id_of(stored: dict[str, Any]) -> int | None:
    return stored.get("job_id")


# ── Ghi vào hàng đợi ────────────────────────────────────────────────


async def enqueue(
    db: AsyncSession,
    *,
    chat_id: int,
    method: str,
    payload: dict[str, Any],
    idem_key: str,
    lane: str = "bulk",
    job_id: int | None = None,
) -> int:
    """Xếp một tin vào hàng đợi. Trả về `outbox_id`. Gọi trong `transaction()`.

    Idempotent theo `idem_key`: gọi mười lần cho cùng một khoá vẫn chỉ có một dòng và
    lần nào cũng trả về đúng `outbox_id` đó. Đây là **chốt chặn cuối** của chuỗi chống
    gửi trùng — kể cả khi các lớp phía trên đều thủng, người dùng vẫn nhận đúng một tin.
    """
    if lane not in _LANES:
        raise ValueError(f"lane không hợp lệ: {lane!r} (có: {sorted(_LANES)})")

    envelope = {**payload, "lane": lane, "job_id": job_id}
    ins = (
        pg_insert(OutboxMessage)
        .values(chat_id=chat_id, method=method, payload=envelope, dedupe_key=idem_key)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(OutboxMessage.outbox_id)
    )
    outbox_id = (await db.execute(ins)).scalar_one_or_none()

    if outbox_id is None:
        # Đã có dòng cho khoá này. Trả lại đúng `outbox_id` cũ thay vì báo lỗi: người gọi
        # chỉ cần biết "tin này đã nằm trong hàng đợi", còn ai xếp nó vào thì không quan trọng.
        outbox_id = (
            await db.execute(
                select(OutboxMessage.outbox_id).where(OutboxMessage.dedupe_key == idem_key)
            )
        ).scalar_one()

    return outbox_id


# ── Lấy việc ────────────────────────────────────────────────────────

_SQL_CLAIM = """
WITH ready AS (
    SELECT outbox_id
      FROM outbox_messages
     WHERE state = :pending
       AND visible_at <= now()
       AND lease_until IS NULL
       AND (CAST(:lane AS text) IS NULL OR payload ->> 'lane' = CAST(:lane AS text))
     ORDER BY visible_at, outbox_id
       FOR UPDATE SKIP LOCKED
     LIMIT :limit
)
UPDATE outbox_messages o
   SET lease_until = now() + make_interval(secs => :lease),
       visible_at  = now() + make_interval(secs => :lease)
  FROM ready
 WHERE o.outbox_id = ready.outbox_id
RETURNING o.outbox_id, o.chat_id, o.method, o.payload, o.dedupe_key, o.attempts
"""


async def claim_batch(
    db: AsyncSession,
    *,
    limit: int,
    lane: str | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> list[Row[Any]]:
    """Nhận một lô việc và giữ chỗ chúng trong `lease_seconds` giây.

    `FOR UPDATE SKIP LOCKED` vì cùng lý do với việc cấp code: nhiều worker chạy song song
    **không bao giờ được nhận trùng một việc**, và người đến sau phải bỏ qua dòng đang bị
    khoá chứ không xếp hàng chờ nó.

    `visible_at` bị đẩy tới cuối hạn thuê để dòng đang bay rơi ra khỏi partial index —
    lần claim kế tiếp không phải bước qua chúng.

    Người gọi **phải commit** trước khi gửi: giữ chỗ mà chưa commit thì worker khác không
    nhìn thấy, và một lần chết ngang là mất luôn dấu vết.
    """
    rows = (
        await db.execute(
            text(_SQL_CLAIM),
            {
                "pending": STATE_PENDING,
                "lane": lane,
                "limit": limit,
                "lease": float(lease_seconds),
            },
        )
    ).all()
    return list(rows)


# ── Ghi kết quả ─────────────────────────────────────────────────────


async def mark_sent(db: AsyncSession, outbox_id: int, message_id: int | None) -> None:
    """Telegram đã nhận tin. Cố ý KHÔNG có điều kiện `state = 0`.

    Nếu một luồng khác vừa đánh dấu dòng này hỏng (hạn thuê quá hạn chẳng hạn) thì sự
    thật vẫn là tin ĐÃ tới tay người dùng — ghi đè sự thật đó bằng một phỏng đoán là
    cách nhanh nhất để gửi lần thứ hai.
    """
    await db.execute(
        text("""
            UPDATE outbox_messages
               SET state = :sent, tg_message_id = :mid, lease_until = NULL, last_error = NULL
             WHERE outbox_id = :oid
        """),
        {"sent": STATE_SENT, "mid": message_id, "oid": outbox_id},
    )


async def mark_failed(
    db: AsyncSession,
    outbox_id: int,
    error: str,
    *,
    permanent: bool,
) -> None:
    """Ghi thất bại. `permanent=True` là dừng hẳn, `False` là hẹn thử lại.

    Phân biệt hai loại này chính là chỗ hệ cũ làm sai: nó gộp 403 (chặn bot — thử lại vô
    ích) và 429/timeout (chưa gửi được, người nhận không có lỗi gì) vào cùng một
    `failed += 1`, nên mỗi đợt mất vĩnh viễn 960-3.830 người.

    Cả hai nhánh đều có điều kiện `state = 0`: một dòng đã `sent` thì không được phép
    quay ngược thành hỏng.
    """
    reason = error[:500]

    if permanent:
        await db.execute(
            text("""
                UPDATE outbox_messages
                   SET state = :failed, attempts = attempts + 1,
                       lease_until = NULL, last_error = :err
                 WHERE outbox_id = :oid AND state = :pending
            """),
            {
                "failed": STATE_FAILED,
                "pending": STATE_PENDING,
                "err": reason,
                "oid": outbox_id,
            },
        )
        return

    # Backoff luỹ thừa CÓ jitter, tính thẳng trong SQL để khỏi phải đọc `attempts` ra
    # trước. Jitter là bắt buộc chứ không phải trang trí: một sự cố mạng làm cả lô hỏng
    # cùng lúc, không tán ra thì cả lô cũng quay lại cùng một khoảnh khắc và lặp lại đúng
    # cú va chạm đó.
    await db.execute(
        text("""
            UPDATE outbox_messages
               SET attempts = attempts + 1,
                   lease_until = NULL,
                   last_error = :err,
                   state = CASE WHEN attempts + 1 >= :max_att THEN :failed ELSE :pending END,
                   visible_at = now() + make_interval(
                       secs => LEAST(:cap, power(2, attempts + 1)::float8) * :jitter
                   )
             WHERE outbox_id = :oid AND state = :pending
        """),
        {
            "err": reason,
            "max_att": MAX_ATTEMPTS,
            "failed": STATE_FAILED,
            "pending": STATE_PENDING,
            "cap": BACKOFF_CAP_SECONDS,
            "jitter": random.uniform(0.75, 1.25),  # noqa: S311 — tán nhịp thử lại, không phải bí mật
            "oid": outbox_id,
        },
    )


async def release(db: AsyncSession, outbox_id: int) -> None:
    """Trả việc về hàng đợi **ngay lập tức, không tính là một lần thử**.

    Dùng khi tin chưa hề rời khỏi tiến trình: xin token quá hạn, worker đang tắt. Gọi
    `mark_failed` cho những trường hợp đó là đếm sai — người nhận không có lỗi gì và hạn
    mức 5 lần thử phải để dành cho lỗi thật.
    """
    await db.execute(
        text("""
            UPDATE outbox_messages
               SET visible_at = now(), lease_until = NULL
             WHERE outbox_id = :oid AND state = :pending
        """),
        {"oid": outbox_id, "pending": STATE_PENDING},
    )


# ── Dọn việc bị bỏ rơi ──────────────────────────────────────────────

_SQL_REAP = """
UPDATE outbox_messages o
   SET visible_at = now(),
       lease_until = NULL,
       attempts = o.attempts + 1,
       state = CASE WHEN o.attempts + 1 >= :max_att THEN :failed ELSE :pending END,
       last_error = :err
  FROM (
    SELECT outbox_id
      FROM outbox_messages
     WHERE state = :pending
       AND lease_until IS NOT NULL
       AND lease_until < now() - make_interval(secs => :older)
       AND visible_at  < now() - make_interval(secs => :older)
     ORDER BY visible_at, outbox_id
       FOR UPDATE SKIP LOCKED
     LIMIT :limit
  ) stuck
 WHERE o.outbox_id = stuck.outbox_id
RETURNING o.outbox_id
"""


async def reap_stuck(
    db: AsyncSession,
    *,
    older_than_seconds: float,
    limit: int = 500,
) -> int:
    """Đưa về hàng đợi những việc bị worker chết giữa chừng. Trả về số dòng đã cứu.

    Không có bước này thì một tiến trình bị `kill -9` đúng lúc đang gửi sẽ để lại vài
    chục dòng `lease_until` treo vĩnh viễn: không ai gửi chúng, và cũng không có dòng lỗi
    nào để ai đó nhận ra.

    `attempts + 1` ở đây là **chốt chặn tin độc**: nếu chính payload đó làm worker chết
    thì sau `MAX_ATTEMPTS` vòng nó bị dừng hẳn thay vì kéo sập worker mãi mãi.

    Điều kiện `visible_at` là bản sao của điều kiện `lease_until` (claim đặt hai cột bằng
    nhau) — thêm vào để câu này quét được bằng partial index thay vì cả bảng.
    """
    rows = (
        await db.execute(
            text(_SQL_REAP),
            {
                "max_att": MAX_ATTEMPTS,
                "failed": STATE_FAILED,
                "pending": STATE_PENDING,
                "err": "lease_qua_han",
                "older": float(older_than_seconds),
                "limit": limit,
            },
        )
    ).all()

    if rows:
        log.warning("outbox_reap_lease_qua_han", so_luong=len(rows))
    return len(rows)


__all__ = [
    "BACKOFF_CAP_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "RESERVED_KEYS",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_SENT",
    "claim_batch",
    "enqueue",
    "job_id_of",
    "lane_of",
    "mark_failed",
    "mark_sent",
    "message_payload",
    "reap_stuck",
    "release",
]
