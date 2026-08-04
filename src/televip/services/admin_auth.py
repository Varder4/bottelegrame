"""Đăng nhập web cho admin: băm mật khẩu, và vòng đời một phiên.

Đây là **cửa duy nhất** vào panel quản trị, và panel quản trị nhìn thấy toàn bộ mã code
chưa dùng — mà một mã chưa dùng chính là một tờ tiền. Nên file này được viết như một
đường tiền, không như một tiện ích đăng nhập.

## Bốn quyết định, mỗi cái vá một cách hỏng cụ thể

1. **`hashlib.scrypt` của thư viện chuẩn, không thêm phụ thuộc.** scrypt là KDF thiết kế
   đúng cho việc này: chậm có chủ đích và **tốn bộ nhớ**, nên máy đào GPU/ASIC không nhân
   được tốc độ như với SHA. Tham số nhúng ngay trong chuỗi băm, nên nâng tham số sau này
   không làm hỏng mật khẩu cũ.

2. **So sánh mật khẩu bằng thời gian hằng định, và LUÔN băm một lần kể cả khi không tìm
   thấy tài khoản.** Không làm vậy thì thời gian phản hồi tự tố cáo tên đăng nhập nào có
   thật — kẻ dò chỉ cần đo đồng hồ để lọc ra danh sách tài khoản trước khi thử mật khẩu.

3. **`session_id` lưu trong database là BĂM của giá trị cookie.** Bản dump database lọt ra
   ngoài không chứa phiên sống nào. Đây là cùng một lý do vì sao không ai lưu mật khẩu
   dạng thô.

4. **Quyền được hỏi lại ở MỖI request, không nhét vào phiên.** Phiên chỉ trả lời *"cookie
   này là ai"*. Câu *"người này được làm gì"* vẫn chỉ `admin_users` × `admin_permissions`
   trả lời được. Nhét vai trò vào cookie nghĩa là thu hồi quyền xong người đó vẫn thao tác
   được cho tới khi cookie hết hạn.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import now_utc
from televip.core.logging import get_logger

log = get_logger(__name__)

# ── Băm mật khẩu ────────────────────────────────────────────────────

#: Tham số scrypt. `n` là chi phí CPU/bộ nhớ (luỹ thừa 2), `r` kích thước khối, `p` song song.
#:
#: n=2^14 tốn `128·n·r` = **16 MB** và ~50ms mỗi lần băm. Đây là mức tối thiểu OWASP khuyến
#: nghị cho scrypt, và có hai lý do không nâng lên nữa:
#:
#: 1. n=2^15 cần đúng 32 MB, **chạm trần `maxmem` mặc định của OpenSSL** và `hashlib.scrypt`
#:    ném thẳng `ValueError: memory limit exceeded`. Lỗi này nổ ngay lần băm đầu tiên chứ
#:    không âm thầm — nhưng nó nổ ở đường đăng nhập, tức là lúc không ai vào được panel nữa.
#: 2. Chi phí bộ nhớ cao là **con dao hai lưỡi trên VPS nhỏ**: mỗi lần thử mật khẩu ngốn
#:    ngần ấy RAM, nên kẻ dò mật khẩu hàng loạt biến chính hàng rào này thành đòn làm cạn
#:    bộ nhớ máy chủ. Hàng rào thật cho việc dò là giới hạn số lần thử, không phải nâng n.
_SCRYPT_N: Final = 2**14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SALT_BYTES: Final = 16
_KEY_BYTES: Final = 32

#: Trần bộ nhớ truyền TƯỜNG MINH cho OpenSSL. Không truyền thì nó dùng mặc định 32 MB, và
#: mặc định đó khác nhau giữa các bản dựng — một tham số chạy được trên máy dev có thể ném
#: `ValueError` trên VPS. Để dư gấp bốn lần nhu cầu thật.
_MAXMEM: Final = 64 * 1024 * 1024

#: Mật khẩu ngắn hơn số này bị từ chối ngay lúc ĐẶT, không phải lúc đăng nhập.
MIN_PASSWORD_LEN: Final = 10

#: Chuỗi băm giả để so khi không tìm thấy tài khoản — xem quyết định 2 ở docstring.
_DUMMY_HASH: Final = (
    "scrypt$16384$8$1$YWJjZGVmZ2hpamtsbW5vcA$"
    "ZGVhZGJlZWZkZWFkYmVlZmRlYWRiZWVmZGVhZGJlZWZkZWFkYmVlZmRlYWRiZWU"
)


class PasswordTooShort(ValueError):
    """Mật khẩu không đạt độ dài tối thiểu."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$muối$băm`. Tham số nhúng trong chuỗi để nâng được về sau."""
    if len(password) < MIN_PASSWORD_LEN:
        raise PasswordTooShort(
            f"mật khẩu phải dài ít nhất {MIN_PASSWORD_LEN} ký tự, nhận {len(password)}"
        )
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES,
        maxmem=_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str | None) -> bool:
    """So mật khẩu với chuỗi băm. `stored = None` vẫn tốn đúng chừng ấy thời gian.

    Đọc tham số **từ chính chuỗi băm** chứ không từ hằng số ở trên: bản ghi tạo bằng tham
    số cũ phải còn đăng nhập được sau khi ta nâng `_SCRYPT_N`.
    """
    raw = stored or _DUMMY_HASH
    try:
        thuat, n, r, p, salt_b64, key_b64 = raw.split("$")
        if thuat != "scrypt":
            return False
        key = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(key_b64)),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        # Chuỗi băm hỏng trong database. Vẫn phải tốn thời gian như một lần so thật, và
        # vẫn phải trả False — không được để hỏng dữ liệu thành một đường vào.
        hashlib.scrypt(
            password.encode(),
            salt=b"x" * 16,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            maxmem=_MAXMEM,
        )
        return False

    ok = hmac.compare_digest(key, _unb64(key_b64))
    # `stored is None` nghĩa là không có tài khoản nào — dù mật khẩu tình cờ khớp chuỗi
    # giả thì vẫn phải trượt.
    return ok and stored is not None


