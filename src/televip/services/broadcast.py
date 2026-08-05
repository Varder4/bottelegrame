"""Điều phối một đợt bắn tin: chọn đích → bàn giao cho hàng đợi gửi → theo dõi tiến độ.

Bốn điều chi phối toàn bộ file, mỗi điều vá một lỗi đã tốn tiền thật của hệ cũ
(`02-broadcast.md`):

1. **Danh sách đích là DỮ LIỆU, không phải một vòng lặp Python.** `materialize_targets`
   bơm `broadcast_targets` bằng **một** câu `INSERT ... SELECT`. Hệ cũ `SELECT *` toàn bộ
   19.151 hàng `users` vào RAM rồi lặp — ở 300k người đó là vài trăm MB cho một việc mà
   database làm trong một câu lệnh, và tiến trình chết giữa chừng là mất sạch danh sách.
2. **Tiến độ sống trong bảng, không trong biến.** Restart giữa đợt thì `state` của từng
   đích vẫn còn; `pump` chỉ nhặt hàng còn `state = 0`. Bài học Lỗi 3: hệ cũ giữ tiến độ
   trong biến Python nên mỗi lần restart là bắn lại từ đầu cho những người đã nhận.
3. **Ba nhóm người KHÔNG BAO GIỜ nằm trong tệp đích**, ở mọi `audience`: đã chặn bot
   (`blocked_at`), đã tắt thông báo (`notify_optout`), và **chưa từng `/start`**
   (`started_bot_at IS NULL`). Nhóm thứ ba không phải bộ lọc sở thích mà là điều kiện vật
   lý: bot không có quyền nhắn trước cho người chưa mở hội thoại, gửi đi chỉ sinh 403 —
   và 403 hàng loạt là tín hiệu xấu nhất có thể gửi cho hệ chống spam của Telegram.
   Người đang bị ban cũng bị loại (`user_bans.unbanned_at IS NULL`).
4. **Không hàm nào ở đây gọi Bot API.** `pump` chỉ chuyển đích thành dòng
   `outbox_messages`; việc gửi thật là của worker outbox, qua `telegram/sender.py`.

## Ý nghĩa `broadcast_targets.state` trong luồng này

    0  chưa bàn giao — `pump` sẽ nhặt
    1  ĐÃ BÀN GIAO cho `outbox_messages` (chưa chắc đã gửi xong)
    3  bỏ qua (đợt bị huỷ trước khi tới lượt)

`sent_at` / `tg_message_id` cố ý **để trống** ở đây: chúng là dấu vết của lần gửi THẬT,
do tầng gửi điền. Vì vậy `status()` không đọc `broadcast_jobs.sent`, mà nối sang
`outbox_messages` qua `dedupe_key` để lấy con số sống — một báo cáo tiến độ nói sai còn
tệ hơn không có báo cáo nào.

## Chống gửi trùng

`dedupe_key = 'bc:<job_id>:<user_id>'` — tất định theo cặp `(job_id, user_id)`, khớp với
`UNIQUE (dedupe_key)` của `outbox_messages`. Gọi `pump` hai lần, hai worker cùng gọi, hay
tiến trình chết giữa `INSERT` và `COMMIT` đều cho ra đúng một tin cho mỗi người: lần thứ
hai đụng `ON CONFLICT DO NOTHING`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import now_utc
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.services import settings_service

log = get_logger(__name__)

# ── Hằng ────────────────────────────────────────────────────────────

#: Khớp `ck_broadcast_jobs_kind`. Lệch nhau thì lỗi nổ ở tầng database chứ không ở đây.
KINDS: Final[frozenset[str]] = frozenset({"broadcast", "send_event", "resend_tanthu"})

#: `audience` mà `materialize_targets` biết dựng câu lọc. `'custom'` cũng hợp lệ với
#: ràng buộc CHECK của bảng nhưng KHÔNG có ở đây: nó nghĩa là "danh sách do người gọi
#: cung cấp", và một câu SQL đoán ra danh sách đó là chỗ để gửi nhầm cho cả tệp.
AUDIENCES: Final[frozenset[str]] = frozenset({"active_30d", "all"})

#: Cửa sổ của `active_30d` — con số nằm ngay trong tên audience và trong CHECK của bảng,
#: nên nó là một phần của ĐỊNH NGHĨA chứ không phải một tham số vặn được.
ACTIVE_WINDOW_DAYS: Final = 30

#: Lane của mọi tin do broadcast sinh ra. `outbox_messages` không có cột `lane` nên nó
#: nằm trong `payload`, chỗ worker outbox đọc để xin token đúng lane (`cache/ratelimit`).
OUTBOX_LANE: Final = "bulk"

#: Trạng thái của một dòng `broadcast_targets` — xem docstring module.
TARGET_PENDING: Final = 0
TARGET_QUEUED: Final = 1
TARGET_FAILED: Final = 2
TARGET_SKIPPED: Final = 3

#: Số đích chuyển sang outbox mỗi lô. Nhỏ để một lần `pause` có hiệu lực gần như tức thì
#: và để giao dịch không giữ khoá trên hàng nghìn hàng cùng lúc.
#: Trần độ dài nội dung một đợt. Telegram từ chối tin quá 4.096 ký tự bằng `BadRequest`,
#: và lớp gửi coi `BadRequest` là lỗi VĨNH VIỄN nên không thử lại: cả đợt chạy hết vòng,
#: đếm đủ số đích, và KHÔNG MỘT AI nhận được gì.
#:
#: Trần này vốn nằm trong tầng phân tích tham số của lệnh Telegram, tức mọi đường vào
#: không-phải-Telegram đều đi vòng qua nó. Ép trong `create_job()` thì không đường vào nào
#: bỏ qua được, kể cả một script chạy tay.
MAX_CONTENT_CHARS: Final = 4_000

#: Khoá cấu hình ngưỡng số người nhận tối thiểu, và giá trị đường lui.
MIN_AUDIENCE_KEY: Final = "broadcast.min_audience"
DEFAULT_MIN_AUDIENCE: Final = 1

#: Tệp mặc định khi người tạo không chọn gì. **Hẹp**, không phải toàn bộ: một đợt gửi nhầm
#: toàn tệp là 19.151 tin không rút lại được, còn gửi nhầm tệp hẹp là vài nghìn.
DEFAULT_AUDIENCE: Final = "active_30d"

#: Nháp quá tuổi này thì không bấm Gửi được nữa. Tệp đích đóng băng ở thời điểm tạo nháp,
#: nên một bản nháp để quên vài ngày sẽ gửi cho một tệp không còn đúng — gồm cả những người
#: đã chặn bot trong khoảng đó.
DRAFT_MAX_AGE_SECONDS: Final = 3_600

#: Không gian khoá tư vấn của luồng bắn tin — `0x4243` = "BC". Khác `0x4556` ("EV", trần
#: ngân sách event) và `0x4344` ("CD", chiến dịch).
PUMP_LOCK_NS: Final = 0x4243

PUMP_BATCH: Final = 200

#: Trần tồn đọng của `outbox_messages`. Hàng đợi outbox dùng CHUNG cho tin cá nhân (trả
#: code) và tin hàng loạt; đổ 300k dòng vào đó một lần là bắt mọi tin cá nhân xếp sau
#: cả đợt broadcast. Vòng bơm dừng lại khi tồn đọng vượt mức này và chờ nó rút xuống.
PUMP_BACKLOG_LIMIT: Final = 1_000

#: Nhịp hỏi lại khi hàng đợi outbox đang đầy (giây).
PUMP_POLL_SECONDS: Final = 1.0


def dedupe_key(job_id: int, user_id: int) -> str:
    """Khoá chống gửi trùng của một (đợt, người nhận).

    Phải khớp từng ký tự với `_DEDUPE_EXPR` bên dưới — hai chỗ dựng cùng một khoá, và
    lệch nhau thì `status()` đếm ra 0 tin đã gửi trong khi người dùng đã nhận đủ.
    """
    return f"bc:{job_id}:{user_id}"


#: Bản SQL của `dedupe_key()`. `t` là alias của `broadcast_targets`.
_DEDUPE_EXPR: Final = "'bc:' || t.job_id || ':' || t.user_id"


@dataclass(frozen=True, slots=True)
class JobStatus:
    """Ảnh chụp tiến độ một đợt — mọi con số đọc tại chỗ, không có cache."""

    job_id: int
    kind: str
    audience: str
    state: str
    total: int
    #: Đích chưa bàn giao cho outbox.
    pending: int
    #: Đích đã bàn giao cho outbox.
    queued: int
    #: Đích bị bỏ qua vì đợt bị huỷ.
    skipped: int
    #: Dòng outbox còn chờ gửi.
    waiting: int
    #: Dòng outbox đã gửi xong.
    sent: int
    #: Dòng outbox hỏng vĩnh viễn.
    failed: int
    #: Người trong tệp đích đã chặn bot (`users.blocked_at`).
    blocked: int
    created_at: datetime
    finished_at: datetime | None
    #: Mốc bàn giao đầu tiên — gốc để tính tốc độ. NULL khi chưa bơm dòng nào.
    first_queued_at: datetime | None
    #: Tin/giây trung bình kể từ `first_queued_at`. 0 khi chưa đủ dữ liệu.
    rate_per_second: float

    @property
    def remaining(self) -> int:
        """Còn phải gửi bao nhiêu tin nữa: chưa bàn giao + đã bàn giao mà chưa gửi."""
        return self.pending + self.waiting


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Kết quả `cancel()` — đủ để lệnh admin nói rõ nó vừa chặn được bao nhiêu tin."""

    cancelled: bool
    #: Đích chưa kịp bàn giao, chuyển sang `skipped`.
    skipped_targets: int
    #: Dòng outbox đã bàn giao nhưng CHƯA gửi và chưa bị worker giữ — xoá kịp.
    dropped_outbox: int


