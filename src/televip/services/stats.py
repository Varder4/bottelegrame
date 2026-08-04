"""Số liệu tổng quan, số liệu cá nhân và bảng xếp hạng — `13-dac-ta-bot-moi.md` §13.2.5-6.

Một luật chi phối cả file: **màn hình đọc bảng tổng hợp, không tự tổng hợp lại.**

Nút `📊Thống Kê TK` của hệ cũ chạy 5 câu `COUNT(*)`/`SUM()` toàn bảng cho **mỗi lượt bấm**
(`main.py:553-571`), còn nút `👑 BXH` thì `GROUP BY` toàn bảng `referrals`
(`db_manager.py:676-684`). Sau một đợt broadcast, hàng nghìn lượt bấm mỗi phút nghĩa là bot
tự DDoS chính database của mình. Ở đây:

* `system_stats` — đúng một dòng, do job định kỳ ghi lại (`refresh_system_stats`).
  Lượt bấm chỉ đọc một dòng theo khoá chính.
* `user_stats` — một dòng mỗi người, do **các luồng nghiệp vụ** cập nhật khi có sự kiện
  (mời bạn, điểm danh, nhận code). File này chỉ đọc, không bao giờ ghi vào đó.

## Bảng xếp hạng theo ngày: cắt mốc bằng khoảng, không bằng hàm

Ngày nghiệp vụ là ngày **giờ Việt Nam** (`core/clock.py`), còn `created_at` lưu UTC. Cách
duy nhất được dùng ở đây là quy ngày VN về một khoảng UTC bằng `vn_day_bounds()` rồi lọc::

    WHERE created_at >= :day_start AND created_at < :day_end   -- dùng được index

**Không** dùng `WHERE date(created_at) = :day`: bọc cột trong một lời gọi hàm là vứt bỏ
index và quét toàn bảng — thứ đúng ra cả file này sinh ra để tránh. `checkins` là ngoại lệ
duy nhất và cũng không phải ngoại lệ thật: nó đã có sẵn cột `business_date` kiểu `DATE`,
nên so bằng trực tiếp trên chính cột đó.

Riêng bảng `👑 BXH` **có thật hai chế độ**. Bot cũ dán nhãn "BXH hôm nay" nhưng cả ba truy
vấn đều không có một điều kiện thời gian nào — đó là bảng toàn thời gian đội lốt bảng ngày
(§13.5 dòng B).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import business_date, now_utc, vn_day_bounds
from televip.core.logging import get_logger
from televip.domain import texts
from televip.services import settings_service

log = get_logger(__name__)

# ── Khoá cấu hình ───────────────────────────────────────────────────
# Hằng `DEFAULT_*` chỉ là đường lui khi bảng `settings` chưa được seed (bản cài mới,
# database test) — giá trị chạy thật nằm ở migration `0010`.

#: Chu kỳ job làm mới `system_stats` (giây).
REFRESH_SECONDS_KEY: Final = "stats.refresh_seconds"
DEFAULT_REFRESH_SECONDS: Final = 60

#: Ảnh chụp cũ hơn mức này thì lượt bấm tự tính lại. Đây là **lưới an toàn cho trường hợp
#: job chết**, không phải đường chạy bình thường: đặt bằng đúng chu kỳ job sẽ biến mỗi mốc
#: hết hạn thành một cơn bão tính lại từ mọi lượt bấm đang chờ.
MAX_AGE_KEY: Final = "stats.max_age_seconds"
DEFAULT_MAX_AGE_SECONDS: Final = 300

#: Số dòng mỗi khối bảng xếp hạng.
TOP_N_KEY: Final = "leaderboard.top_n"
DEFAULT_TOP_N: Final = 3


# ── Kiểu trả về ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """Phần "THỐNG KÊ TỔNG QUAN HỆ THỐNG" của màn hình §13.2.5."""

    total_users: int
    codes_available: int
    codes_issued: int
    total_referrals: int
    total_value_vnd: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserSnapshot:
    """Phần "THỐNG KÊ CÁ NHÂN" của cùng màn hình.

    `rank_today` / `rank_alltime` là `None` khi người này chưa có hoạt động nào được xếp
    hạng — §13.2.5 buộc in `Chưa xếp hạng`, không phải số 0 và **không phải** chuỗi cứng
    "Chưa có" như bot cũ (chuỗi đó không bao giờ có giá trị thật).
    """

    user_id: int
    refs_total: int
    refs_qualified: int
    value_total: int
    checkin_streak: int
    codes_received: int
    value_received: int
    rank_today: int | None
    rank_alltime: int | None


#: Một dòng bảng xếp hạng: tên đã dựng bằng `texts.display_name` + con số của khối đó.
Entry = tuple[str, int]


@dataclass(frozen=True, slots=True)
class Leaderboard:
    """Ba khối của màn hình §13.2.6, cho **một** chế độ."""

    today: bool
    top_referrers: tuple[Entry, ...]
    top_receivers: tuple[Entry, ...]
    top_streaks: tuple[Entry, ...]
    updated_at: datetime


# ── system_stats ────────────────────────────────────────────────────

_SQL_READ_SYSTEM = """
SELECT total_users, codes_available, codes_issued, total_referrals, total_value_vnd,
       updated_at
  FROM system_stats
 WHERE id = 1
