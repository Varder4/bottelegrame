"""Hàng rào của panel: cookie, CSRF, header bảo mật, chặn dò mật khẩu.

Panel này nằm trên internet công cộng (quyết định của chủ dự án), và nó nhìn thấy toàn bộ
mã code chưa dùng — mỗi mã là một tờ tiền. Nên mọi thứ ở đây **hỏng theo hướng đóng**:
thiếu thông tin thì từ chối, không phải cho qua.

## Vì sao trả 404 chứ không 401/403 cho người chưa đăng nhập

Một trang trả 401 là một trang tự khai *"ở đây có thứ đáng đăng nhập"*. Trả 404 cho mọi
đường dẫn của panel khi chưa có phiên hợp lệ khiến máy quét tự động không phân biệt được
panel với một tên miền trống. Đây là cùng tư thế `services/admin.py` đã chọn cho bot: từ
chối **im lặng**, không xác nhận lệnh đó có tồn tại.

Riêng trang đăng nhập tất nhiên phải trả 200 — nhưng nó là **một** đường dẫn, không phải
cả bề mặt panel.
"""

from __future__ import annotations

import time
from typing import Final

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from televip.core.logging import get_logger

log = get_logger(__name__)

# ── Cookie ──────────────────────────────────────────────────────────

#: Tiền tố `__Host-` buộc trình duyệt yêu cầu `Secure`, `Path=/`, và **không** có thuộc
#: tính `Domain`. Hệ quả: một tên miền con bị chiếm cũng không đặt được cookie này cho
#: tên miền chính — chặn hẳn kiểu tấn công tiêm cookie từ subdomain.
COOKIE_NAME: Final = "__Host-tvadm"

#: Trên máy dev chạy `http://localhost` thì trình duyệt TỪ CHỐI cookie có tiền tố
#: `__Host-` (vì nó đòi `Secure`, mà `Secure` cần HTTPS). Nên dev dùng tên khác. Đây là
#: khác biệt duy nhất giữa dev và thật, và nó nằm ở đúng một chỗ.
COOKIE_NAME_DEV: Final = "tvadm_dev"


def cookie_name(*, secure: bool) -> str:
    return COOKIE_NAME if secure else COOKIE_NAME_DEV


def set_session_cookie(response: Response, value: str, *, secure: bool, max_age: int) -> None:
    response.set_cookie(
        cookie_name(secure=secure),
        value,
        max_age=max_age,
        httponly=True,  # JavaScript không đọc được ⇒ XSS không lấy được phiên
        secure=secure,
        samesite="lax",  # form liên nguồn gốc không mang cookie sang
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(cookie_name(secure=secure), path="/")


# ── Header bảo mật ──────────────────────────────────────────────────

#: CSP chặt: không CDN, không inline script. Panel tự chứa mọi thứ, nên không cần nới.
#: `'unsafe-inline'` cho style vì Tailwind sinh vài thuộc tính style nội tuyến; script thì
#: KHÔNG được nới — đó mới là đường XSS thật.
_CSP: Final = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)

_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Panel không dùng camera/mic/vị trí. Tắt sẵn để một thư viện thêm vào sau này không
    # âm thầm xin quyền.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}


