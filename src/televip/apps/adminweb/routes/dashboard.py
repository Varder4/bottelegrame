"""Tổng quan — màn hình đầu tiên sau khi đăng nhập.

Giai đoạn 0 dựng khung và đường vào. Số liệu thật (tồn kho, thống kê) là giai đoạn 1, và
nó sẽ gác **theo từng mảnh** chứ không theo cả trang: trang này trộn số liệu quyền `/stats`
(cả bốn vai trò xem được) với tồn kho quyền `/tonkho` (`cskh` KHÔNG có). Gác cả trang bằng
`/tonkho` là mọi `cskh` nhận 404 ngay sau khi đăng nhập; gác bằng `/stats` là rò tồn kho
cho `cskh`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, phien_hien_tai
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session

router = APIRouter()


def _templates() -> object:
    from televip.apps.adminweb.app import templates

    return templates


@router.get("/", response_class=HTMLResponse)
async def bang_dieu_khien(
    request: Request, nguoi: Annotated[NguoiDung, Depends(phien_hien_tai)]
) -> Response:
    """Trang chủ panel.

    Menu sinh từ `admin_permissions` — cùng nguồn với `/help_admin` của bot. Người vai trò
    `cskh` không **nhìn thấy** mục "Kho code" vì bảng không có hàng đó, chứ không phải vì
    giao diện ẩn nó đi.
    """
    async with session() as db:
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/")

    return _templates().TemplateResponse(  # type: ignore[attr-defined]
        request,
        "bangdieukhien.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "so_muc": sum(1 for m in menu if m["duoc_phep"]),
        },
    )


__all__ = ["router"]