# ── Tài khoản ───────────────────────────────────────────────────────

_SQL_BY_LOGIN = """
SELECT a.user_id, a.role, a.password_hash, u.username, u.full_name
  FROM admin_users a
  JOIN users u ON u.user_id = a.user_id
 WHERE a.login_name = :ten
   AND a.revoked_at IS NULL
"""

# S105 báo động nhầm vì tên biến chứa chữ "PASSWORD": đây là câu SQL, không phải mật khẩu.
_SQL_SET_PASSWORD = """
UPDATE admin_users
   SET login_name = :ten, password_hash = :bam, password_changed_at = now()
 WHERE user_id = :uid
   AND revoked_at IS NULL
RETURNING user_id
"""  # noqa: S105


@dataclass(frozen=True, slots=True)
class AdminAccount:
    user_id: int
    role: str
    username: str | None
    full_name: str | None


async def set_password(db: AsyncSession, *, user_id: int, login_name: str, password: str) -> bool:
    """Đặt tên đăng nhập + mật khẩu cho một admin ĐANG có hiệu lực.

    Trả `False` khi `user_id` không phải admin đang hoạt động — cố ý **không** tự tạo
    admin. Đặt mật khẩu không phải là cách cấp quyền; cấp quyền là `/admin_add`, và
    `admin_users` phải giữ nguyên là nguồn quyền duy nhất.
    """
    bam = hash_password(password)
    row = (
        await db.execute(
            text(_SQL_SET_PASSWORD),
            {"uid": user_id, "ten": login_name.strip().lower(), "bam": bam},
        )
    ).scalar_one_or_none()
    return row is not None


async def authenticate(db: AsyncSession, *, login_name: str, password: str) -> AdminAccount | None:
    """Kiểm tên đăng nhập + mật khẩu. `None` = sai, và không nói sai chỗ nào.

    Không phân biệt "không có tài khoản" với "sai mật khẩu" ở giá trị trả về **lẫn** ở
    thời gian chạy: cả hai đều tốn đúng một lần băm scrypt.
    """
    row = (await db.execute(text(_SQL_BY_LOGIN), {"ten": login_name.strip().lower()})).one_or_none()

    if not verify_password(password, row.password_hash if row else None):
        return None
    assert row is not None  # noqa: S101 - verify_password trả False khi row is None
    return AdminAccount(
        user_id=row.user_id, role=row.role, username=row.username, full_name=row.full_name
    )


# ── Phiên ───────────────────────────────────────────────────────────

#: Hạn tuyệt đối. Hết là hết, không gia hạn — kể cả đang thao tác.
SESSION_TTL: Final = timedelta(hours=8)

#: Hạn nhàn rỗi. Rời máy quá lâu là phiên chết, dù hạn tuyệt đối còn.
IDLE_TTL: Final = timedelta(minutes=30)

_SQL_CREATE_SESSION = """
INSERT INTO admin_sessions (session_id, user_id, csrf_token, ua_hash, ip, expires_at)
     VALUES (:sid, :uid, :csrf, :ua, :ip, :het_han)
"""