# ── Tạo đợt ─────────────────────────────────────────────────────────

_SQL_CREATE_JOB = """
INSERT INTO broadcast_jobs (kind, audience, payload, state, created_by)
     VALUES (:kind, :audience, CAST(:payload AS jsonb), 'draft', :created_by)
  RETURNING job_id
"""


async def create_job(
    db: AsyncSession,
    *,
    kind: str,
    audience: str,
    payload: dict[str, Any],
    created_by: int,
) -> int:
    """Một đợt mới ở trạng thái `draft`. Trả về `job_id`.

    `draft` chứ không phải `running`: đợt chỉ bắt đầu khi có người bấm xác nhận. Nội dung
    lưu **một lần** ở `payload` — hệ cũ nhét bản sao nội dung vào từng dòng hàng đợi.

    Ném:
        ValueError: `kind` / `audience` không hợp lệ, nội dung quá dài, hoặc payload mang
            `parse_mode`.
    """
    if kind not in KINDS:
        raise ValueError(f"kind không hợp lệ: {kind!r} (hợp lệ: {', '.join(sorted(KINDS))})")
    if audience not in AUDIENCES:
        raise ValueError(
            f"audience không hợp lệ: {audience!r} (hợp lệ: {', '.join(sorted(AUDIENCES))})"
        )

    # Trần độ dài ép Ở ĐÂY, không ở tầng đọc tham số: xem ghi chú của `MAX_CONTENT_CHARS`.
    for truong in ("text", "caption"):
        noi_dung = payload.get(truong)
        if isinstance(noi_dung, str) and len(noi_dung) > MAX_CONTENT_CHARS:
            raise ValueError(
                f"nội dung dài {len(noi_dung)} ký tự, vượt trần {MAX_CONTENT_CHARS} — "
                "Telegram sẽ từ chối cả đợt và không ai nhận được gì"
            )

    # `parse_mode` mở đường cho HTML/Markdown do người gõ tự chèn. Một thẻ mở không đóng là
    # `BadRequest` cho TỪNG tin, tức cả đợt hỏng theo cùng một kiểu vĩnh viễn.
    if "parse_mode" in payload:
        raise ValueError("payload không được mang 'parse_mode' — nội dung gửi dạng văn bản thuần")

    row = await db.execute(
        text(_SQL_CREATE_JOB),
        {
            "kind": kind,
            "audience": audience,
            "payload": json.dumps(payload, ensure_ascii=False),
            "created_by": created_by,
        },
    )
    job_id = int(row.scalar_one())
    log.info("broadcast_job_tao", job_id=job_id, kind=kind, audience=audience, by=created_by)
    return job_id


