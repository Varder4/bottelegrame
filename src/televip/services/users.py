"""Ghi nhận người dùng, ý định giới thiệu, và các truy vấn tra cứu của quản trị.

Hai thao tác ghi ở đây đều phải idempotent: Telegram có thể gửi lại cùng một update, và
người dùng bấm `/start` bao nhiêu lần tuỳ thích.

## Phần tra cứu quản trị (`resolve_user`, `admin_profile`, `recent_users`, `user_totals`)

Bốn hàm đó vốn là bốn hằng chuỗi SQL nằm trong `apps/worker/handlers/admin/ops.py`. Chúng
đứng yên được vì chỉ có MỘT màn hình đọc chúng. Panel web là màn hình thứ hai, và hai màn
hình cùng đọc một con số qua hai đoạn mã khác nhau là hai con số sẽ lệch nhau.

Có một dấu hiệu rõ hơn nữa cho thấy `resolve_user` đã nằm sai tầng từ trước: hai handler
khác (`identity.py`, `share_event.py`) đang **nhập nó từ một handler** — thứ chỉ xảy ra khi
một hàm nghiệp vụ bị kẹt trong tầng trình bày.

Câu chữ (emoji, nhãn tiếng Việt, cách xuống dòng) **ở lại** bên handler: đó là cách NÓI,
không phải cách ĐỌC.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UpsertResult:
    user_id: int
    #: True khi đây là lần đầu người này chạm vào bot. Quyết định có ghi nhận
    #: người giới thiệu hay không — chỉ người MỚI mới được tính cho ai đó.
    is_new: bool
    is_verified: bool


async def upsert_user(
    db: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
) -> UpsertResult:
    """Tạo mới hoặc cập nhật hồ sơ. Gọi trong `transaction()`.

    `xmax = 0` là mẹo của PostgreSQL để phân biệt INSERT thật với UPDATE trong cùng một
    câu `ON CONFLICT`: hàng vừa được chèn có `xmax` bằng 0, hàng bị cập nhật thì không.
    Cách khác là chạy một câu SELECT trước, nhưng như vậy có khoảng trống giữa hai câu
    và hai request song song của cùng một người sẽ cùng thấy "chưa tồn tại".
    """
    row = (
        await db.execute(
            text("""
            INSERT INTO users (user_id, username, full_name, started_bot_at, last_active)
                 VALUES (:uid, :un, :fn, now(), now())
            ON CONFLICT (user_id) DO UPDATE
                    SET username    = EXCLUDED.username,
                        full_name   = EXCLUDED.full_name,
                        last_active = now()
              RETURNING (xmax = 0) AS is_new,
                        (verified_at IS NOT NULL) AS is_verified
            """),
            {"uid": user_id, "un": username, "fn": full_name},
        )
    ).one()

    return UpsertResult(user_id=user_id, is_new=row.is_new, is_verified=row.is_verified)


async def record_referral_intent(
    db: AsyncSession,
    *,
    referee_id: int,
    referrer_id: int,
) -> bool:
    """Ghi nhận "ai giới thiệu ai" từ deep link. Gọi trong `transaction()`.

    Đây mới chỉ là **ý định**, chưa phải referral được tính. Nó chỉ chuyển thành referral
    thật sau khi người được mời xác minh xong — nếu tính ngay lúc bấm link thì chỉ cần
    phát tán link cho hàng nghìn tài khoản rác là ăn được thưởng.

    Trả về True nếu vừa ghi mới. Bỏ qua im lặng khi tự mời chính mình hoặc người giới
    thiệu không tồn tại.
    """
    if referrer_id == referee_id:
        return False

    result = await db.execute(
        text("""
        INSERT INTO referral_intents (referee_id, referrer_id)
             SELECT :referee, :referrer
              WHERE EXISTS (SELECT 1 FROM users WHERE user_id = :referrer)
        ON CONFLICT (referee_id) DO NOTHING
          RETURNING referee_id
        """),
        {"referee": referee_id, "referrer": referrer_id},
    )
    created = result.scalar_one_or_none() is not None
    if created:
        log.info("ghi_nhan_nguoi_gioi_thieu", referee_id=referee_id, referrer_id=referrer_id)
    return created


# ══════════════════════════════════════════════════════════════════════
# Tra cứu cho quản trị
# ══════════════════════════════════════════════════════════════════════


_SQL_USER_BY_USERNAME = "SELECT user_id FROM users WHERE lower(username) = lower(:un)"

_SQL_USER_BY_PK = "SELECT user_id FROM users WHERE user_id = :uid"


def lam_sach_token(raw: str) -> str:
    """Dọn một chuỗi người vận hành dán vào ô tra cứu.

    `.strip()` của Python **không** bỏ U+200B (zero-width space) hay U+FEFF, mà dán từ
    Telegram thì hay kéo theo đúng những ký tự đó cùng với U+00A0. Kết quả: ô tra cứu báo
    "không tìm thấy" một người đang tồn tại, và không có gì trên màn hình gợi ý vì sao.
    Bài kiểm luôn xanh vì bài kiểm gõ chuỗi sạch.

    Bỏ mọi ký tự loại `Cf` (format, gồm zero-width và các dấu điều hướng RTL), quy nbsp về
    khoảng trắng thường, rồi bỏ đúng MỘT dấu `@` ở đầu.
    """
    sach = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")
    sach = sach.replace(" ", " ").strip()  # nbsp → khoảng trắng thường
    return sach[1:].strip() if sach.startswith("@") else sach


def _doc_user_id(raw: str) -> int | None:
    """Chuỗi → `user_id` dương. None nếu không phải số.

    `isdecimal` chứ không `isdigit`: `"²".isdigit()` là True nhưng `int("²")` ném
    `ValueError`. Và từ chối thẳng chuỗi bắt đầu bằng `-` thay vì `lstrip("-")` — `"--5"`
    qua được `lstrip` rồi làm `int()` ném, đúng cái bẫy còn sót trong bản của `codes.py`.
    """
    if not raw.isdecimal():
        return None
    value = int(raw)
    return value if value > 0 else None


async def resolve_user(db: AsyncSession, token: str, *, doan_username: bool = False) -> int | None:
    """`@username` hoặc `user_id` → `user_id`. None khi không tìm thấy.

    Tra username bằng `lower()` để khớp index `uq_users_username_lower`: username của
    Telegram không phân biệt hoa thường, và CSKH gõ lại tên theo trí nhớ.

    `doan_username=False` là ngữ nghĩa NGUYÊN VẸN của bản trong `ops.py` mà lệnh `/user`
    của bot vẫn dùng: không có `@` và không phải số ⇒ `None`. Panel web truyền `True` để
    thêm một nhánh tha thứ — quên dấu `@` thì vẫn coi là username. **Không** đổi mặc định:
    `/resend_tanthu <token>` là một lệnh gửi mã có giá trị tiền thật, và nới lỏng cách nó
    đọc tham số là đổi ý nghĩa của lệnh đó chứ không phải dọn dẹp.
    """
    cleaned = token.strip()
    if cleaned.startswith("@"):
        params = {"un": cleaned[1:]}
        return (await db.execute(text(_SQL_USER_BY_USERNAME), params)).scalar_one_or_none()

    parsed = _doc_user_id(cleaned)
    if parsed is None:
        if not doan_username or not cleaned:
            return None
        row = await db.execute(text(_SQL_USER_BY_USERNAME), {"un": cleaned})
        return row.scalar_one_or_none()

    result = await db.execute(text(_SQL_USER_BY_PK), {"uid": parsed})
    return result.scalar_one_or_none()


# ── Hồ sơ một người ─────────────────────────────────────────────────

#: ⚠️ `LEFT JOIN user_bans` ở đây **cố ý KHÔNG lọc** `unbanned_at IS NULL`, khác với câu
#: của danh sách bên dưới. Lý do: hồ sơ cần in được nhánh "đã gỡ khoá lúc X", mà muốn in
#: được thì phải còn dòng ban trong kết quả. Đồng bộ hai câu này cho "nhất quán" là xoá mất
#: nhánh đó — và nó sẽ im lặng trở thành "chưa từng bị khoá".
_SQL_USER_BY_ID = """
SELECT u.user_id, u.username, u.full_name, u.joined_at, u.verified_at,
       u.points_balance, u.checkin_streak, u.risk_score,
       b.reason AS ban_reason, b.banned_at, b.unbanned_at,
       (SELECT count(*) FROM referrals r WHERE r.referrer_id = u.user_id)      AS refs_total,
       (SELECT count(*) FROM referrals r
         WHERE r.referrer_id = u.user_id AND r.qualified_at IS NOT NULL)       AS refs_qualified,
       (SELECT coalesce(sum(g.value_vnd), 0)::bigint FROM code_grants g
         WHERE g.user_id = u.user_id AND g.state = 'delivered')                AS value_total,
       (SELECT count(*) FROM code_grants g WHERE g.user_id = u.user_id)        AS grants_total
  FROM users u
  LEFT JOIN user_bans b ON b.user_id = u.user_id
 WHERE u.user_id = :uid