"""

#: Định nghĩa của năm con số này phải trùng `handlers/admin/ops.py: _SQL_STATS_DIRECT` —
#: `/stats` của admin và màn hình của người dùng mà đếm khác nhau thì một trong hai sai,
#: và không có cách nào biết cái nào.
_SQL_REFRESH_SYSTEM = """
INSERT INTO system_stats AS s
       (id, total_users, codes_available, codes_issued, total_referrals, total_value_vnd,
        updated_at)
SELECT 1,
       (SELECT count(*) FROM users),
       (SELECT count(*) FROM codes WHERE status = 'available'),
       (SELECT count(*) FROM code_grants WHERE state = 'delivered'),
       (SELECT count(*) FROM referrals WHERE qualified_at IS NOT NULL),
       -- `sum(bigint)` trả NUMERIC trong Postgres; ép về bigint để tầng hiển thị nhận
       -- đúng một số nguyên thay vì Decimal.
       (SELECT coalesce(sum(value_vnd), 0)::bigint
          FROM code_grants WHERE state = 'delivered'),
       now()
ON CONFLICT (id) DO UPDATE
   SET total_users     = EXCLUDED.total_users,
       codes_available = EXCLUDED.codes_available,
       codes_issued    = EXCLUDED.codes_issued,
       total_referrals = EXCLUDED.total_referrals,
       total_value_vnd = EXCLUDED.total_value_vnd,
       -- `onupdate` của mixin là luật phía ORM; câu lệnh thô phải tự đặt mốc, nếu không
       -- dòng được cập nhật mà `updated_at` đứng yên và nó luôn bị coi là quá cũ.
       updated_at      = now()
RETURNING total_users, codes_available, codes_issued, total_referrals, total_value_vnd,
          updated_at
"""


async def refresh_system_stats(db: AsyncSession) -> SystemSnapshot:
    """Tính lại toàn bộ `system_stats` và ghi đè dòng duy nhất. Dành cho job định kỳ.

    Đây là **nơi duy nhất** ghi vào bảng đó, nên không có tranh chấp nào ngoài hai lần
    chạy chồng lên nhau của chính job — và `ON CONFLICT DO UPDATE` giải quyết việc đó.
    """
    row = (await db.execute(text(_SQL_REFRESH_SYSTEM))).one()
    return SystemSnapshot(
        total_users=row.total_users,
        codes_available=row.codes_available,
        codes_issued=row.codes_issued,
        total_referrals=row.total_referrals,
        total_value_vnd=row.total_value_vnd,
        updated_at=row.updated_at,
    )


#: `system_stats` không có cột "đã xác minh", nên con số này luôn tính trực tiếp. Rẻ: một
#: `count(*)` có điều kiện trên một cột đã đánh index, và nó chỉ chạy ở màn hình quản trị.
_SQL_VERIFIED_USERS = "SELECT count(*) FROM users WHERE verified_at IS NOT NULL"


async def verified_count(db: AsyncSession) -> int:
    """Số người đã xác minh. Luôn LIVE — không có trong ảnh chụp `system_stats`."""
    return int((await db.execute(text(_SQL_VERIFIED_USERS))).scalar_one())


async def system_snapshot(db: AsyncSession) -> SystemSnapshot:
    """Số liệu tổng quan. Đọc bảng tổng hợp; **quá cũ hoặc chưa có thì tự tính lại**.

    ``db`` phải là session ghi được (`db.engine.transaction()`): nhánh tính lại có ghi.
    Nhánh đó chỉ chạy khi job làm mới đã chết lâu hơn `stats.max_age_seconds` — bot hiện
    số liệu cũ hàng giờ mà không ai biết còn tệ hơn một truy vấn nặng mỗi 5 phút.
    """
    row = (await db.execute(text(_SQL_READ_SYSTEM))).one_or_none()
    if row is None:
        return await refresh_system_stats(db)

    max_age = await settings_service.get_int(MAX_AGE_KEY, DEFAULT_MAX_AGE_SECONDS, db=db)
    age_seconds = (now_utc() - row.updated_at).total_seconds()
    if age_seconds > max_age:
        log.warning("system_stats_qua_cu", tuoi_giay=int(age_seconds), tran_giay=max_age)
        return await refresh_system_stats(db)

    return SystemSnapshot(
        total_users=row.total_users,
        codes_available=row.codes_available,
        codes_issued=row.codes_issued,
        total_referrals=row.total_referrals,
        total_value_vnd=row.total_value_vnd,
        updated_at=row.updated_at,
    )


# ── user_stats ──────────────────────────────────────────────────────

_SQL_READ_USER = """
SELECT coalesce(s.refs_total, 0)      AS refs_total,
       coalesce(s.refs_qualified, 0)  AS refs_qualified,
       coalesce(s.value_total, 0)     AS value_total,
       coalesce(s.checkin_streak, 0)  AS checkin_streak,
       u.total_codes_received,
       u.total_value_received
  FROM users u
  LEFT JOIN user_stats s ON s.user_id = u.user_id
 WHERE u.user_id = :uid