# ── Chọn đích ───────────────────────────────────────────────────────

# Ba điều kiện đầu là hàng rào VẬT LÝ, đứng trước mọi bộ lọc audience và không bao giờ
# được bỏ — xem điểm 3 ở docstring module. Chúng khớp đúng partial index
# `ix_users_audience`, nên câu này đọc index chứ không quét toàn bảng `users`.
_SQL_MATERIALIZE = """
INSERT INTO broadcast_targets (job_id, user_id)
SELECT :job_id, u.user_id
  FROM users u
 WHERE u.blocked_at IS NULL
   AND u.notify_optout = false
   AND u.started_bot_at IS NOT NULL
   AND NOT EXISTS (
           SELECT 1
             FROM user_bans b
            WHERE b.user_id = u.user_id
              AND b.unbanned_at IS NULL
       )
   {audience_filter}
ON CONFLICT (job_id, user_id) DO NOTHING
"""

_AUDIENCE_FILTER: Final[dict[str, str]] = {
    "all": "",
    "active_30d": "AND u.last_active > now() - make_interval(days => :window_days)",
}

_SQL_COUNT_TARGETS = "SELECT count(*) FROM broadcast_targets WHERE job_id = :job_id"

_SQL_SET_TOTAL = """
UPDATE broadcast_jobs
   SET total = :total
 WHERE job_id = :job_id
"""


async def materialize_targets(db: AsyncSession, *, job_id: int, audience: str) -> int:
    """Bơm `broadcast_targets` bằng **một** câu `INSERT ... SELECT`. Trả về số đích.

    Gọi lại lần thứ hai cho cùng một đợt là vô hại (`ON CONFLICT DO NOTHING`) và trả về
    tổng số đích hiện có, không phải số hàng vừa chèn — con số mà lệnh admin cần in ra là
    "đợt này sẽ gửi cho bao nhiêu người", không phải "lần gọi này thêm được mấy người".

    Ném:
        ValueError: `audience` không nằm trong `AUDIENCES`.
    """
    if audience not in AUDIENCES:
        raise ValueError(
            f"audience không hợp lệ: {audience!r} (hợp lệ: {', '.join(sorted(AUDIENCES))})"
        )

    params: dict[str, Any] = {"job_id": job_id}
    if audience == "active_30d":
        params["window_days"] = ACTIVE_WINDOW_DAYS

    await db.execute(
        text(_SQL_MATERIALIZE.format(audience_filter=_AUDIENCE_FILTER[audience])), params
    )
    total = int((await db.execute(text(_SQL_COUNT_TARGETS), {"job_id": job_id})).scalar_one())
    await db.execute(text(_SQL_SET_TOTAL), {"job_id": job_id, "total": total})

    log.info("broadcast_dich_da_chon", job_id=job_id, audience=audience, total=total)
    return total


# ── Bàn giao cho hàng đợi gửi ───────────────────────────────────────

_SQL_JOB_ROW = "SELECT state, payload FROM broadcast_jobs WHERE job_id = :job_id"

# Một câu lệnh duy nhất, ba việc: giữ chỗ lô đích (`FOR UPDATE SKIP LOCKED` để hai worker
# không tranh cùng một hàng), sinh dòng outbox, rồi đánh dấu đã bàn giao. Tách thành ba
# câu là để hở đúng khe mà hệ cũ chết: chết giữa chừng thì có người nhận hai lần hoặc
# không nhận lần nào.
_SQL_PUMP = """
WITH picked AS (
    SELECT t.job_id, t.user_id
      FROM broadcast_targets t
     WHERE t.job_id = :job_id
       AND t.state = 0
       AND t.next_attempt_at <= now()
     ORDER BY t.user_id
     LIMIT :lim
       FOR UPDATE SKIP LOCKED
), queued AS (
    INSERT INTO outbox_messages (chat_id, method, payload, dedupe_key)
    SELECT p.user_id,
           CAST(:method AS text),
           (SELECT j.payload FROM broadcast_jobs j WHERE j.job_id = :job_id)
               || jsonb_build_object(
                      'lane', CAST(:lane AS text),
                      'job_id', p.job_id,
                      'user_id', p.user_id
                  ),
           'bc:' || p.job_id || ':' || p.user_id
      FROM picked p
    ON CONFLICT (dedupe_key) DO NOTHING
)
UPDATE broadcast_targets t
   SET state = 1
  FROM picked p
 WHERE t.job_id = p.job_id
   AND t.user_id = p.user_id
"""


def outbox_method(payload: dict[str, Any]) -> str:
    """Tên phương thức Bot API cho một `payload` đợt.

    Đọc từ chính nội dung thay vì thêm một cột: đợt có `photo` là `sendPhoto`, còn lại là
    `sendMessage`. Worker outbox nhờ đó không phải đoán, và một đợt kèm ảnh không lặng lẽ
    biến thành tin nhắn chữ.
    """
    return "sendPhoto" if "photo" in payload else "sendMessage"


