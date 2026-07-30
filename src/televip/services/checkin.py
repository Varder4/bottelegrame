"""Điểm danh và đổi điểm lấy code — §13.2.7 và §13.2.8.

Ba hàng rào của khối này đều nằm ở tầng database, không ở tầng Python:

1. **`checkins` có khoá chính tổ hợp `(user_id, business_date)`.** Đó *là* luật "một ngày
   một lần". Hệ cũ kiểm bằng `if last_checkin_date == today` trong Python rồi mới UPDATE,
   nên bấm nhanh hai lần cộng 4.000đ.
2. **`points_ledger` có `UNIQUE (user_id, reason, ref_key)`.** `ref_key` tất định theo ngày
   nghiệp vụ, nên bút toán điểm danh của một ngày không bao giờ ghi được hai lần — kể cả
   khi hàng rào 1 bị một đường ghi khác đi vòng qua.
3. **Trừ điểm là phép TƯƠNG ĐỐI CÓ ĐIỀU KIỆN** (`SET points_balance = points_balance - $1
   WHERE points_balance >= $1`), không bao giờ ghi giá trị tuyệt đối đọc trước đó. Hệ cũ
   đọc số dư rồi ghi đè (`db_manager.py:804-836`): hai lượt bấm song song cùng đọc 20.000,
   mỗi bên trừ 10.000 rồi cùng ghi 10.000 → lấy hai code mà chỉ mất 10.000 điểm.

## Ngày nghiệp vụ theo giờ Việt Nam

Mọi khái niệm "ngày" ở đây đi qua `core.clock.business_date()`, tức
`(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`. Hệ cũ dùng `datetime.now().date()` theo
giờ máy chủ; VPS chạy UTC nên "ngày mới" của điểm danh bắt đầu lúc **7 giờ sáng giờ VN**,
và người điểm danh lúc 23h30 bị tính sang hôm sau, đứt streak vô cớ (§13.5, dòng H).

## Số dư: sổ cái là nguồn sự thật

`balance()` cộng từ `points_ledger`. `users.points_balance` là **bản tổng hợp để hiển thị
và để làm hàng rào nguyên tử lúc trừ** — nó không bao giờ được đọc ra làm câu trả lời cho
"người này có bao nhiêu điểm". Hai chỗ đó chỉ trùng nhau khi mọi lần cộng/trừ đều đi qua
đúng module này, nên module này giữ cả hai trong **cùng một** transaction, không bao giờ
cập nhật một cái mà bỏ cái kia.

## Một lượt đổi cho mỗi người mỗi ngày

`grant_key` của lượt đổi là `redeem:{user_id}:{ngày nghiệp vụ}` (`core.ids`), và `ref_key`
của bút toán trừ dùng đúng chuỗi đó. Hệ quả: **mỗi người đổi được tối đa một lần mỗi ngày
nghiệp vụ**, và lần bấm thứ hai trong ngày trả lại đúng mã cũ thay vì trừ điểm lần nữa.
Đây là cái giá của việc `grant_key` phải tất định — 03-code.md §3.3 cấm nhét timestamp vào
khoá vì làm vậy là vô hiệu hoá toàn bộ cơ chế idempotency. Ràng buộc này không cắt ai:
điểm danh cho 2.000đ/ngày còn bậc thấp nhất là 10.000đ, nên nhịp đổi thực tế là 5 ngày một
lần.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import business_date, is_consecutive_day
from televip.core.errors import ConfigError, TelevipError
from televip.core.ids import grant_key_checkin_redeem
from televip.core.logging import get_logger
from televip.db.models.engagement import Checkin, PointsLedgerEntry
from televip.db.models.identity import User
from televip.services import code_issuance, settings_service
from televip.services.code_issuance import Grant

log = get_logger(__name__)

#: Loại code trong KHO cho luồng đổi điểm (`db/models/codes.py: CODE_TYPES`).
CODE_TYPE = "diemdanh"
#: Loại phát trong SỔ CÁI (`GRANT_TYPES`). Khác tên với `CODE_TYPE` một cách cố ý.
GRANT_TYPE = "points_redeem"

#: Giá trị của `points_ledger.reason`; phải nằm trong CHECK `reason_valid` của bảng.
REASON_CHECKIN = "checkin"
REASON_REDEEM = "redeem"

# Default chỉ là lưới an toàn khi bảng `settings` chưa seed (test dựng bảng rỗng). Giá trị
# đang chạy luôn đọc từ `settings_service` — §13.6.4.
DEFAULT_POINTS_PER_DAY = 2_000
DEFAULT_TIERS: tuple[int, ...] = (10_000, 20_000, 50_000)
DEFAULT_RESET_STREAK_ON_MISS = True


class NotEnoughPoints(TelevipError):
    """Số dư không đủ cho bậc muốn đổi. **Không có gì bị trừ** khi lỗi này được ném.

    Định nghĩa tại chỗ thay vì thêm vào `core/errors.py` cùng lý do với
    `text_service.TemplateError`: nó chỉ có nghĩa với luồng đổi điểm, và tầng handler bắt
    nó ngay tại nơi gọi.
    """

    def __init__(self, *, balance: int, required: int) -> None:
        self.balance = balance
        self.required = required
        super().__init__(f"Không đủ điểm: có {balance}, cần {required}")


class InvalidRedeemTier(TelevipError):
    """Mệnh giá không nằm trong `settings.redeem.tiers`.

    Đường tới đây là `callback_data` bịa tay (`redeem_999999999`) — Telegram cho phép gửi
    bất kỳ chuỗi nào, nên bậc phải được kiểm lại ở server chứ không tin bàn phím đã gửi đi.
    """

    def __init__(self, value_vnd: int, tiers: list[int]) -> None:
        self.value_vnd = value_vnd
        self.tiers = tiers
        super().__init__(f"Bậc đổi điểm không hợp lệ: {value_vnd} (hợp lệ: {tiers})")


@dataclass(frozen=True, slots=True)
class CheckinResult:
    """Kết quả một lượt bấm `✅ Điểm Danh`.

    `already=True` là một nhánh kịch bản **bình thường** (bấm lần hai trong ngày), không
    phải lỗi — nên nó là một trường của kết quả chứ không phải một exception.
    """

    business_day: date
    already: bool
    #: Điểm cộng ở CHÍNH lượt này. Luôn 0 khi `already`.
    points: int
    balance: int
    streak: int


# ── Cấu hình ────────────────────────────────────────────────────────


async def points_per_day(*, db: AsyncSession | None = None) -> int:
    """Điểm mỗi lần điểm danh (`settings.checkin.points_per_day`).

    Ném:
        ConfigError: giá trị ≤ 0. `checkins` có `CHECK (points_delta > 0)`, nên số 0 làm
            **mọi** lượt điểm danh nổ giữa đường; chặn ở đây để lỗi chỉ ra đúng khoá cấu
            hình bị gõ nhầm thay vì một `IntegrityError` không nói gì.
    """
    points = await settings_service.get_int("checkin.points_per_day", DEFAULT_POINTS_PER_DAY, db=db)
    if points <= 0:
        raise ConfigError(
            f"settings['checkin.points_per_day'] = {points}; phải > 0 vì bảng checkins có "
            "CHECK (points_delta > 0)"
        )
    return points


async def redeem_tiers(*, db: AsyncSession | None = None) -> list[int]:
    """Các bậc đổi điểm (`settings.redeem.tiers`), sắp tăng dần, không trùng.

    Ném:
        ConfigError: danh sách rỗng hoặc có phần tử không phải số nguyên dương.
    """
    raw = await settings_service.get_list("redeem.tiers", list(DEFAULT_TIERS), db=db)
    bad = [v for v in raw if isinstance(v, bool) or not isinstance(v, int) or v <= 0]
    if bad or not raw:
        raise ConfigError(
            f"settings['redeem.tiers'] phải là danh sách số nguyên dương, đang là {raw!r}"
        )
    return sorted(set(raw))


async def reset_streak_on_miss(*, db: AsyncSession | None = None) -> bool:
    return await settings_service.get_bool(
        "checkin.reset_streak_on_miss", DEFAULT_RESET_STREAK_ON_MISS, db=db
    )


# ── Đọc ─────────────────────────────────────────────────────────────


async def balance(db: AsyncSession, user_id: int) -> int:
    """Số dư điểm, cộng từ `points_ledger`.

    Cố ý KHÔNG đọc `users.points_balance`: cột đó là bộ nhớ đệm hiển thị, và một khi nó
    trôi khỏi sổ cái thì mọi con số suy ra từ nó đều sai mà không có gì báo động.
    """
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(PointsLedgerEntry.delta), 0)).where(
                    PointsLedgerEntry.user_id == user_id
                )
            )
        ).scalar_one()
    )


async def current_streak(db: AsyncSession, user_id: int) -> int:
    """Chuỗi ngày liên tiếp đang giữ, đọc từ lượt điểm danh gần nhất."""
    row = await _last_checkin(db, user_id)
    return row.streak_after if row is not None else 0


# ── Điểm danh ───────────────────────────────────────────────────────


async def do_checkin(db: AsyncSession, user_id: int) -> CheckinResult:
    """Một lượt điểm danh. Gọi trong `transaction()`.

    Ngày nghiệp vụ theo giờ Việt Nam; bấm lần thứ hai trong cùng ngày trả về
    `already=True` và **không** cộng điểm.
    """
    day = business_date()
    points = await points_per_day(db=db)

    previous = await _last_checkin(db, user_id)
    if previous is not None and previous.business_date == day:
        # Đường nhanh: đã điểm danh hôm nay. Vẫn đọc số dư để màn hình nói đúng con số.
        return CheckinResult(
            business_day=day,
            already=True,
            points=0,
            balance=await balance(db, user_id),
            streak=previous.streak_after,
        )

    streak = _next_streak(
        previous_day=previous.business_date if previous else None,
        previous_streak=previous.streak_after if previous else 0,
        today=day,
        reset_on_miss=await reset_streak_on_miss(db=db),
    )

    # Hàng rào 1. `ON CONFLICT DO NOTHING` + `RETURNING`: hai lượt bấm cách nhau một phần
    # nghìn giây đều tới được đây, nhưng chỉ một lượt nhận về dòng dữ liệu.
    inserted = (
        await db.execute(
            pg_insert(Checkin)
            .values(
                user_id=user_id,
                business_date=day,
                points_delta=points,
                streak_after=streak,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "business_date"])
            .returning(Checkin.streak_after)
        )
    ).scalar_one_or_none()

    if inserted is None:
        # Lượt thua trong cuộc đua. Đọc lại dòng của người thắng để hiển thị đúng streak.
        row = await _last_checkin(db, user_id)
        return CheckinResult(
            business_day=day,
            already=True,
            points=0,
            balance=await balance(db, user_id),
            streak=row.streak_after if row is not None else streak,
        )

    # Hàng rào 2. `ref_key` là ngày nghiệp vụ, tất định — cùng ngày không ghi được hai lần.
    ledger_id = (
        await db.execute(
            pg_insert(PointsLedgerEntry)
            .values(
                user_id=user_id,
                delta=points,
                reason=REASON_CHECKIN,
                ref_key=day.isoformat(),
            )
            .on_conflict_do_nothing(index_elements=["user_id", "reason", "ref_key"])
            .returning(PointsLedgerEntry.entry_id)
        )
    ).scalar_one_or_none()

    # Bộ đệm hiển thị chỉ được cộng khi sổ cái THẬT SỰ có thêm một dòng. Cộng vô điều kiện
    # ở đây là cách hai con số bắt đầu trôi khỏi nhau, và không ai phát hiện ra.
    values: dict[str, Any] = {"checkin_streak": streak, "last_checkin_date": day}
    if ledger_id is not None:
        values["points_balance"] = User.points_balance + points
    else:
        log.warning("but_toan_diem_danh_da_ton_tai", user_id=user_id, business_date=day.isoformat())
    await db.execute(update(User).where(User.user_id == user_id).values(**values))

    return CheckinResult(
        business_day=day,
        already=False,
        points=points if ledger_id is not None else 0,
        balance=await balance(db, user_id),
        streak=streak,
    )


def _next_streak(
    *,
    previous_day: date | None,
    previous_streak: int,
    today: date,
    reset_on_miss: bool,
) -> int:
    """Streak sau lượt điểm danh hôm nay.

    Liền ngày thì +1. Đứt quãng thì về 1, trừ khi `checkin.reset_streak_on_miss = false` —
    khoá đó tồn tại để chủ bot nới luật mà không phải sửa code (§13.6.4).
    """
    if previous_day is None:
        return 1
    if is_consecutive_day(previous_day, today):
        return previous_streak + 1
    return 1 if reset_on_miss else previous_streak + 1


async def _last_checkin(db: AsyncSession, user_id: int) -> Any:
    """Lượt điểm danh gần nhất (ngày + streak), hoặc None."""
    return (
        await db.execute(
            select(Checkin.business_date, Checkin.streak_after)
            .where(Checkin.user_id == user_id)
            .order_by(Checkin.business_date.desc())
            .limit(1)
        )
    ).one_or_none()


# ── Đổi điểm lấy code ───────────────────────────────────────────────


async def redeem(db: AsyncSession, *, user_id: int, value_vnd: int) -> Grant:
    """Đổi điểm lấy một mã. Gọi trong `transaction()`.

    Ba bước trong **một** giao dịch: kiểm đủ điểm và trừ (nguyên tử) → ghi bút toán trừ →
    giữ chỗ một mã. Bước ba hỏng (hết kho) thì cả giao dịch cuộn lại và **điểm không bị
    trừ** — đó là lý do ba bước phải nằm chung một giao dịch chứ không phải ba lời gọi.

    Ném:
        InvalidRedeemTier: mệnh giá không có trong `settings.redeem.tiers`.
        NotEnoughPoints: số dư không đủ; không có gì bị trừ.
        OutOfStock: kho hết mã `(diemdanh, value_vnd)`; không có gì bị trừ.
    """
    tiers = await redeem_tiers(db=db)
    if value_vnd not in tiers:
        raise InvalidRedeemTier(value_vnd, tiers)

    day = business_date()
    key = grant_key_checkin_redeem(user_id, day.isoformat())

    charged_before = (
        await db.execute(
            select(PointsLedgerEntry.entry_id).where(
                PointsLedgerEntry.user_id == user_id,
                PointsLedgerEntry.reason == REASON_REDEEM,
                PointsLedgerEntry.ref_key == key,
            )
        )
    ).scalar_one_or_none()

    if charged_before is None:
        # Trừ điểm CÓ ĐIỀU KIỆN: `rowcount = 0` nghĩa là không đủ điểm VÀ không có gì bị
        # trừ. Đọc số dư ra rồi so trong Python là đúng chỗ hệ cũ mất tiền.
        remaining = (
            await db.execute(
                update(User)
                .where(User.user_id == user_id, User.points_balance >= value_vnd)
                .values(points_balance=User.points_balance - value_vnd)
                .returning(User.points_balance)
            )
        ).scalar_one_or_none()
        if remaining is None:
            raise NotEnoughPoints(balance=await balance(db, user_id), required=value_vnd)

        db.add(
            PointsLedgerEntry(
                user_id=user_id,
                delta=-value_vnd,
                reason=REASON_REDEEM,
                ref_key=key,
            )
        )
        # Flush ngay: bút toán phải nằm trong database TRƯỚC khi `reserve()` chạm vào kho,
        # để `UNIQUE (user_id, reason, ref_key)` chặn được lượt song song ngay tại đây.
        await db.flush()

    grant = await code_issuance.reserve(
        db,
        user_id=user_id,
        grant_type=GRANT_TYPE,
        grant_key=key,
        code_type=CODE_TYPE,
        value_vnd=value_vnd,
        reason={"business_date": day.isoformat()},
    )

    if grant.was_existing and grant.value_vnd != value_vnd:
        # Đã đổi bậc khác trong cùng ngày nghiệp vụ. Trả lại đúng mã cũ (không trừ thêm
        # điểm, không phát thêm mã) — xem "Một lượt đổi cho mỗi người mỗi ngày" ở đầu file.
        log.info(
            "doi_code_lap_trong_ngay",
            user_id=user_id,
            requested_vnd=value_vnd,
            granted_vnd=grant.value_vnd,
        )

    return grant


__all__ = [
    "CODE_TYPE",
    "DEFAULT_POINTS_PER_DAY",
    "DEFAULT_RESET_STREAK_ON_MISS",
    "DEFAULT_TIERS",
    "GRANT_TYPE",
    "REASON_CHECKIN",
    "REASON_REDEEM",
    "CheckinResult",
    "InvalidRedeemTier",
    "NotEnoughPoints",
    "balance",
    "current_streak",
    "do_checkin",
    "points_per_day",
    "redeem",
    "redeem_tiers",
    "reset_streak_on_miss",
]
