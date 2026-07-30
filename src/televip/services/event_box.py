"""Event đập hộp — `13-dac-ta-bot-moi.md` §13.2.9 và §13.5.1.

Hai luật của file này, và cả hai đều là chỗ bot cũ sai nặng nhất.

## 1. CHỈ CÓ MỘT BẢNG TỈ LỆ

Bảng nằm ở `settings.event.prize_table` (trọng số basis point, tổng đúng 10.000).
`draw()` quay số trên chính nó, `render_caption()` in ra chính nó. Không có hằng số tỉ lệ
nào trong file này và không có "bảng hiển thị" ở bất kỳ đâu khác.

Bot cũ có hai từ điển song song (`config.py:78-92`): `EVENT_PROBABILITIES_REAL` để quay
(rỗng 60% / 5k 35% / 10k 5%) và `EVENT_PROBABILITIES_DISPLAY` để in (có cả 20k, 50k, 88k).
Ba mệnh giá được quảng cáo cho hàng chục nghìn người có xác suất trúng **bằng 0**. Đó
không phải sai số kỹ thuật — đó là hai sự thật cùng sống trong một file, và loại lỗi đó
chỉ chết hẳn khi **xoá một trong hai nguồn**. Ở đây nguồn thứ hai không tồn tại, nên hai
con số không thể lệch nhau về mặt kiến trúc.

## 2. CỬA SỔ MỞ HỘP TÍNH THEO `sent_at` CỦA TỪNG NGƯỜI NHẬN

    now() <= COALESCE(broadcast_targets.sent_at, events.created_at) + window_minutes

Bot cũ tính từ `events.created_at` — một mốc **toàn cục** — trong khi giao 300k tin mất
khoảng ba giờ. Chỉ nhóm nhận tin đầu tiên còn trong cửa sổ 10 phút; phần lớn người nhận
bị ép "hộp rỗng" mà không ai biết vì sao. `events.created_at` ở đây chỉ còn là đường dự
phòng khi chưa có `sent_at`, và mỗi lượt tham gia ghi lại mình đã dùng gốc nào
(`event_participations.window_source`) để đo được thay đổi này tốn thêm bao nhiêu code.

## Thứ tự các bước của `open_box`, và vì sao đúng thứ tự đó

    1. đọc event                → không có / đã đóng ⇒ `closed`, không ghi gì
    2. tính cửa sổ theo sent_at → quyết định `window_source`
    3. GIÀNH CHỖ bằng PK        → thua ⇒ `already`, và đó là hàng rào THẬT (DB, không Python)
    4. ngoài cửa sổ ⇒ `empty`   → dòng đã ghi ở bước 3 giữ nguyên `result='empty'`
    5. chạm trần ngân sách ⇒ `empty` (lý do khác, tin nhắn khác — §13.5.1 điểm 3)
    6. quay số                  → rỗng ⇒ dừng
    7. trúng ⇒ `code_issuance.reserve()` trong SAVEPOINT

Bước 3 đứng **trước** bước quay số vì hàng rào chống mở hai lần phải là PK
`(user_id, event_id)` chứ không phải một câu `SELECT` rồi `if` trong Python (nguyên tắc
N4). Dòng ghi trước mang kết quả tạm `empty`, và các bước sau chỉ việc `UPDATE` nó.

Bước 7 nằm trong `begin_nested()` (SAVEPOINT) vì `reserve()` cố ý ném `OutOfStock` để
giao dịch cuộn lại — cuộn cả giao dịch ngoài thì mất luôn dòng giành chỗ ở bước 3, và
người dùng mở lại được lần hai. SAVEPOINT cuộn đúng phần grant, giữ nguyên phần còn lại.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import now_utc
from televip.core.errors import AlreadyClaimed, ConfigError, OutOfStock
from televip.core.ids import grant_key_event
from televip.core.logging import get_logger
from televip.domain import texts
from televip.services import code_issuance, settings_service, text_service

log = get_logger(__name__)

# ── Hằng ────────────────────────────────────────────────────────────

#: Khớp CHECK `event_type_valid` của bảng `events`.
EVENT_TYPE: Final = "dap_hop"

#: Loại trong KHO (`codes.code_type`) và loại trong SỔ CÁI (`code_grants.grant_type`) —
#: hai khái niệm khác nhau, cố ý khác tên (`db/models/codes.py`).
CODE_TYPE: Final = "event"
GRANT_TYPE: Final = "event_box"

PRIZE_TABLE_KEY: Final = "event.prize_table"
WINDOW_MINUTES_KEY: Final = "event.window_minutes"
BUDGET_CAP_KEY: Final = "event.budget_cap_vnd"
REQUIRE_FULL_STOCK_KEY: Final = "event.require_full_stock"
GAME_LINK_KEY: Final = "link.game_bot"

#: Mẫu số của trọng số basis point (phần vạn). Đây là **định nghĩa của đơn vị**, không
#: phải một con số nghiệp vụ vặn được: §13.5.1 buộc `event.prize_table` cộng đúng bằng nó.
TOTAL_WEIGHT_BP: Final = 10_000

#: Giá trị hợp lệ của `event_participations.result` khi TRÚNG — phải khớp từng chữ với
#: CHECK `result_valid`. Một mệnh giá lạ trong bảng tỉ lệ sẽ bị chặn ngay lúc đọc cấu
#: hình (`parse_prize_table`), chứ không nổ dưới dạng lỗi CHECK giữa lượt bấm của user.
WINNING_RESULTS: Final[frozenset[str]] = frozenset({"5k", "10k", "20k", "50k", "88k"})

RESULT_EMPTY: Final = "empty"
RESULT_OUT_OF_STOCK: Final = "out_of_stock"

#: Gốc thời gian đã dùng để tính cửa sổ — khớp CHECK `window_source_valid`.
WINDOW_PER_RECIPIENT: Final = "per_recipient"
WINDOW_EVENT_CREATED: Final = "event_created"

BoxStatus = Literal["win", "empty", "out_of_stock", "already", "closed"]

#: Vì sao ra kết quả đó. Chỉ `empty` mới có nhiều lý do, và chúng dẫn tới **hai câu chữ
#: khác nhau**: chạm trần ngân sách là điều duy nhất được phép làm lệch tỉ lệ đã quảng
#: cáo, nên §13.5.1 điểm 3 buộc nó phải hiện ra chứ không được im lặng.
BoxReason = Literal[
    "won", "draw", "window_closed", "budget_cap", "event_inactive", "already_played", "no_stock"
]


@dataclass(frozen=True, slots=True)
class Prize:
    """Một mức của bảng tỉ lệ. `value_vnd = 0` là hộp rỗng."""

    value_vnd: int
    weight_bp: int

    @property
    def is_empty(self) -> bool:
        return self.value_vnd == 0


@dataclass(frozen=True, slots=True)
class BoxResult:
    """Kết quả một lượt mở hộp — đủ để handler chọn tin nhắn mà không đọc lại database."""

    status: BoxStatus
    reason: BoxReason
    value_vnd: int = 0
    code_value: str | None = None
    grant_id: int | None = None
    #: `None` khi không có lượt tham gia nào được ghi (event không tồn tại / đã đóng).
    window_source: str | None = None


@dataclass(frozen=True, slots=True)
class StockRow:
    """Một dòng của bảng đối chiếu kho trước khi bắn event."""

    value_vnd: int
    weight_bp: int
    available: int

    @property
    def missing(self) -> bool:
        return self.available == 0


# ── Bảng tỉ lệ ──────────────────────────────────────────────────────


def parse_prize_table(raw: Sequence[Any]) -> tuple[Prize, ...]:
    """Đọc `settings.event.prize_table` thành các mức đã kiểm.

    Kiểm ở đây chứ không ở chỗ quay số: một bảng tỉ lệ hỏng phải bị chặn lúc **đọc cấu
    hình** (tức lúc `/send_event` hoặc lúc lượt bấm đầu tiên), không phải lúc đã có người
    đang chờ code.

    Ném:
        ConfigError: sai cấu trúc, trọng số âm, mệnh giá trùng, mệnh giá không ghi được
            vào `event_participations.result`, hoặc tổng trọng số khác 10.000.
    """
    if not raw:
        raise ConfigError(f"settings[{PRIZE_TABLE_KEY!r}] rỗng")

    prizes: list[Prize] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"settings[{PRIZE_TABLE_KEY!r}] phải là mảng object, gặp {item!r}")
        try:
            value_vnd = item["value_vnd"]
            weight_bp = item["weight_bp"]
        except KeyError as exc:
            raise ConfigError(
                f"settings[{PRIZE_TABLE_KEY!r}] thiếu khoá {exc.args[0]!r} ở mức {item!r}"
            ) from exc

        if isinstance(value_vnd, bool) or not isinstance(value_vnd, int) or value_vnd < 0:
            raise ConfigError(f"mệnh giá không hợp lệ trong bảng tỉ lệ: {value_vnd!r}")
        if isinstance(weight_bp, bool) or not isinstance(weight_bp, int) or weight_bp < 0:
            raise ConfigError(f"trọng số không hợp lệ trong bảng tỉ lệ: {weight_bp!r}")
        if value_vnd in seen:
            raise ConfigError(f"mệnh giá {value_vnd} xuất hiện hai lần trong bảng tỉ lệ")
        if value_vnd and result_label(value_vnd) not in WINNING_RESULTS:
            raise ConfigError(
                f"mệnh giá {value_vnd} không ghi được vào event_participations.result "
                f"(chỉ nhận: {', '.join(sorted(WINNING_RESULTS))})"
            )

        seen.add(value_vnd)
        prizes.append(Prize(value_vnd=value_vnd, weight_bp=weight_bp))

    total = sum(p.weight_bp for p in prizes)
    if total != TOTAL_WEIGHT_BP:
        raise ConfigError(
            f"tổng trọng số của {PRIZE_TABLE_KEY} là {total}, phải đúng {TOTAL_WEIGHT_BP} "
            f"(basis point)"
        )
    return tuple(prizes)


async def prize_table(db: AsyncSession | None = None) -> tuple[Prize, ...]:
    """Bảng tỉ lệ đang chạy. **Nguồn duy nhất** cho cả quay số lẫn caption."""
    return parse_prize_table(await settings_service.get_list(PRIZE_TABLE_KEY, db=db))


def result_label(value_vnd: int) -> str:
    """`5000` → `'5k'` — nhãn ghi vào `event_participations.result`."""
    return texts.value_label(value_vnd).lower()


def draw(prizes: Sequence[Prize], *, roll: Callable[[int], int] = secrets.randbelow) -> Prize:
    """Quay một lượt trên bảng tỉ lệ. Trả về mức trúng (có thể là hộp rỗng).

    Dùng `secrets`, **không** dùng `random`: nguồn ngẫu nhiên dính tới tiền thì không được
    đoán trước được. `roll` chỉ để test bơm một con số cố định vào.
    """
    ticket = roll(TOTAL_WEIGHT_BP)
    cursor = 0
    for prize in prizes:
        cursor += prize.weight_bp
        if ticket < cursor:
            return prize
    # Chỉ tới được đây nếu tổng trọng số < 10.000, mà `parse_prize_table` đã chặn.
    return prizes[-1]


async def render_caption(db: AsyncSession | None = None) -> str:
    """Caption tin event — **sinh ra từ chính bảng tỉ lệ dùng để quay**.

    Không nhận tham số tỉ lệ nào từ bên ngoài, và không có đường nào truyền vào một bộ số
    khác. Đó là toàn bộ lý do hàm này tồn tại (§13.5 dòng E).
    """
    prizes = await prize_table(db)
    game_link = await settings_service.get_str(GAME_LINK_KEY, "", db=db)
    return await text_service.render(
        "event.box_caption",
        prize_lines=texts.event_prize_lines([(p.value_vnd, p.weight_bp) for p in prizes]),
        game_link=game_link,
    )


# ── Kho ─────────────────────────────────────────────────────────────


async def stock_report(db: AsyncSession) -> list[StockRow]:
    """Tồn kho của **từng mệnh giá có trọng số > 0**, theo bảng tỉ lệ đang chạy.

    §13.5.1 điều kiện 1: quảng cáo một giải mà kho không có mã là lừa người dùng, nên
    `/send_event` phải đối chiếu đúng danh sách này trước khi bắn.
    """
    rows: list[StockRow] = []
    for prize in await prize_table(db):
        if prize.is_empty or prize.weight_bp <= 0:
            continue
        available = await code_issuance.available_count(
            db, code_type=CODE_TYPE, value_vnd=prize.value_vnd
        )
        rows.append(
            StockRow(value_vnd=prize.value_vnd, weight_bp=prize.weight_bp, available=available)
        )
    return rows


async def missing_stock(db: AsyncSession) -> list[StockRow]:
    """Những mệnh giá có trọng số > 0 mà kho đang rỗng. Rỗng nghĩa là đủ điều kiện bắn."""
    return [row for row in await stock_report(db) if row.missing]


# ── Tạo đợt ─────────────────────────────────────────────────────────

_SQL_CREATE_EVENT = """
INSERT INTO events (event_type, created_by, media_asset_key, caption, is_active)
     VALUES (:event_type, :created_by, :asset, :caption, true)
  RETURNING event_id