async def pump(db: AsyncSession, *, job_id: int, limit: int = PUMP_BATCH) -> int:
    """Chuyển tối đa `limit` đích sang `outbox_messages`. Trả về số đích đã bàn giao.

    **Chỉ chạy khi đợt đang `running`** — đó là thứ làm `pause` và `cancel` có hiệu lực
    thật chứ không chỉ đổi một chữ trong bảng.

    Gọi lại không nhân đôi: `dedupe_key` tất định + `ON CONFLICT DO NOTHING`, và đích đã
    bàn giao chuyển sang `state = 1` nên lượt sau không nhặt lại.
    """
    if limit <= 0:
        return 0

    job = (await db.execute(text(_SQL_JOB_ROW), {"job_id": job_id})).one_or_none()
    if job is None or job.state != "running":
        return 0

    result = await db.execute(
        text(_SQL_PUMP),
        {
            "job_id": job_id,
            "lim": limit,
            "method": outbox_method(job.payload),
            "lane": OUTBOX_LANE,
        },
    )
    moved = int(result.rowcount or 0)
    if moved:
        log.info("broadcast_bam_giao", job_id=job_id, moved=moved)
    return moved


# ── Chuyển trạng thái ───────────────────────────────────────────────

_SQL_TRANSITION = """
UPDATE broadcast_jobs
   SET state = :new_state
 WHERE job_id = :job_id
   AND state = ANY(:from_states)
RETURNING state
"""

_SQL_FINISH = """
UPDATE broadcast_jobs j
   SET state = 'done', finished_at = now()
 WHERE j.job_id = :job_id
   AND j.state = 'running'
   AND NOT EXISTS (
           SELECT 1 FROM broadcast_targets t
            WHERE t.job_id = j.job_id AND t.state = 0
       )
RETURNING j.job_id
"""

_SQL_CANCEL = """
UPDATE broadcast_jobs
   SET state = 'cancelled', finished_at = now()
 WHERE job_id = :job_id
   AND state IN ('draft', 'running', 'paused')
RETURNING job_id
"""

_SQL_SKIP_REMAINING = """
UPDATE broadcast_targets
   SET state = 3
 WHERE job_id = :job_id
   AND state = 0
"""

# Xoá những tin ĐÃ vào outbox nhưng chưa gửi và chưa bị worker giữ (`lease_until`). Không
# có bước này thì "huỷ đợt" chỉ huỷ phần chưa bàn giao, còn hàng nghìn tin đã nằm trong
# hàng đợi vẫn bay đi — đúng thứ admin bấm huỷ để ngăn.
_SQL_DROP_QUEUED = f"""
DELETE FROM outbox_messages o
 USING broadcast_targets t
 WHERE t.job_id = :job_id
   AND o.dedupe_key = {_DEDUPE_EXPR}
   AND o.state = 0
   AND o.lease_until IS NULL
"""  # noqa: S608 - `_DEDUPE_EXPR` là hằng số của module, không có dữ liệu người dùng đi vào


async def _transition(
    db: AsyncSession, *, job_id: int, from_states: list[str], new_state: str
) -> str | None:
    row = await db.execute(
        text(_SQL_TRANSITION),
        {"job_id": job_id, "from_states": from_states, "new_state": new_state},
    )
    return row.scalar_one_or_none()


#: Một câu `UPDATE` có điều kiện, NGUYÊN TỬ. Postgres khoá hàng, nên hai request đồng thời
#: (bấm lại nút, F5 trên POST, hai tab) chỉ ĐÚNG MỘT cái nhận được `RETURNING` — cái còn
#: lại thấy `None` và không bơm gì. Đây là chỗ chống bấm-gửi-hai-lần, và nó nằm ở tầng
#: database chứ không ở giao diện: một nút bị vô hiệu bằng JavaScript không chặn được `curl`.
#:
#: Bốn điều kiện phụ, mỗi cái chặn một kịch bản khác nhau — xem docstring của `start()`.
_SQL_START = """
UPDATE broadcast_jobs
   SET state = 'running'
 WHERE job_id = :job_id
   AND state = 'draft'
   AND kind = :kind
   AND created_by = COALESCE(CAST(:actor AS bigint), created_by)
   AND total = COALESCE(CAST(:expect_total AS int), total)
   AND created_at > now() - make_interval(secs => CAST(:max_age AS int))
RETURNING state
"""


async def start(
    db: AsyncSession,
    *,
    job_id: int,
    kind: str,
    actor_id: int | None = None,
    expect_total: int | None = None,
    max_age_seconds: int = DRAFT_MAX_AGE_SECONDS,
) -> str | None:
    """`draft` → `running`. Trả `'running'` khi đổi được, `None` khi KHÔNG đủ điều kiện.

    `None` là câu trả lời đúng cho lượt bấm xác nhận thứ hai: đợt đã chạy rồi, và bắt đầu
    lại là gửi lần hai cho những người đã nhận.

    Bốn điều kiện phụ, mỗi cái chặn đúng một kịch bản đã đo được:

    ``kind`` — **bắt buộc**. Bản trước chỉ kiểm `state`, và trên Telegram điều đó an toàn vì
    `callback_data` chỉ đến từ nút do chính bot dựng. Trên web `job_id` là một trường POST
    giả được: một tài khoản có `/send_event` nhưng KHÔNG có `/broadcast` chỉ cần POST id của
    một đợt broadcast nháp là bắn tin cho 19.151 người bằng một quyền họ không có.

    ``actor_id`` — chỉ người TẠO nháp mới bấm gửi được nó. Không có nó thì hai admin soạn
    hai bản nháp cùng lúc có thể bấm nhầm nháp của nhau.

    ``expect_total`` — số đích mà người bấm ĐÃ NHÌN THẤY trên màn xem thử. Lệch thì từ chối:
    một màn xem thử mở từ hôm qua nói "gửi cho 3.000 người" trong khi tệp thật đã thành
    18.000 là một lần bấm với thông tin sai.

    ``max_age_seconds`` — tệp đích đóng băng ở thời điểm tạo nháp, nên một bản nháp để quên
    vài ngày sẽ gửi cho một tệp không còn đúng.
    """
    row = await db.execute(
        text(_SQL_START),
        {
            "job_id": job_id,
            "kind": kind,
            "actor": actor_id,
            "expect_total": expect_total,
            "max_age": int(max_age_seconds),
        },
    )
    new = row.scalar_one_or_none()
    if new is not None:
        log.info("broadcast_bat_dau", job_id=job_id, kind=kind, by=actor_id)
    else:
        # Ghi cả lượt KHÔNG bắt đầu được: đó là thứ cần trả lời khi soát lại "vì sao đợt
        # này không chạy" — im lặng ở đây là im lặng đúng chỗ khó chẩn đoán nhất.
        log.warning("broadcast_bat_dau_tu_choi", job_id=job_id, kind=kind, by=actor_id)
    return new


