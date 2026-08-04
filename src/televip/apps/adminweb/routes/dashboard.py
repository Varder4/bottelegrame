"""Bảng điều khiển — khung sườn của giai đoạn 0.

Giai đoạn 0 chỉ dựng đường vào và khung giao diện. Nội dung thật (thống kê, tồn kho) là
giai đoạn 1, và nó gác **theo từng mảnh** chứ không theo cả trang: bảng điều khiển trộn
số liệu quyền `/stats` (cả bốn vai trò xem được) với tồn kho quyền `/tonkho` (`cskh`
KHÔNG có). Gác cả trang bằng `/tonkho` là mọi `cskh` nhận 404 ngay sau khi đăng nhập; gác
bằng `/stats` là rò tồn kho cho `cskh`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, phien_hien_tai
from televip.db.engine import session
from televip.services import admin as admin_service

router = APIRouter()


def _templates() -> object:
    from televip.apps.adminweb.app import templates

    return templates


@router.get("/", response_class=HTMLResponse)
async def bang_dieu_khien(
    request: Request, nguoi: Annotated[NguoiDung, Depends(phien_hien_tai)]
) -> Response:
    """Trang chủ panel.

    Danh sách mục trong menu sinh từ `admin_permissions` — cùng nguồn với `/help_admin`
    của bot. Người vai trò `cskh` không **nhìn thấy** mục "Nạp kho" vì bảng không có hàng
    đó, chứ không phải vì giao diện ẩn nó đi.
    """
    async with session() as db:
        lenh_duoc_phep = await admin_service.commands_for_role(db, nguoi.role)

    return _templates().TemplateResponse(  # type: ignore[attr-defined]
        request,
        "bangdieukhien.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "lenh_duoc_phep": lenh_duoc_phep,
        },
    )


__all__ = ["router"]