"""

_SQL_ATTACH_JOB = """
UPDATE events
   SET job_id = :job_id
 WHERE event_id = :event_id
   AND job_id IS NULL
RETURNING event_id
"""

_SQL_CLOSE_EVENT = "UPDATE events SET is_active = false WHERE event_id = :event_id"

_SQL_EVENT_OF_JOB = "SELECT event_id FROM events WHERE job_id = :job_id"


async def create_event(
    db: AsyncSession,
    *,
    created_by: int,
    caption: str,
    media_asset_key: str | None = None,
) -> int:
    """Một đợt đập hộp mới. Trả về `event_id`. Gọi trong `transaction()`.

    `caption` lưu lại **đúng chuỗi đã gửi đi**, kể cả khối tỉ lệ: khi có khiếu nại "bot
    quảng cáo 88k" thì câu trả lời phải đọc được từ dữ liệu, không phải dựng lại bằng cấu
    hình hiện tại (mà admin có thể đã đổi sau đó).
    """
    event_id = int(
        (
            await db.execute(
                text(_SQL_CREATE_EVENT),
                {
                    "event_type": EVENT_TYPE,
                    "created_by": created_by,
                    "asset": media_asset_key,
                    "caption": caption,
                },
            )
        ).scalar_one()
    )
    log.info("event_tao", event_id=event_id, created_by=created_by)
    return event_id


async def attach_job(db: AsyncSession, *, event_id: int, job_id: int) -> bool:
    """Gắn đợt bắn tin vào event. Trả `False` nếu event đã có job.

    Không có bước này thì `open_box` không tìm được `broadcast_targets.sent_at` của người
    bấm, và cửa sổ 10 phút lại rơi về mốc toàn cục — đúng lỗi của bot cũ.
    """
    row = (
        await db.execute(text(_SQL_ATTACH_JOB), {"event_id": event_id, "job_id": job_id})
    ).scalar_one_or_none()
    return row is not None


async def close_event(db: AsyncSession, *, event_id: int) -> None:
    """Đóng một đợt: mọi lượt bấm sau đó trả `closed`."""
    await db.execute(text(_SQL_CLOSE_EVENT), {"event_id": event_id})


async def event_of_job(db: AsyncSession, *, job_id: int) -> int | None:
    """Event của một đợt bắn tin, hoặc `None` nếu đợt đó không phải event."""
    row = (await db.execute(text(_SQL_EVENT_OF_JOB), {"job_id": job_id})).scalar_one_or_none()
    return None if row is None else int(row)


def expected_cost_vnd(prizes: Sequence[Prize]) -> float:
    """Chi phí kỳ vọng mỗi lượt = Σ (xác suất × mệnh giá) — §13.5.1.

    Suy ra từ bảng tỉ lệ, không phải một con số gõ tay: đổi tỉ lệ bằng `/setcauhinh` thì
    ước tính chi phí của `/send_event` tự đổi theo.
    """
    return sum(p.value_vnd * p.weight_bp for p in prizes) / TOTAL_WEIGHT_BP


# ── Mở hộp ──────────────────────────────────────────────────────────

_SQL_EVENT_ROW = """
SELECT event_id, created_at, is_active, job_id
  FROM events
 WHERE event_id = :event_id