"""

#: Hạng toàn thời gian tính theo `refs_total` — **cùng con số với khối `🏆 TOP MỜI BẠN BÈ`**
#: của bảng xếp hạng. Hai chỗ nói về "hạng" mà xếp theo hai thước đo khác nhau là cách chắc
#: chắn nhất để người dùng thấy mình hạng 4 trong khi bảng xếp hạng in tên họ ở hạng 2.
_SQL_RANK_ALLTIME = "SELECT count(*) + 1 AS rank FROM user_stats WHERE refs_total > :mine"

_SQL_RANK_TODAY = """
WITH per_user AS (
    SELECT referrer_id, count(*) AS n
      FROM referrals
     WHERE created_at >= :day_start AND created_at < :day_end
     GROUP BY referrer_id
)
SELECT (SELECT count(*) FROM per_user other WHERE other.n > me.n) + 1 AS rank
  FROM per_user me
 WHERE me.referrer_id = :uid
"""


async def user_snapshot(db: AsyncSession, user_id: int, *, day: date | None = None) -> UserSnapshot:
    """Số liệu cá nhân + hai thứ hạng.

    Người chưa từng `/start` (hoặc chưa có dòng `user_stats`) trả về toàn số 0 và hai hạng
    `None` — §13.2.5 nói rõ trường hợp này **không được ném lỗi**.

    Hai con số "đã nhận" lấy từ `users`, nơi `code_issuance.mark_delivered()` cộng vào; hai
    con số về mời bạn lấy từ `user_stats`. Cố ý không đọc chéo: mỗi bảng có đúng một chủ sở
    hữu ghi vào nó.
    """
    row = (await db.execute(text(_SQL_READ_USER), {"uid": user_id})).one_or_none()
    if row is None:
        return UserSnapshot(
            user_id=user_id,
            refs_total=0,
            refs_qualified=0,
            value_total=0,
            checkin_streak=0,
            codes_received=0,
            value_received=0,
            rank_today=None,
            rank_alltime=None,
        )

    return UserSnapshot(
        user_id=user_id,
        refs_total=row.refs_total,
        refs_qualified=row.refs_qualified,
        value_total=row.value_total,
        checkin_streak=row.checkin_streak,
        codes_received=row.total_codes_received,
        value_received=row.total_value_received,
        rank_today=await _rank_today(db, user_id, day=day),
        rank_alltime=await _rank_alltime(db, row.refs_total),
    )


async def _rank_alltime(db: AsyncSession, refs_total: int) -> int | None:
    # Chưa mời được ai thì không có hạng. Nếu không, mọi tài khoản mới toanh đều "đồng
    # hạng cuối" và con số đó chỉ nói lên hệ thống có bao nhiêu người.
    if refs_total <= 0:
        return None
    return (await db.execute(text(_SQL_RANK_ALLTIME), {"mine": refs_total})).scalar_one()


async def _rank_today(db: AsyncSession, user_id: int, *, day: date | None) -> int | None:
    start, end = vn_day_bounds(day or business_date())
    return (
        await db.execute(
            text(_SQL_RANK_TODAY), {"uid": user_id, "day_start": start, "day_end": end}
        )
    ).scalar_one_or_none()


# ── Bảng xếp hạng ───────────────────────────────────────────────────

_SQL_TOP_REFERRERS_TODAY = """
SELECT u.username, u.full_name, t.n AS value
  FROM (SELECT referrer_id, count(*) AS n
          FROM referrals
         WHERE created_at >= :day_start AND created_at < :day_end
         GROUP BY referrer_id
         ORDER BY n DESC, referrer_id
         LIMIT :lim) t
  JOIN users u ON u.user_id = t.referrer_id
 ORDER BY t.n DESC, u.user_id