"""


@dataclass(frozen=True, slots=True)
class HoSoNguoiDung:
    """Hồ sơ đầy đủ một người, cho màn hình quản trị.

    Các con số về mời bạn và tiền đã nhận đếm **LIVE** trên `referrals` và `code_grants`,
    không đọc `users.total_codes_received` / `total_value_received` (hai cột đó là bộ nhớ
    đệm hiển thị) và cũng không đọc `user_stats` (bảng tổng hợp do job nuôi — con số sẽ
    lệch với sổ cái và không ai biết vì sao).
    """

    user_id: int
    username: str | None
    full_name: str | None
    joined_at: datetime
    verified_at: datetime | None
    points_balance: int
    checkin_streak: int
    risk_score: int
    ban_reason: str | None
    banned_at: datetime | None
    unbanned_at: datetime | None
    refs_total: int
    refs_qualified: int
    value_total: int
    grants_total: int

    @property
    def dang_khoa(self) -> bool:
        return self.banned_at is not None and self.unbanned_at is None

    @property
    def da_tung_khoa(self) -> bool:
        """Từng bị khoá nhưng đã được gỡ."""
        return self.banned_at is not None and self.unbanned_at is not None


async def admin_profile(db: AsyncSession, user_id: int) -> HoSoNguoiDung | None:
    """Hồ sơ một người. **None** khi không có ai mang id đó.

    `one_or_none()` chứ không `one()`: trong bot, id đã được `resolve_user` xác nhận ngay
    trước đó nên `one()` an toàn. Trên web thì hồ sơ mở được bằng URL trực tiếp — và một
    id gõ tay không tồn tại phải ra 404, không phải một vệt stack trace 500.
    """
    row = (await db.execute(text(_SQL_USER_BY_ID), {"uid": user_id})).one_or_none()
    if row is None:
        return None
    return HoSoNguoiDung(
        user_id=row.user_id,
        username=row.username,
        full_name=row.full_name,
        joined_at=row.joined_at,
        verified_at=row.verified_at,
        points_balance=row.points_balance,
        checkin_streak=row.checkin_streak,
        risk_score=row.risk_score,
        ban_reason=row.ban_reason,
        banned_at=row.banned_at,
        unbanned_at=row.unbanned_at,
        refs_total=row.refs_total,
        refs_qualified=row.refs_qualified,
        value_total=row.value_total,
        grants_total=row.grants_total,
    )


# ── Danh sách và số tổng ────────────────────────────────────────────

#: ⚠️ `AND b.unbanned_at IS NULL` nằm TRONG mệnh đề `ON` — khác câu hồ sơ ở trên, và khác
#: một cách CỐ Ý. Ở đây chỉ cần một cờ "đang khoá hay không", nên người đã được gỡ khoá
#: phải rơi ra khỏi join. Chuyển điều kiện này xuống `WHERE` sẽ biến `LEFT JOIN` thành
#: `INNER JOIN` trên thực tế và danh sách chỉ còn người đang bị khoá.
_SQL_USERS_RECENT = """
SELECT u.user_id, u.username, u.full_name, u.joined_at, u.verified_at,
       (b.user_id IS NOT NULL) AS dang_khoa
  FROM users u
  LEFT JOIN user_bans b ON b.user_id = u.user_id AND b.unbanned_at IS NULL
 ORDER BY u.joined_at DESC
 LIMIT :lim