"""

# Gốc cửa sổ của CHÍNH người bấm. `sent_at` do tầng gửi điền sau khi Telegram xác nhận,
# nên nó là mốc "người này nhìn thấy tin lúc nào" — thứ duy nhất đúng để đo 10 phút.
_SQL_SENT_AT = """
SELECT sent_at
  FROM broadcast_targets
 WHERE job_id = :job_id
   AND user_id = :user_id
"""

_SQL_CLAIM_SLOT = """
INSERT INTO event_participations (user_id, event_id, result, window_source)
     VALUES (:user_id, :event_id, :result, :window_source)
ON CONFLICT (user_id, event_id) DO NOTHING
  RETURNING user_id
"""

_SQL_SET_RESULT = """
UPDATE event_participations
   SET result = :result,
       code_grant_id = :grant_id
 WHERE user_id = :user_id
   AND event_id = :event_id
"""

# Tổng giá trị ĐÃ PHÁT của đợt: cộng trên sổ cái phát hành, không cộng trên một bộ đếm
# riêng. Một bộ đếm tự tăng là thứ đã trôi khỏi sự thật ở hệ cũ, và ở đây nó quyết định
# lúc nào ngừng tiêu tiền.
_SQL_SPENT = """
SELECT COALESCE(SUM(g.value_vnd), 0) AS spent
  FROM event_participations p
  JOIN code_grants g ON g.grant_id = p.code_grant_id
 WHERE p.event_id = :event_id
