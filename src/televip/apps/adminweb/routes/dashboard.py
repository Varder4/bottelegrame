"""Tổng quan — màn hình đầu tiên sau khi đăng nhập.

## Gác theo MẢNH, không theo trang

Trang này trộn hai khối quyền khác nhau. Gác cả trang bằng `/tonkho` thì mọi `cskh` nhận
404 ngay sau khi đăng nhập — panel trở thành thứ họ không dùng được. Gác bằng `/stats` thì
số liệu kho rò sang `cskh`. Nên mỗi khối tự hỏi quyền của nó, và người thiếu quyền chỉ mất
đúng khối đó.

Ranh giới lấy đúng theo bot: `/stats` của bot in ra `codes_available` như MỘT con số tổng,
và `cskh` chạy được `/stats`. Cái `cskh` không được xem là **bảng chi tiết theo mệnh giá**
— đó là `/tonkho`, và nó nằm ở màn hình `/kho` riêng.

## Vì sao `transaction()` chứ không `session()` cho một request GET

`stats.system_snapshot()` tự tính lại và **ghi** khi ảnh chụp quá `stats.max_age_seconds`.
Đó là lưới an toàn cho trường hợp job làm mới chết; đưa nó một session chỉ-đọc thì nhánh
đó ném lỗi đúng vào lúc nó cần chạy nhất — tức là lúc đã có một thứ khác hỏng.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, phien_hien_tai
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session, transaction
from televip.services import admin as admin_service
from televip.services import stats as stats_service
from televip.services import stock

router = APIRouter()


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
        xem_stats = await admin_service.can_run(db, nguoi.user_id, "/stats")
        xem_kho = await admin_service.can_run(db, nguoi.user_id, "/tonkho")

    anh_chup = None
    da_xac_minh = None
    if xem_stats:
        async with transaction() as db:  # xem docstring module: nhánh làm mới có GHI
            anh_chup = await stats_service.system_snapshot(db)
            da_xac_minh = await stats_service.verified_count(db)

    canh_bao_kho = None
    if xem_kho:
        nguong = await stock.warn_threshold()
        async with session() as db:
            rows = await stock.read_stock(db)
        tong = stock.summarize(rows, threshold=nguong)
        # Chỉ mang sang màn hình cái người ta cần HÀNH ĐỘNG: có bao nhiêu mệnh giá đã tụt
        # dưới ngưỡng. Bảng đầy đủ nằm ở `/kho`.
        canh_bao_kho = {"low_count": tong.low_count, "nguong": nguong, "con_lai": tong.available}

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "bangdieukhien.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "so_muc": sum(1 for m in menu if m["duoc_phep"]),
            "xem_stats": xem_stats,
            "anh_chup": anh_chup,
            "da_xac_minh": da_xac_minh,
            "canh_bao_kho": canh_bao_kho,
        },
    )


__all__ = ["router"]