async def pause(db: AsyncSession, *, job_id: int) -> str | None:
    """`running` → `paused`. `None` khi đợt không đang chạy."""
    new = await _transition(db, job_id=job_id, from_states=["running"], new_state="paused")
    if new is not None:
        log.info("broadcast_tam_dung", job_id=job_id)
    return new


async def resume(db: AsyncSession, *, job_id: int) -> str | None:
    """`paused` → `running`. `None` khi đợt không đang tạm dừng."""
    new = await _transition(db, job_id=job_id, from_states=["paused"], new_state="running")
    if new is not None:
        log.info("broadcast_chay_tiep", job_id=job_id)
    return new


async def cancel(db: AsyncSession, *, job_id: int) -> CancelResult:
    """Huỷ hẳn: đổi trạng thái, bỏ qua đích còn lại, **xoá tin chưa gửi khỏi outbox**.

    Không đụng tới tin đã gửi hoặc đang được worker giữ — thu hồi tin đã bay khỏi máy chủ
    Telegram là việc của job xoá tin, không phải của lệnh huỷ.
    """
    cancelled = (await db.execute(text(_SQL_CANCEL), {"job_id": job_id})).scalar_one_or_none()
    if cancelled is None:
        return CancelResult(cancelled=False, skipped_targets=0, dropped_outbox=0)

    dropped = int((await db.execute(text(_SQL_DROP_QUEUED), {"job_id": job_id})).rowcount or 0)
    skipped = int((await db.execute(text(_SQL_SKIP_REMAINING), {"job_id": job_id})).rowcount or 0)

    log.info("broadcast_huy", job_id=job_id, skipped=skipped, dropped=dropped)
    return CancelResult(cancelled=True, skipped_targets=skipped, dropped_outbox=dropped)


async def finish_if_drained(db: AsyncSession, *, job_id: int) -> bool:
    """Đánh dấu `done` khi không còn đích nào chờ bàn giao. Trả `True` nếu vừa đổi."""
    done = (await db.execute(text(_SQL_FINISH), {"job_id": job_id})).scalar_one_or_none()
    if done is not None:
        log.info("broadcast_xong", job_id=job_id)
    return done is not None


# ── Theo dõi ────────────────────────────────────────────────────────

_SQL_JOB_HEAD = """
SELECT job_id, kind, audience, state, total, created_at, finished_at
  FROM broadcast_jobs
 WHERE job_id = :job_id
"""

# Nối sang outbox bằng ĐẲNG THỨC trên `dedupe_key` (đã có UNIQUE index) chứ không phải
# `LIKE 'bc:1:%'`: dạng LIKE không dùng được index với collation mặc định và biến một
# lệnh admin thành lượt quét toàn bảng outbox.
_SQL_JOB_PROGRESS = f"""
SELECT count(*) FILTER (WHERE t.state = 0)                AS pending,
       count(*) FILTER (WHERE t.state = 1)                AS queued,
       count(*) FILTER (WHERE t.state = 3)                AS skipped,
       count(*) FILTER (WHERE o.state = 0)                AS waiting,
       count(*) FILTER (WHERE o.state = 1)                AS sent,
       count(*) FILTER (WHERE o.state = 2)                AS failed,
       count(*) FILTER (WHERE u.blocked_at IS NOT NULL)   AS blocked,
       min(o.created_at)                                  AS first_queued_at,
       extract(epoch FROM now() - min(o.created_at))      AS elapsed
  FROM broadcast_targets t
  LEFT JOIN outbox_messages o ON o.dedupe_key = {_DEDUPE_EXPR}
  LEFT JOIN users u ON u.user_id = t.user_id
 WHERE t.job_id = :job_id
"""  # noqa: S608 - `_DEDUPE_EXPR` là hằng số của module, không có dữ liệu người dùng đi vào

_SQL_LATEST_JOB = """
SELECT job_id
  FROM broadcast_jobs
 WHERE CAST(:only_active AS boolean) = false OR state IN ('running', 'paused')
 ORDER BY job_id DESC
 LIMIT 1
"""


async def status(db: AsyncSession, job_id: int) -> JobStatus | None:
    """Tiến độ một đợt, hoặc `None` nếu không có đợt đó.

    Con số **đọc tại chỗ** từ `broadcast_targets` × `outbox_messages`, không đọc bộ đếm
    `broadcast_jobs.sent`: một bộ đếm tự cộng là thứ đã trôi khỏi sự thật ở hệ cũ, và một
    báo cáo tiến độ nói sai còn tệ hơn không có báo cáo nào.
    """
    head = (await db.execute(text(_SQL_JOB_HEAD), {"job_id": job_id})).one_or_none()
    if head is None:
        return None

    row = (await db.execute(text(_SQL_JOB_PROGRESS), {"job_id": job_id})).one()
    elapsed = float(row.elapsed or 0.0)
    sent = int(row.sent)

    return JobStatus(
        job_id=head.job_id,
        kind=head.kind,
        audience=head.audience,
        state=head.state,
        total=head.total,
        pending=int(row.pending),
        queued=int(row.queued),
        skipped=int(row.skipped),
        waiting=int(row.waiting),
        sent=sent,
        failed=int(row.failed),
        blocked=int(row.blocked),
        created_at=head.created_at,
        finished_at=head.finished_at,
        first_queued_at=row.first_queued_at,
        rate_per_second=(sent / elapsed) if elapsed > 0 and sent else 0.0,
    )