class SecurityHeaders(BaseHTTPMiddleware):
    """Gắn header bảo mật cho MỌI phản hồi, kể cả trang lỗi.

    Gắn ở middleware chứ không ở từng route: một route quên gắn là một trang không có
    hàng rào, và đó luôn là trang người ta không nghĩ tới.
    """

    def __init__(self, app: object, *, secure: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._secure = secure

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in _HEADERS.items():
            response.headers.setdefault(key, value)
        if self._secure:
            # Chỉ gắn khi thật sự chạy HTTPS. Gắn HSTS trên `http://localhost` làm trình
            # duyệt ghi nhớ và ép HTTPS cho localhost — hỏng mọi dự án khác trên máy đó.
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


# ── Chặn dò mật khẩu ────────────────────────────────────────────────

#: Số lần đăng nhập hỏng liên tiếp trước khi khoá, và thời gian khoá.
#: Khoá theo IP **và** theo tên đăng nhập: chỉ khoá IP thì một botnet đi vòng qua dễ dàng;
#: chỉ khoá tên đăng nhập thì bất kỳ ai cũng khoá được tài khoản của người khác.
MAX_LOGIN_FAILS: Final = 8
LOCK_SECONDS: Final = 900

_PREFIX: Final = "adm:fail:"


def _key(loai: str, gia_tri: str) -> str:
    return f"{_PREFIX}{loai}:{gia_tri}"


async def login_locked(*, ip: str | None, login_name: str) -> int:
    """Số giây còn bị khoá, hoặc 0 nếu được thử. Redis chết thì CHO QUA.

    Redis chết mà chặn đăng nhập nghĩa là một lỗi hạ tầng biến thành *không ai vào được
    panel* — đúng lúc cần vào nhất để xem chuyện gì đang xảy ra. Hàng rào thật cho việc dò
    mật khẩu là `limit_req` của nginx; lớp này là lớp thứ hai, không phải lớp duy nhất.
    """
    from televip.cache.client import get_redis

    try:
        redis = get_redis()
        for loai, gia_tri in (("ip", ip or "?"), ("ten", login_name.strip().lower())):
            so = await redis.get(_key(loai, gia_tri))
            if so is not None and int(so) >= MAX_LOGIN_FAILS:
                return max(1, int(await redis.ttl(_key(loai, gia_tri))))
    except Exception as exc:  # noqa: BLE001 — xem docstring
        log.warning("adminweb_khong_kiem_duoc_khoa_dang_nhap", loi=str(exc))
    return 0


async def note_login_fail(*, ip: str | None, login_name: str) -> None:
    from televip.cache.client import get_redis

    try:
        redis = get_redis()
        for loai, gia_tri in (("ip", ip or "?"), ("ten", login_name.strip().lower())):
            khoa = _key(loai, gia_tri)
            so = await redis.incr(khoa)
            if so == 1:
                await redis.expire(khoa, LOCK_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("adminweb_khong_ghi_duoc_lan_hong", loi=str(exc))


async def clear_login_fails(*, ip: str | None, login_name: str) -> None:
    """Đăng nhập đúng thì xoá bộ đếm — nếu không, một người gõ nhầm vài lần rồi gõ đúng
    vẫn bị khoá ở lần sau."""
    from televip.cache.client import get_redis

    try:
        redis = get_redis()
        await get_redis().delete(_key("ip", ip or "?"), _key("ten", login_name.strip().lower()))
        del redis
    except Exception as exc:  # noqa: BLE001
        log.warning("adminweb_khong_xoa_duoc_bo_dem", loi=str(exc))


# ── Chống dò theo thời gian ở tầng HTTP ─────────────────────────────

#: Mỗi lượt đăng nhập hỏng tốn ít nhất chừng này. Băm scrypt đã ~45ms; đệm thêm cho tổng
#: thời gian phản hồi không tiết lộ nhánh nào đã chạy.
MIN_LOGIN_SECONDS: Final = 0.35


class Timer:
    """Ép một khối mã tốn ít nhất `MIN_LOGIN_SECONDS`."""

    def __init__(self, toi_thieu: float = MIN_LOGIN_SECONDS) -> None:
        self._toi_thieu = toi_thieu
        self._bat_dau = 0.0

    async def __aenter__(self) -> Timer:
        self._bat_dau = time.perf_counter()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        import asyncio

        con = self._toi_thieu - (time.perf_counter() - self._bat_dau)
        if con > 0:
            await asyncio.sleep(con)


__all__ = [
    "COOKIE_NAME",
    "COOKIE_NAME_DEV",
    "LOCK_SECONDS",
    "MAX_LOGIN_FAILS",
    "MIN_LOGIN_SECONDS",
    "SecurityHeaders",
    "Timer",
    "clear_login_fails",
    "clear_session_cookie",
    "cookie_name",
    "login_locked",
    "note_login_fail",
    "set_session_cookie",
]
