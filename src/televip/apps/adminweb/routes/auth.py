"""Đăng nhập và đăng xuất.

Đây là đường duy nhất vào panel, nên nó được viết như một đường tiền:

- **Không nói sai chỗ nào.** "Sai tên đăng nhập" và "sai mật khẩu" ra cùng một câu. Phân
  biệt hai câu đó là tặng kẻ dò một bộ lọc để tìm tên đăng nhập có thật.
- **Mọi lượt hỏng tốn thời gian như nhau**, kể cả lượt bị khoá. `Timer` ép sàn thời gian
  phản hồi để đồng hồ không tố cáo nhánh nào đã chạy.
- **Đăng nhập thành công thì đổi phiên**, không tái dùng giá trị cookie nào có sẵn — chặn
  kiểu tấn công cố định phiên (kẻ tấn công đặt sẵn cookie rồi dụ nạn nhân đăng nhập).
- **Đăng xuất là POST**, không phải GET. Một liên kết đăng xuất bằng GET bị nhúng vào ảnh
  trên trang khác sẽ đá admin ra khỏi phiên mỗi lần họ mở trang đó.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb import security
from televip.apps.adminweb.deps import NguoiDung, ip_cua, phien_hien_tai
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.services import admin_auth
from televip.services.admin import write_audit

log = get_logger(__name__)

router = APIRouter()

#: Một câu duy nhất cho MỌI kiểu đăng nhập hỏng.
_SAI = "Tên đăng nhập hoặc mật khẩu không đúng."


def _templates() -> object:
    from televip.apps.adminweb.app import templates

    return templates


@router.get("/dangnhap", response_class=HTMLResponse)
async def trang_dang_nhap(request: Request) -> Response:
    """Trang đăng nhập. Đã có phiên thì đá thẳng vào bảng điều khiển."""
    ten_cookie = security.cookie_name(secure=request.app.state.secure_cookies)
    if request.cookies.get(ten_cookie):
        async with session() as db:
            phien = await admin_auth.load_session(
                db,
                cookie_value=request.cookies[ten_cookie],
                user_agent=request.headers.get("user-agent"),
            )
            await db.commit()
        if phien is not None:
            return RedirectResponse("/", status_code=303)

    return _templates().TemplateResponse(  # type: ignore[attr-defined]
        request, "dangnhap.html", {"loi": None, "csrf": None}
    )


@router.post("/dangnhap")
async def dang_nhap(
    request: Request,
    login_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    ip = await ip_cua(request)

    async with security.Timer():
        con_khoa = await security.login_locked(ip=ip, login_name=login_name)
        if con_khoa:
            log.warning("adminweb_dang_nhap_bi_khoa", ip=ip, ten=login_name, con_giay=con_khoa)
            return _templates().TemplateResponse(  # type: ignore[attr-defined]
                request,
                "dangnhap.html",
                {
                    "loi": f"Sai quá nhiều lần. Thử lại sau {con_khoa // 60 + 1} phút.",
                    "csrf": None,
                },
                status_code=429,
            )

        async with session() as db:
            tai_khoan = await admin_auth.authenticate(db, login_name=login_name, password=password)

        if tai_khoan is None:
            await security.note_login_fail(ip=ip, login_name=login_name)
            log.warning("adminweb_dang_nhap_sai", ip=ip, ten=login_name)
            return _templates().TemplateResponse(  # type: ignore[attr-defined]
                request, "dangnhap.html", {"loi": _SAI, "csrf": None}, status_code=401
            )

    async with transaction() as db:
        cookie, _csrf = await admin_auth.create_session(
            db,
            user_id=tai_khoan.user_id,
            user_agent=request.headers.get("user-agent"),
            ip=ip,
        )
        # Sổ kiểm toán của panel PHẢI có IP. `audit_log` không có cột IP nên nó đi vào
        # `after` — xem `docs/ke-hoach-v2/16-admin-web.md`, đây là chỗ trả lời câu
        # "lệnh đó gõ từ đâu" mà đường bot không trả lời được.
        await write_audit(
            db,
            actor_id=tai_khoan.user_id,
            action="adminweb.dangnhap",
            entity_type="admin_sessions",
            after={"ip": ip, "ua": request.headers.get("user-agent", "")[:200]},
        )

    await security.clear_login_fails(ip=ip, login_name=login_name)
    log.info("adminweb_dang_nhap_ok", user_id=tai_khoan.user_id, role=tai_khoan.role, ip=ip)

    response = RedirectResponse("/", status_code=303)
    security.set_session_cookie(
        response,
        cookie,
        secure=request.app.state.secure_cookies,
        max_age=int(admin_auth.SESSION_TTL.total_seconds()),
    )
    return response


@router.post("/dangxuat")
async def dang_xuat(
    request: Request, nguoi: Annotated[NguoiDung, Depends(phien_hien_tai)]
) -> Response:
    """POST chứ không GET — xem docstring module."""
    ten_cookie = security.cookie_name(secure=request.app.state.secure_cookies)
    gia_tri = request.cookies.get(ten_cookie)
    if gia_tri:
        async with transaction() as db:
            await admin_auth.revoke_session(db, cookie_value=gia_tri)

    log.info("adminweb_dang_xuat", user_id=nguoi.user_id)
    response = RedirectResponse("/dangnhap", status_code=303)
    security.clear_session_cookie(response, secure=request.app.state.secure_cookies)
    return response


__all__ = ["router"]