async def latest_job_id(db: AsyncSession, *, only_active: bool = True) -> int | None:
    """Đợt gần nhất — mặc định chỉ tính đợt đang chạy hoặc đang tạm dừng.

    Có nó thì `/broadcast_status` không tham số mới trả lời được đúng câu admin đang hỏi:
    "đợt đang chạy tới đâu rồi".
    """
    row = await db.execute(text(_SQL_LATEST_JOB), {"only_active": only_active})
    job_id = row.scalar_one_or_none()
    return None if job_id is None else int(job_id)


# ══════════════════════════════════════════════════════════════════════
# Tạo nháp: một hàm, hai đường vào
# ══════════════════════════════════════════════════════════════════════
#
# Trước bản này, "tạo đợt" là bốn bước rời nhau nằm trong handler Telegram: đọc ngưỡng, tạo
# job, dựng tệp đích, rồi huỷ nếu dưới ngưỡng. Bốn bước ấy đúng thứ tự thì an toàn; sai thứ
# tự thì để lại một bản nháp "GỬI NGAY" treo lơ lửng chờ ai đó bấm nhầm.
#
# Gói lại thành một hàm để đường vào thứ hai (panel web) không phải nhớ thứ tự — và không
# thể nhớ sai.


@dataclass(frozen=True, slots=True)
class DraftResult:
    """Kết quả tạo nháp. `refused=True` nghĩa là nháp đã bị HUỶ, không phải đang chờ."""

    job_id: int
    total: int
    audience: str
    refused: bool
    #: Ngưỡng đang áp dụng — để tầng hiển thị nói được con số thật, không phải một câu chung.
    minimum: int
    #: Nháp cùng loại của chính người này vừa bị huỷ để nhường chỗ.
    da_huy_nhap_cu: list[int]


async def min_audience(db: AsyncSession | None = None) -> int:
    """Ngưỡng số người nhận tối thiểu của một đợt."""
    return await settings_service.get_int(MIN_AUDIENCE_KEY, DEFAULT_MIN_AUDIENCE, db=db)


_SQL_MY_DRAFTS = """
SELECT job_id FROM broadcast_jobs
 WHERE state = 'draft' AND kind = :kind AND created_by = :actor AND job_id <> :tru
 ORDER BY job_id
"""


async def create_and_materialize(
    db: AsyncSession,
    *,
    kind: str,
    audience: str,
    payload: dict[str, Any],
    created_by: int,
) -> DraftResult:
    """Tạo nháp, dựng tệp đích, và ép ngưỡng — tất cả trong giao dịch của nơi gọi.

    Ba bất biến, và cả ba đều là hàng rào chứ không phải tiện lợi:

    1. **Dưới ngưỡng thì nháp bị HUỶ ngay trong cùng giao dịch.** Một đợt bị từ chối mà vẫn
       nằm ở `draft` là một nút "GỬI NGAY" treo lơ lửng. Số đích thấp bất thường hầu như
       luôn là bộ lọc sai, không phải tệp nhỏ thật.
    2. **Mỗi người tối đa MỘT nháp sống cho mỗi `kind`.** Bấm "tạo nháp" hai lần cho ra hai
       job, và bản cũ vẫn bấm gửi được — tức một nội dung đã bị thay vẫn bay đi. Nháp cũ
       của chính người này bị huỷ, nháp của người khác **không** bị đụng.
    3. **Tệp đích dựng NGAY**, nên `broadcast_jobs.total` là con số thật để màn xem thử in
       ra và để `start()` đối chiếu.

    ``db`` phải là session GHI. Không tự commit: nơi gọi quyết định.
    """
    job_id = await create_job(
        db, kind=kind, audience=audience, payload=payload, created_by=created_by
    )
    total = await materialize_targets(db, job_id=job_id, audience=audience)

    # Huỷ nháp cũ TRƯỚC khi trả về, và chỉ nháp của chính người này.
    cu = [
        int(v)
        for v in (
            await db.execute(
                text(_SQL_MY_DRAFTS), {"kind": kind, "actor": created_by, "tru": job_id}
            )
        ).scalars()
    ]
    for old in cu:
        await cancel(db, job_id=old)

    nguong = await min_audience(db)
    if total < nguong:
        await cancel(db, job_id=job_id)
        log.warning(
            "broadcast_duoi_nguong", job_id=job_id, total=total, nguong=nguong, by=created_by
        )
        return DraftResult(
            job_id=job_id,
            total=total,
            audience=audience,
            refused=True,
            minimum=nguong,
            da_huy_nhap_cu=cu,
        )

    return DraftResult(
        job_id=job_id,
        total=total,
        audience=audience,
        refused=False,
        minimum=nguong,
        da_huy_nhap_cu=cu,
    )


@dataclass(frozen=True, slots=True)
class NoiDungDot:
    """Nội dung và tuổi của một đợt — đủ cho màn xem thử, không hơn."""

    job_id: int
    kind: str
    payload: dict[str, Any]
    created_by: int
    created_at: datetime
    #: Nháp quá tuổi thì không bấm Gửi được nữa. Tính Ở ĐÂY, không ở tầng hiển thị: điều
    #: kiện thật nằm trong câu `UPDATE` của `start()`, và một màn hình tự tính bằng công
    #: thức khác sẽ hiện nút Gửi cho một nháp mà service chắc chắn từ chối.
    qua_han: bool

    @property
    def text(self) -> str:
        gia_tri = self.payload.get("text")
        return gia_tri if isinstance(gia_tri, str) else ""


_SQL_JOB_CONTENT = """
SELECT job_id, kind, payload, created_by, created_at
  FROM broadcast_jobs WHERE job_id = :job_id
"""