"""

_SQL_TOP_RECEIVERS_TODAY = """
SELECT u.username, u.full_name, t.total AS value
  FROM (SELECT user_id, sum(value_vnd)::bigint AS total
          FROM code_grants
         WHERE state = 'delivered'
           AND created_at >= :day_start AND created_at < :day_end
         GROUP BY user_id
         ORDER BY total DESC, user_id
         LIMIT :lim) t
  JOIN users u ON u.user_id = t.user_id
 ORDER BY t.total DESC, u.user_id
"""

#: `business_date` là cột `DATE` sẵn có, so bằng trực tiếp — không có múi giờ nào để lệch
#: và cũng không có hàm nào bọc quanh cột.
_SQL_TOP_STREAKS_TODAY = """
SELECT u.username, u.full_name, c.streak_after AS value
  FROM checkins c
  JOIN users u ON u.user_id = c.user_id
 WHERE c.business_date = :day
 ORDER BY c.streak_after DESC, c.user_id
 LIMIT :lim
"""


#: Ba cột duy nhất được phép cắm vào `_sql_top_alltime` — khớp ba index `DESC` của bảng.
_ALLTIME_COLUMNS: Final[frozenset[str]] = frozenset({"refs_total", "value_total", "checkin_streak"})


def _sql_top_alltime(column: str) -> str:
    """Ba khối toàn thời gian chỉ khác nhau đúng một tên cột của `user_stats`.

    `column` là hằng trong file này, **không bao giờ** đến từ người dùng — và lời kiểm
    dưới đây giữ nguyên bất biến đó nếu có ai thêm lời gọi thứ tư. Ba index `DESC` của
    bảng phục vụ đúng ba câu này, nên mỗi khối chỉ đọc vài dòng đầu.
    """
    if column not in _ALLTIME_COLUMNS:  # pragma: no cover - chặn lỗi lập trình, không phải input
        raise ValueError(f"cột xếp hạng không hợp lệ: {column!r}")
    # ruff S608: câu lệnh dựng bằng f-string, nhưng phần thay đổi vừa bị chặn ở dòng trên.
    sql = f"""
SELECT u.username, u.full_name, s.{column} AS value
  FROM user_stats s
  JOIN users u ON u.user_id = s.user_id
 WHERE s.{column} > 0
 ORDER BY s.{column} DESC, s.user_id
 LIMIT :lim