#: Đọc phiên VÀ chạm `last_seen_at` trong một câu — hai câu riêng thì giữa chúng có một
#: khe mà phiên vừa hết hạn nhàn rỗi vẫn được cho qua.
_SQL_TOUCH_SESSION = """
UPDATE admin_sessions
   SET last_seen_at = now()
 WHERE session_id = :sid
   AND revoked_at IS NULL
   AND expires_at > now()
   AND last_seen_at > now() - make_interval(secs => :idle_giay)
RETURNING user_id, csrf_token, ua_hash
"""

_SQL_REVOKE_SESSION = """
UPDATE admin_sessions SET revoked_at = now()
 WHERE session_id = :sid AND revoked_at IS NULL
"""

_SQL_REVOKE_ALL = """
UPDATE admin_sessions SET revoked_at = now()
 WHERE user_id = :uid AND revoked_at IS NULL
RETURNING session_id
"""


@dataclass(frozen=True, slots=True)
class Session:
    user_id: int
    csrf_token: str


def _sid(cookie_value: str) -> str:
    return hashlib.sha256(cookie_value.encode()).hexdigest()


def ua_hash(user_agent: str | None) -> str | None:
    return hashlib.sha256(user_agent.encode()).hexdigest()[:32] if user_agent else None


async def create_session(
    db: AsyncSession, *, user_id: int, user_agent: str | None, ip: str | None
) -> tuple[str, str]:
    """Tạo phiên. Trả `(giá trị cookie, csrf_token)` — database chỉ giữ BĂM của cookie."""
    cookie = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    await db.execute(
        text(_SQL_CREATE_SESSION),
        {
            "sid": _sid(cookie),
            "uid": user_id,
            "csrf": csrf,
            "ua": ua_hash(user_agent),
            "ip": ip,
            "het_han": now_utc() + SESSION_TTL,
        },
    )
    return cookie, csrf


async def load_session(
    db: AsyncSession, *, cookie_value: str, user_agent: str | None
) -> Session | None:
    """Xác thực cookie và chạm mốc hoạt động. `None` = không có phiên hợp lệ.

    User-Agent lệch thì **giết phiên ngay**: một cookie bị bê sang máy khác hiếm khi đi
    kèm đúng chuỗi User-Agent, và nếu đúng là bị trộm thì để phiên sống thêm một giây nào
    cũng là quá lâu.
    """
    sid = _sid(cookie_value)
    row = (
        await db.execute(
            text(_SQL_TOUCH_SESSION),
            # `make_interval(secs => …)` chứ không truyền thẳng `timedelta`: asyncpg không
            # suy ra được kiểu cho một tham số chỉ xuất hiện trong phép trừ, và ném ngay ở
            # tầng driver. Cùng cách đã dùng ở `handlers/admin/campaign.py`.
            {"sid": sid, "idle_giay": int(IDLE_TTL.total_seconds())},
        )
    ).one_or_none()
    if row is None:
        return None

    hien_tai = ua_hash(user_agent)
    if row.ua_hash is not None and hien_tai != row.ua_hash:
        await db.execute(text(_SQL_REVOKE_SESSION), {"sid": sid})
        log.warning("phien_admin_ua_lech", user_id=row.user_id)
        return None

    return Session(user_id=row.user_id, csrf_token=row.csrf_token)


async def revoke_session(db: AsyncSession, *, cookie_value: str) -> None:
    await db.execute(text(_SQL_REVOKE_SESSION), {"sid": _sid(cookie_value)})


async def revoke_all_sessions(db: AsyncSession, *, user_id: int) -> int:
    """Giết mọi phiên của một người. Gọi trong CÙNG giao dịch với việc thu hồi quyền.

    Không gọi thì người vừa bị `/admin_del` vẫn thao tác được trên panel cho tới khi
    cookie hết hạn — tối đa 8 tiếng.
    """
    return len((await db.execute(text(_SQL_REVOKE_ALL), {"uid": user_id})).all())


def check_csrf(gui_len: str | None, cua_phien: str) -> bool:
    """So token CSRF bằng thời gian hằng định. Thiếu token là TRƯỢT, không phải bỏ qua."""
    return bool(gui_len) and hmac.compare_digest(gui_len or "", cua_phien)


__all__ = [
    "IDLE_TTL",
    "MIN_PASSWORD_LEN",
    "SESSION_TTL",
    "AdminAccount",
    "PasswordTooShort",
    "Session",
    "authenticate",
    "check_csrf",
    "create_session",
    "hash_password",
    "load_session",
    "revoke_all_sessions",
    "revoke_session",
    "set_password",
    "ua_hash",
    "verify_password",
]