async def job_content(db: AsyncSession, job_id: int) -> NoiDungDot | None:
    """Nội dung một đợt. `None` khi không có đợt đó."""
    row = (await db.execute(text(_SQL_JOB_CONTENT), {"job_id": job_id})).one_or_none()
    if row is None:
        return None
    tuoi = (now_utc() - row.created_at).total_seconds()
    return NoiDungDot(
        job_id=row.job_id,
        kind=row.kind,
        payload=row.payload or {},
        created_by=row.created_by,
        created_at=row.created_at,
        qua_han=tuoi > DRAFT_MAX_AGE_SECONDS,
    )


#: Sàn tốc độ dùng để ƯỚC TÍNH thời gian gửi. Thấp hơn trần thật (30 tin/giây) một cách có
#: chủ ý: hàng đợi dùng chung với tin cá nhân, và một ước tính lạc quan làm người vận hành
#: tưởng đợt đã treo trong khi nó đang chạy đúng.
ESTIMATE_RATE: Final = 8.0


def estimate_seconds(total: int) -> int:
    """Ước tính thời gian gửi xong, theo sàn tốc độ."""
    return 0 if total <= 0 else int(total / ESTIMATE_RATE) + 1


def format_duration(seconds: int) -> str:
    """`95` → `1 phút 35 giây`. Dùng chung cho bot và web."""
    if seconds < 60:
        return f"{seconds} giây"
    phut, giay = divmod(seconds, 60)
    if phut < 60:
        return f"{phut} phút {giay} giây" if giay else f"{phut} phút"
    gio, phut = divmod(phut, 60)
    return f"{gio} giờ {phut} phút" if phut else f"{gio} giờ"


# ── Vòng bơm nền ────────────────────────────────────────────────────

_SQL_OUTBOX_BACKLOG = "SELECT count(*) FROM outbox_messages WHERE state = 0"

#: `job_id` → task đang bơm. Chỉ để **không chạy hai vòng bơm cho cùng một đợt** trong một
#: tiến trình; nó KHÔNG phải nơi giữ tiến độ (tiến độ nằm ở `broadcast_targets`), nên mất
#: dict này khi restart không mất gì.
_pump_tasks: dict[int, asyncio.Task[int]] = {}


async def run_pump_loop(job_id: int, *, batch: int = PUMP_BATCH) -> int:
    """Bơm từng lô cho tới khi hết đích, hết trạng thái `running`, hoặc bị huỷ.

    Dừng lại khi hàng đợi outbox còn tồn đọng quá `PUMP_BACKLOG_LIMIT`: hàng đợi đó dùng
    chung với tin cá nhân (trả code), và đổ cả đợt vào một lần là bắt người vừa bấm nhận
    code xếp sau 300k tin quảng bá.
    """
    moved_total = 0
    while True:
        async with transaction() as db:
            # Đọc trạng thái TRƯỚC cả bước đo tồn đọng: nếu không, một đợt bị `pause` đúng
            # lúc hàng đợi đang đầy sẽ quay vòng chờ mãi mà không bao giờ nhìn lại bảng.
            job = (await db.execute(text(_SQL_JOB_ROW), {"job_id": job_id})).one_or_none()
            if job is None or job.state != "running":
                return moved_total

            # Khoá tư vấn LIÊN TIẾN TRÌNH. `_pump_tasks` chỉ là dict trong RAM của một
            # tiến trình, nên nó không biết gì về vòng bơm của tiến trình khác — và hai
            # vòng bơm cùng đợt thì mỗi vòng tự đo tồn đọng độc lập, tức trần 1.000 thành
            # 2.000 và tin trả code của người vừa /start xếp sau gấp đôi thời gian.
            #
            # Bản `try_` chứ không phải bản chờ: bản chờ giữ một kết nối pool đứng im.
            # Và khoá `xact` tự nhả cuối mỗi lô, nên một tiến trình bị kill không để lại
            # khoá kẹt.
            co_khoa = bool(
                (
                    await db.execute(
                        text("SELECT pg_try_advisory_xact_lock(:ns, :job_id)"),
                        {"ns": PUMP_LOCK_NS, "job_id": job_id},
                    )
                ).scalar_one()
            )
            if not co_khoa:
                moved = -1  # "chưa tới lượt" — KHÁC hẳn 0, vốn nghĩa là "hết việc"
                # `finally` của transaction sẽ nhả khoá; ngủ rồi thử lại ở vòng sau.
                await asyncio.sleep(PUMP_POLL_SECONDS)
                continue

            backlog = int((await db.execute(text(_SQL_OUTBOX_BACKLOG))).scalar_one())
            if backlog >= PUMP_BACKLOG_LIMIT:
                moved = -1  # tín hiệu "chưa tới lượt", khác hẳn với "hết việc"
            else:
                moved = await pump(db, job_id=job_id, limit=batch)
                if moved == 0:
                    await finish_if_drained(db, job_id=job_id)

        if moved == 0:
            return moved_total
        if moved > 0:
            moved_total += moved
        await asyncio.sleep(PUMP_POLL_SECONDS if moved < 0 else 0)


def start_pump(job_id: int, *, batch: int = PUMP_BATCH) -> asyncio.Task[int] | None:
    """Chạy `run_pump_loop` như một task nền. Trả `None` nếu đợt đã có vòng bơm đang chạy.

    Không dựng task thứ hai cho cùng một đợt: hai vòng bơm không làm ai nhận hai lần
    (khoá `dedupe_key` chặn) nhưng chúng tranh nhau khoá hàng và làm log khó đọc.
    """
    existing = _pump_tasks.get(job_id)
    if existing is not None and not existing.done():
        return None

    task = asyncio.create_task(run_pump_loop(job_id, batch=batch), name=f"broadcast-pump-{job_id}")
    _pump_tasks[job_id] = task
    task.add_done_callback(lambda done: _on_pump_done(job_id, done))
    return task


