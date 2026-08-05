"""Phụ thuộc dùng chung: đọc phiên, gác quyền, kiểm CSRF, lấy IP thật.

## Hai luật của file này

**Quyền hỏi lại ở MỖI request.** Phiên chỉ trả lời *"cookie này là ai"*. Câu *"người này
được làm gì"* luôn đi hỏi `admin_users` × `admin_permissions` — cùng một nguồn với bot.
Nhét vai trò vào cookie nghĩa là thu hồi quyền xong người đó vẫn thao tác được cho tới khi
cookie hết hạn, tối đa 8 tiếng.

**Đơn vị quyền là TÊN LỆNH của bot.** Route xem tồn kho gác bằng `"/tonkho"`, route nạp
kho gác bằng `"/add_giffcode"`. Không đẻ ra một không gian tên quyền thứ hai cho web:
`admin_permissions` có bài kiểm khoá hai chiều, và một tên quyền mới ở đó buộc phải kèm
một lệnh bot thật. Dùng lại tên lệnh nghĩa là phân quyền của panel **tự động** khớp với
phân quyền của bot, mãi mãi.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from televip.core.logging import get_logger
from televip.db.engine import session
from televip.services import admin as admin_service
from televip.services import admin_auth, settings_service

log = get_logger(__name__)

#: Khoá cấu hình danh sách proxy tin được — cùng khoá mà Mini App dùng.
SETTING_TRUSTED_PROXIES = "web.trusted_proxies"


@dataclass(frozen=True, slots=True)
class NguoiDung:
    """Người đang đăng nhập. KHÔNG mang theo quyền — quyền hỏi lại mỗi lần cần."""

    user_id: int
    role: str
    csrf_token: str


async def _trusted_proxies() -> list[str]:
    raw: list[Any] = await settings_service.get_list(SETTING_TRUSTED_PROXIES, [])
    return [str(x) for x in raw]


def client_ip(request: Request, trusted: list[str]) -> str | None:
    """IP thật của người gọi.

    Chỉ đọc `X-Forwarded-For` khi kết nối đến TỪ một proxy trong danh sách tin được.
    Không kiểm thì bất kỳ ai cũng tự chọn IP ghi vào sổ bằng cách đặt header — và sổ kiểm
    toán trở thành thứ kẻ tấn công viết hộ.
    """
    truc_tiep = request.client.host if request.client else None
    if not truc_tiep or truc_tiep not in trusted:
        return truc_tiep

    chuoi = request.headers.get("x-forwarded-for", "")
    for phan in chuoi.split(","):
        ung_vien = phan.strip()
        if not ung_vien:
            continue
        try:
            ipaddress.ip_address(ung_vien)
        except ValueError:
            continue
        return ung_vien
    return truc_tiep


async def ip_cua(request: Request) -> str | None:
    return client_ip(request, await _trusted_proxies())


def khong_thay() -> HTTPException:
    """404 chứ không 401.

    Trang trả 401 là trang tự khai "ở đây có thứ đáng đăng nhập". Máy quét tự động không
    phân biệt được panel với một tên miền trống nếu mọi đường dẫn đều 404.
    """
    return HTTPException(status_code=404, detail="Not Found")


async def phien_hien_tai(request: Request) -> NguoiDung:
    """Đọc cookie → phiên → vai trò. Ném 404 nếu không có phiên hợp lệ.

    Vai trò được đọc lại ở ĐÂY, mỗi request — không lấy từ cookie.
    """
    from televip.apps.adminweb import security

    ten_cookie = security.cookie_name(secure=request.app.state.secure_cookies)
    gia_tri = request.cookies.get(ten_cookie)
    if not gia_tri:
        raise khong_thay()

    async with session() as db:
        phien = await admin_auth.load_session(
            db, cookie_value=gia_tri, user_agent=request.headers.get("user-agent")
        )
        await db.commit()
        if phien is None:
            raise khong_thay()
        role = await admin_service.get_role(db, phien.user_id)

    if role is None:
        # Quyền vừa bị thu hồi giữa hai lượt bấm. Giết phiên luôn thay vì chỉ từ chối lượt
        # này — người đã hết quyền không có lý do gì để còn một phiên sống.
        async with session() as db:
            await admin_auth.revoke_all_sessions(db, user_id=phien.user_id)
            await db.commit()
        log.warning("adminweb_phien_cua_nguoi_het_quyen", user_id=phien.user_id)
        raise khong_thay()

    return NguoiDung(user_id=phien.user_id, role=role, csrf_token=phien.csrf_token)


def can_quyen(lenh: str) -> Callable[..., Awaitable[NguoiDung]]:
    """Dependency gác một route bằng quyền của MỘT lệnh bot.

    Ví dụ: `Depends(can_quyen("/tonkho"))`. Người không có quyền nhận 404 — cùng mã với
    người chưa đăng nhập, nên không dò ra được đường dẫn nào tồn tại.
    """

    async def _gac(nguoi: Annotated[NguoiDung, Depends(phien_hien_tai)]) -> NguoiDung:
        async with session() as db:
            duoc = await admin_service.can_run(db, nguoi.user_id, lenh)
        if not duoc:
            log.warning("adminweb_tu_choi_quyen", user_id=nguoi.user_id, role=nguoi.role, lenh=lenh)
            raise khong_thay()
        return nguoi

    return _gac


async def kiem_csrf(
    request: Request, nguoi: Annotated[NguoiDung, Depends(phien_hien_tai)]
) -> NguoiDung:
    """Bắt buộc cho MỌI request ghi. Thiếu token hoặc thiếu `Origin` đều là TRƯỢT.

    Ba lớp cùng lúc:
      1. `SameSite=Lax` trên cookie — form liên nguồn gốc không mang cookie sang.
      2. Token của phiên phải khớp. Nhận ở **header `X-TV-CSRF`** (HTMX gắn sẵn qua
         `hx-headers` của thẻ body) **hoặc trường ẩn `_csrf`** của một form thuần.
      3. `Origin` phải khớp. **Thiếu `Origin` cũng từ chối** — hỏng theo hướng đóng.

    ## Vì sao chấp nhận cả trường ẩn, không chỉ header

    Bản đầu chỉ nhận header, với lý lẽ "form liên nguồn gốc không đặt được header tuỳ
    chỉnh". Lý lẽ đó đúng, nhưng nó cũng làm **mọi form HTML thuần không gửi được** — kể cả
    form của chính panel. Mà panel cố ý dựng bằng form thuần để còn dùng được khi tệp JS
    chưa tải: sửa câu chữ và sửa cấu hình là hai đường ghi duy nhất trên web, và một đường
    ghi phụ thuộc JS là một đường ghi hỏng vào đúng lúc mạng chậm.

    Trường ẩn vẫn an toàn vì nó dựa trên điều kiện thật sự chặn CSRF: **kẻ tấn công không
    ĐỌC được token**. Trang của họ không đọc được HTML của panel (không CORS, cookie
    `HttpOnly`), nên không dựng nổi một form có token đúng. Lớp `Origin` bắt buộc vẫn còn
    nguyên bên dưới.
    """
    from televip.services.admin_auth import check_csrf

    token = request.headers.get("x-tv-csrf")
    if token is None:
        # `request.form()` được Starlette nhớ lại, nên route đọc lại form vẫn thấy dữ liệu.
        form = await request.form()
        gia_tri = form.get("_csrf")
        token = gia_tri if isinstance(gia_tri, str) else None

    if not check_csrf(token, nguoi.csrf_token):
        log.warning("adminweb_csrf_truot", user_id=nguoi.user_id, duong_dan=request.url.path)
        raise khong_thay()

    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        log.warning(
            "adminweb_origin_lech",
            user_id=nguoi.user_id,
            origin=origin,
            mong_doi=str(request.base_url),
        )
        raise khong_thay()
    return nguoi


__all__ = [
    "SETTING_TRUSTED_PROXIES",
    "NguoiDung",
    "can_quyen",
    "khong_thay",
    "client_ip",
    "ip_cua",
    "kiem_csrf",
    "phien_hien_tai",
]