"""  # noqa: S608
    return sql


_SQL_TOP_REFERRERS_ALLTIME = _sql_top_alltime("refs_total")
_SQL_TOP_RECEIVERS_ALLTIME = _sql_top_alltime("value_total")
_SQL_TOP_STREAKS_ALLTIME = _sql_top_alltime("checkin_streak")


async def top_n() -> int:
    """Số dòng mỗi khối, theo `settings.leaderboard.top_n`."""
    return await settings_service.get_int(TOP_N_KEY, DEFAULT_TOP_N)


async def leaderboard(
    db: AsyncSession, *, today: bool, limit: int, day: date | None = None
) -> Leaderboard:
    """Ba khối xếp hạng cho một chế độ. Chỉ đọc.

    Chế độ `today` tính trực tiếp trên dữ liệu của **ngày nghiệp vụ giờ VN**; chế độ toàn
    thời gian đọc bảng tổng hợp `user_stats`. Khối rỗng trả về tuple rỗng — tầng hiển thị
    in `Chưa có dữ liệu` cho đúng khối đó và hai khối còn lại vẫn hiện (§13.2.6).
    """
    if today:
        business_day = day or business_date()
        start, end = vn_day_bounds(business_day)
        window = {"day_start": start, "day_end": end, "lim": limit}
        return Leaderboard(
            today=True,
            top_referrers=await _entries(db, _SQL_TOP_REFERRERS_TODAY, window),
            top_receivers=await _entries(db, _SQL_TOP_RECEIVERS_TODAY, window),
            top_streaks=await _entries(
                db, _SQL_TOP_STREAKS_TODAY, {"day": business_day, "lim": limit}
            ),
            updated_at=now_utc(),
        )

    params = {"lim": limit}
    return Leaderboard(
        today=False,
        top_referrers=await _entries(db, _SQL_TOP_REFERRERS_ALLTIME, params),
        top_receivers=await _entries(db, _SQL_TOP_RECEIVERS_ALLTIME, params),
        top_streaks=await _entries(db, _SQL_TOP_STREAKS_ALLTIME, params),
        updated_at=now_utc(),
    )


async def _entries(db: AsyncSession, sql: str, params: dict[str, object]) -> tuple[Entry, ...]:
    rows = (await db.execute(text(sql), params)).all()
    return tuple((texts.display_name(row.username, row.full_name), row.value) for row in rows)


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_REFRESH_SECONDS",
    "DEFAULT_TOP_N",
    "MAX_AGE_KEY",
    "REFRESH_SECONDS_KEY",
    "TOP_N_KEY",
    "Entry",
    "Leaderboard",
    "SystemSnapshot",
    "UserSnapshot",
    "leaderboard",
    "refresh_system_stats",
    "refresh_user_stats",
    "system_snapshot",
    "top_n",
    "user_snapshot",
]


# ══════════════════════════════════════════════════════════════════════
# user_stats — bảng tổng hợp theo từng người
# ══════════════════════════════════════════════════════════════════════

#: Tính lại toàn bộ `user_stats` từ các bảng nguồn.
#:
#: Bảng này từng KHÔNG có ai ghi vào: docstring hứa "các luồng nghiệp vụ cập nhật khi có
#: sự kiện" nhưng không luồng nào làm, nên `📊Thống Kê TK` in "0 người đã mời" cho mọi
#: người và `👑 BXH` toàn thời gian rỗng vĩnh viễn — kể cả với người đã mời 50 bạn thật.
#:
#: Chọn cách tính lại toàn bộ theo chu kỳ thay vì cộng dần ở từng luồng, vì hai lý do:
#: nó **tự sửa** mọi sai lệch tích luỹ (cộng dần mà sót một nhánh là lệch vĩnh viễn), và
#: nó giữ ba luồng nghiệp vụ khỏi phải nhớ cập nhật một bảng thứ tư trong transaction của
#: mình. Đánh đổi là số liệu trễ đúng một chu kỳ — chấp nhận được với bảng xếp hạng.
_SQL_REFRESH_USER_STATS = """
INSERT INTO user_stats (user_id, refs_total, refs_qualified, value_total, checkin_streak,
                        updated_at)
SELECT u.user_id,
       COALESCE(r.total, 0),
       COALESCE(r.qualified, 0),
       COALESCE(u.total_value_received, 0),
       COALESCE(u.checkin_streak, 0),
       now()
  FROM users u
  LEFT JOIN (
       SELECT referrer_id,
              count(*)                                        AS total,
              count(*) FILTER (WHERE qualified_at IS NOT NULL) AS qualified
         FROM referrals
        GROUP BY referrer_id
  ) r ON r.referrer_id = u.user_id
 WHERE COALESCE(r.total, 0) > 0
    OR COALESCE(u.total_value_received, 0) > 0
    OR COALESCE(u.checkin_streak, 0) > 0
    ON CONFLICT (user_id) DO UPDATE
   SET refs_total     = EXCLUDED.refs_total,
       refs_qualified = EXCLUDED.refs_qualified,
       value_total    = EXCLUDED.value_total,
       checkin_streak = EXCLUDED.checkin_streak,
       updated_at     = EXCLUDED.updated_at
"""


async def refresh_user_stats(db: AsyncSession) -> int:
    """Tính lại `user_stats`. Trả về số dòng đã ghi. Dành cho job định kỳ.

    Chỉ ghi những người có ít nhất một con số khác 0 — bảng này phục vụ bảng xếp hạng và
    màn hình hồ sơ, nên một dòng toàn số 0 chỉ làm nó phình ra mà không trả lời câu hỏi nào.
    """
    result = await db.execute(text(_SQL_REFRESH_USER_STATS))
    return result.rowcount or 0