"""

_SQL_USERS_TOTAL = """
SELECT count(*)                                              AS tong,
       count(*) FILTER (WHERE verified_at IS NOT NULL)       AS da_xac_minh,
       count(*) FILTER (WHERE joined_at >= now() - interval '24 hours') AS moi_24h
  FROM users
"""


@dataclass(frozen=True, slots=True)
class DongNguoiDung:
    user_id: int
    username: str | None
    full_name: str | None
    joined_at: datetime
    verified_at: datetime | None
    dang_khoa: bool


@dataclass(frozen=True, slots=True)
class TongNguoiDung:
    tong: int
    da_xac_minh: int
    moi_24h: int


async def recent_users(db: AsyncSession, *, limit: int) -> list[DongNguoiDung]:
    """`limit` người vào bot gần nhất.

    Sắp theo `joined_at DESC` chứ không `last_active`: câu hỏi mà danh sách này trả lời là
    "ai vừa vào", và một tài khoản cũ mở lại bot không phải người mới.
    """
    rows = (await db.execute(text(_SQL_USERS_RECENT), {"lim": limit})).all()
    return [
        DongNguoiDung(
            user_id=r.user_id,
            username=r.username,
            full_name=r.full_name,
            joined_at=r.joined_at,
            verified_at=r.verified_at,
            dang_khoa=r.dang_khoa,
        )
        for r in rows
    ]


async def user_totals(db: AsyncSession) -> TongNguoiDung:
    """Ba con số tổng, đếm LIVE trên `users`.

    Không thay bằng `stats.system_snapshot().total_users`: đó là ảnh chụp do job định kỳ
    nuôi, nên con số sẽ khác con số của danh sách ngay bên dưới nó.
    """
    row = (await db.execute(text(_SQL_USERS_TOTAL))).one()
    return TongNguoiDung(tong=row.tong, da_xac_minh=row.da_xac_minh, moi_24h=row.moi_24h)


def parse_referral_payload(payload: str | None) -> int | None:
    """Đọc `ref_<user_id>` từ deep link `t.me/bot?start=...`.

    Bot cũ dùng tiền tố `cref_575_` với một số nhóm nhúng ở giữa; bot mới chỉ có
    `ref_<id>`. Trả None nếu payload rỗng, sai định dạng, hoặc id không phải số dương.
    """
    if not payload or not payload.startswith("ref_"):
        return None
    raw = payload[4:]
    # `isdecimal` chứ không `isdigit`: `"²".isdigit()` là True nhưng `int("²")` ném
    # `ValueError`. Đường tới đây là `/start ref_²` của một người dùng THƯỜNG, và
    # không có `add_error_handler` nào trong source — ngoại lệ thoát ra thì người đó
    # không nhận được tin chào, không có dòng `users`, và không ai biết.
    if not raw.isdecimal():
        return None
    value = int(raw)
    return value if value > 0 else None