def _on_pump_done(job_id: int, task: asyncio.Task[int]) -> None:
    # So DANH TÍNH trước khi xoá. Có một khe giữa lúc task kết thúc và lúc callback này
    # chạy; nếu job định kỳ gọi `start_pump` trúng khe đó thì đã có một task MỚI trong
    # dict, và `pop` vô điều kiện sẽ xoá dấu của task còn sống — sau đó lần gọi kế tiếp
    # dựng thêm một vòng bơm thứ hai cho cùng một đợt.
    if _pump_tasks.get(job_id) is task:
        del _pump_tasks[job_id]
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        # Task nền nuốt lỗi là cách chắc chắn nhất để một đợt đứng im mà không ai biết.
        log.error("broadcast_pump_loi", job_id=job_id, detail=str(error))


_SQL_RUNNING_JOBS = """
SELECT job_id FROM broadcast_jobs WHERE state = 'running' ORDER BY job_id LIMIT :lim
"""


async def resume_running_jobs(*, limit: int = 50) -> list[int]:
    """Bơm tiếp mọi đợt còn `running` sau khi tiến trình khởi động lại.

    Trạng thái nằm trong bảng chứ không trong RAM, nên "chạy tiếp" chỉ là dựng lại vòng
    bơm. Thiếu bước này thì một lần restart giữa đợt làm đợt đó đứng im vĩnh viễn ở trạng
    thái `running` — không ai nhận thêm tin và cũng không có dòng lỗi nào.
    """
    async with transaction() as db:
        rows = await db.execute(text(_SQL_RUNNING_JOBS), {"lim": limit})
        job_ids = [int(v) for v in rows.scalars()]
    for job_id in job_ids:
        start_pump(job_id)
    if job_ids:
        log.info("broadcast_chay_tiep_sau_restart", jobs=job_ids)
    return job_ids


__all__ = [
    "AUDIENCE_LABEL",
    "DEFAULT_AUDIENCE",
    "DEFAULT_MIN_AUDIENCE",
    "DRAFT_MAX_AGE_SECONDS",
    "ESTIMATE_RATE",
    "MAX_CONTENT_CHARS",
    "MIN_AUDIENCE_KEY",
    "PUMP_LOCK_NS",
    "DraftResult",
    "NoiDungDot",
    "job_content",
    "create_and_materialize",
    "estimate_seconds",
    "format_duration",
    "min_audience",
    "JOB_STATE_LABEL",
    "DongDot",
    "list_jobs",
    "ACTIVE_WINDOW_DAYS",
    "AUDIENCES",
    "KINDS",
    "OUTBOX_LANE",
    "PUMP_BATCH",
    "CancelResult",
    "JobStatus",
    "cancel",
    "create_job",
    "dedupe_key",
    "finish_if_drained",
    "latest_job_id",
    "materialize_targets",
    "outbox_method",
    "pause",
    "pump",
    "resume",
    "resume_running_jobs",
    "run_pump_loop",
    "start",
    "start_pump",
    "status",
]


# ── Danh sách đợt, cho màn hình quản trị ────────────────────────────
#
# `status(job_id)` trả lời "đợt này đang ra sao". Không có hàm nào trả lời "có những đợt
# nào" — trên Telegram không cần, vì admin vừa tạo đợt nào thì nhớ số đợt đó. Trên web thì
# đó là câu hỏi đầu tiên của màn hình.


#: Nhãn tiếng Việt của tệp người nhận. Ở cạnh dữ liệu chứ không ở template: hai màn hình
#: cùng đọc `audience` mà mỗi nơi tự dịch là hai bản dịch sẽ lệch nhau.
AUDIENCE_LABEL: Final[dict[str, str]] = {
    "active_30d": f"hoạt động {ACTIVE_WINDOW_DAYS} ngày gần đây",
    "all": "TOÀN BỘ người đã /start",
    "custom": "danh sách chỉ định",
}

JOB_STATE_LABEL: Final[dict[str, str]] = {
    "draft": "nháp, chờ xác nhận",
    "running": "đang chạy",
    "paused": "đang tạm dừng",
    "done": "đã xong",
    "cancelled": "đã huỷ",
}


@dataclass(frozen=True, slots=True)
class DongDot:
    job_id: int
    kind: str
    audience: str
    state: str
    total: int
    sent: int
    failed: int
    created_by: int
    created_at: datetime
    finished_at: datetime | None

    @property
    def dang_chay(self) -> bool:
        return self.state == "running"

    @property
    def huy_duoc(self) -> bool:
        """Chỉ đợt chưa kết thúc mới huỷ được — cùng luật với `cancel()`."""
        return self.state in {"draft", "running", "paused"}


_SQL_JOBS = """
SELECT job_id, kind, audience, state, total, sent, failed, created_by,
       created_at, finished_at
  FROM broadcast_jobs
 WHERE (CAST(:cursor AS int) IS NULL OR job_id < CAST(:cursor AS int))
 ORDER BY job_id DESC
 LIMIT :lim
"""


async def list_jobs(
    db: AsyncSession, *, cursor: int | None = None, limit: int = 30
) -> list[DongDot]:
    """Các đợt gần nhất, mới nhất trước. `cursor` là `job_id` của dòng cuối trang trước.

    Phân trang keyset chứ không OFFSET: `job_id` tăng đơn điệu nên nó vừa là con trỏ vừa
    là thứ tự, và không có dòng nào bị nhảy khi có đợt mới chen vào giữa hai lần bấm.
    """
    rows = (await db.execute(text(_SQL_JOBS), {"cursor": cursor, "lim": limit})).all()
    return [
        DongDot(
            job_id=r.job_id,
            kind=r.kind,
            audience=r.audience,
            state=r.state,
            total=r.total,
            sent=r.sent,
            failed=r.failed,
            created_by=r.created_by,
            created_at=r.created_at,
            finished_at=r.finished_at,
        )
        for r in rows
    ]