"""


async def spent_vnd(db: AsyncSession, *, event_id: int) -> int:
    """Tổng giá trị code đã phát trong một đợt."""
    return int((await db.execute(text(_SQL_SPENT), {"event_id": event_id})).scalar_one())


# Khoá tư vấn theo ĐỢT, giữ tới hết giao dịch. Không khoá dòng `events` vì dòng đó còn bị
# đọc bởi những đường không liên quan tới tiền (`window_origin`, bảng xem thử của admin) —
# khoá nó là biến mọi việc đọc thành xếp hàng sau việc phát thưởng.
#
# Hai tham số: `pg_advisory_xact_lock(int, int)` cần một cặp int4. Số đầu là "không gian
# tên" để khoá của đợt event không đụng khoá của thứ khác dùng chung cơ chế này về sau.
_SQL_LOCK_EVENT_BUDGET = "SELECT pg_advisory_xact_lock(:ns, :event_id)"

#: Không gian tên khoá tư vấn của trần ngân sách event. Đổi số này là đổi ý nghĩa khoá.
_LOCK_NS_EVENT_BUDGET: Final = 0x4556  # "EV"


async def _lock_budget(db: AsyncSession, *, event_id: int) -> None:
    """Xếp hàng các lượt SẮP TIÊU TIỀN của cùng một đợt.

    Chỉ đường thắng mới gọi hàm này. Lượt trượt — phần lớn tuyệt đối các lượt — không
    chạm khoá, nên lúc cả nghìn người bấm cùng lúc thì hàng đợi chỉ dài bằng số người
    trúng, không bằng số người bấm.
    """
    await db.execute(
        text(_SQL_LOCK_EVENT_BUDGET),
        {"ns": _LOCK_NS_EVENT_BUDGET, "event_id": event_id},
    )


async def window_origin(
    db: AsyncSession, *, event_id: int, user_id: int
) -> tuple[datetime, str] | None:
    """`(mốc gốc, window_source)` để tính cửa sổ cho **chính người này**.

    Trả `None` khi không có event. `sent_at` chưa có (tin còn trong hàng đợi, hoặc đợt
    chưa gắn job) thì rơi về `events.created_at` — đường dự phòng, và lượt đó được ghi
    `window_source = 'event_created'` để đo được nó xảy ra bao nhiêu lần.
    """
    event = (await db.execute(text(_SQL_EVENT_ROW), {"event_id": event_id})).one_or_none()
    if event is None:
        return None
    return await _origin_of(db, event=event, user_id=user_id)


async def _origin_of(db: AsyncSession, *, event: Any, user_id: int) -> tuple[datetime, str]:
    """Như `window_origin` nhưng nhận sẵn dòng event — `open_box` đã đọc nó rồi."""
    if event.job_id is not None:
        sent_at = (
            await db.execute(text(_SQL_SENT_AT), {"job_id": event.job_id, "user_id": user_id})
        ).scalar_one_or_none()
        if sent_at is not None:
            return sent_at, WINDOW_PER_RECIPIENT
    return event.created_at, WINDOW_EVENT_CREATED


async def open_box(db: AsyncSession, *, event_id: int, user_id: int) -> BoxResult:
    """Một lượt mở hộp. Gọi trong `transaction()`. Xem thứ tự các bước ở docstring module.

    Không ném lỗi cho bất kỳ nhánh nghiệp vụ nào: mọi kết quả — kể cả hết kho và chạm
    trần — là một `BoxResult` để handler chọn câu chữ. Ném chỉ còn dành cho lỗi cấu hình
    (`ConfigError`), thứ mà người dùng không sửa được và handler phải báo "hệ thống bận".
    """
    event = (await db.execute(text(_SQL_EVENT_ROW), {"event_id": event_id})).one_or_none()
    if event is None or not event.is_active:
        return BoxResult(status="closed", reason="event_inactive")

    base, window_source = await _origin_of(db, event=event, user_id=user_id)

    # Giành chỗ TRƯỚC khi quay số. Kết quả tạm là `empty`; các nhánh dưới chỉ UPDATE nó.
    claimed = (
        await db.execute(
            text(_SQL_CLAIM_SLOT),
            {
                "user_id": user_id,
                "event_id": event_id,
                "result": RESULT_EMPTY,
                "window_source": window_source,
            },
        )
    ).scalar_one_or_none()
    if claimed is None:
        return BoxResult(status="already", reason="already_played", window_source=window_source)

    window_minutes = await settings_service.get_int(WINDOW_MINUTES_KEY, db=db)
    if now_utc() > base + timedelta(minutes=window_minutes):
        log.info("dap_hop_ngoai_cua_so", event_id=event_id, user_id=user_id, source=window_source)
        return BoxResult(status="empty", reason="window_closed", window_source=window_source)

    cap_vnd = await settings_service.get_int(BUDGET_CAP_KEY, db=db)
    already_spent = await spent_vnd(db, event_id=event_id)
    if already_spent >= cap_vnd:
        # Quá trần thì mọi lượt còn lại là rỗng — nhưng người dùng phải được nói thẳng lý
        # do (§13.5.1 điểm 3), nên `reason` khác và handler gửi một câu khác hẳn.
        log.warning(
            "dap_hop_cham_tran_ngan_sach", event_id=event_id, spent=already_spent, cap=cap_vnd
        )
        return BoxResult(status="empty", reason="budget_cap", window_source=window_source)

    prize = draw(await prize_table(db))
    if prize.is_empty:
        return BoxResult(status="empty", reason="draw", window_source=window_source)

    # ── Chốt trần, lần này là thật ──────────────────────────────────
    # Lần đọc phía trên chỉ là cửa nhanh cho lượt trượt. Nó KHÔNG chốt được trần: giữa
    # lúc đọc và lúc phát, hàng chục giao dịch khác cùng đọc một con số cũ rồi cùng kết
    # luận "còn ngân sách" — mỗi lượt trúng vượt trần thêm một giải, và bot cũ đã trả tiền
    # thật cho đúng lối này. Khoá tư vấn ép các lượt trúng đi qua đây từng cái một, nên
    # phần vượt trần tối đa là MỘT giải, và là phần vượt cố ý (`>= cap` mới dừng).
    await _lock_budget(db, event_id=event_id)
    already_spent = await spent_vnd(db, event_id=event_id)
    if already_spent >= cap_vnd:
        log.warning(
            "dap_hop_cham_tran_ngan_sach_sau_quay",
            event_id=event_id,
            user_id=user_id,
            spent=already_spent,
            cap=cap_vnd,
            value_vnd=prize.value_vnd,
        )
        return BoxResult(status="empty", reason="budget_cap", window_source=window_source)

    grant = await _reserve_prize(db, event_id=event_id, user_id=user_id, prize=prize)
    if grant is None:
        # Hết kho ĐÚNG mệnh giá vừa trúng. KHÔNG đổi sang mệnh giá khác: người dùng trúng
        # 88k mà nhận 5k là một lời hứa bị bẻ, và sổ sách thì không còn đối chiếu được
        # với bảng tỉ lệ nữa.
        await db.execute(
            text(_SQL_SET_RESULT),
            {
                "user_id": user_id,
                "event_id": event_id,
                "result": RESULT_OUT_OF_STOCK,
                "grant_id": None,
            },
        )
        log.error("dap_hop_het_kho", event_id=event_id, user_id=user_id, value_vnd=prize.value_vnd)
        return BoxResult(
            status="out_of_stock",
            reason="no_stock",
            value_vnd=prize.value_vnd,
            window_source=window_source,
        )

    await db.execute(
        text(_SQL_SET_RESULT),
        {
            "user_id": user_id,
            "event_id": event_id,
            "result": result_label(prize.value_vnd),
            "grant_id": grant.grant_id,
        },
    )
    log.info(
        "dap_hop_trung",
        event_id=event_id,
        user_id=user_id,
        value_vnd=grant.value_vnd,
        grant_id=grant.grant_id,
    )
    return BoxResult(
        status="win",
        reason="won",
        value_vnd=grant.value_vnd,
        code_value=grant.code_value,
        grant_id=grant.grant_id,
        window_source=window_source,
    )


async def _reserve_prize(
    db: AsyncSession, *, event_id: int, user_id: int, prize: Prize
) -> code_issuance.Grant | None:
    """Giữ chỗ một mã cho giải vừa trúng, hoặc `None` khi kho rỗng.

    SAVEPOINT là bắt buộc: `reserve()` ném `OutOfStock` **sau khi** đã chèn dòng
    `code_grants`, và nó trông cậy vào việc giao dịch cuộn lại để dòng đó biến mất. Cuộn
    cả giao dịch ngoài thì mất luôn dòng giành chỗ ở `event_participations` — người dùng
    mở lại được lần hai. SAVEPOINT cuộn đúng phần grant.
    """
    try:
        async with db.begin_nested():
            return await code_issuance.reserve(
                db,
                user_id=user_id,
                grant_type=GRANT_TYPE,
                grant_key=grant_key_event(event_id, user_id),
                code_type=CODE_TYPE,
                value_vnd=prize.value_vnd,
                reason={"event_id": event_id, "weight_bp": prize.weight_bp},
            )
    except OutOfStock:
        return None
    except AlreadyClaimed:
        # Có grant cho khoá này nhưng chưa gắn được mã: lần trước chạm đúng lúc kho rỗng
        # rồi chết giữa chừng. Với người dùng thì đây vẫn là "hết code", không phải "đã
        # nhận rồi" — họ chưa cầm mã nào trong tay.
        log.warning("dap_hop_grant_chua_co_ma", event_id=event_id, user_id=user_id)
        return None


__all__ = [
    "BUDGET_CAP_KEY",
    "CODE_TYPE",
    "EVENT_TYPE",
    "GAME_LINK_KEY",
    "GRANT_TYPE",
    "PRIZE_TABLE_KEY",
    "REQUIRE_FULL_STOCK_KEY",
    "TOTAL_WEIGHT_BP",
    "WINDOW_EVENT_CREATED",
    "WINDOW_MINUTES_KEY",
    "WINDOW_PER_RECIPIENT",
    "WINNING_RESULTS",
    "BoxResult",
    "Prize",
    "StockRow",
    "attach_job",
    "close_event",
    "create_event",
    "draw",
    "event_of_job",
    "expected_cost_vnd",
    "missing_stock",
    "open_box",
    "parse_prize_table",
    "prize_table",
    "render_caption",
    "result_label",
    "spent_vnd",
    "stock_report",
    "window_origin",
]
