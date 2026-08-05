"""Màn hình chiến dịch mời bạn — CHỈ ĐỌC trong đợt này.

Gác bằng `/chiendich`.

## Vì sao chỉ đọc

`start` / `extend` / `end` của bot nằm gọn trong `handlers/admin/campaign.py`: sáu câu SQL,
một `pg_advisory_xact_lock(0x4344)`, và một thứ tự bắt buộc **KHOÁ → KẾT THÚC MỌI CHIẾN
DỊCH ĐANG BẬT → MỞ CÁI MỚI**. Không có `services/campaign.py`.

Dựng nút "Bắt đầu" trên web nghĩa là chép cả ba thứ đó sang một tầng khác. Chép hụt thứ tự
là hai dòng cùng `is_active`, mà `campaign_window()` chỉ đọc **dòng mới nhất** — nên dòng
cũ trở thành "bật nhưng vô hình" và vẫn phát thưởng sau khi admin đã đọc "đã dừng". Mỗi
người mời đủ ăn tối đa 100.000đ; trên tệp 19.151 người đó là một đường chi không ai thấy.

Nên màn này hiện đúng thứ giúp phát hiện tình trạng đó, và điều khiển vẫn nằm trong bot.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session
from televip.services import referral as referral_service

router = APIRouter()


@router.get("/chiendich", response_class=HTMLResponse)
async def man_hinh(
    request: Request, nguoi: Annotated[NguoiDung, Depends(can_quyen("/chiendich"))]
) -> Response:
    async with session() as db:
        cua_so = await referral_service.campaign_window(db)
        dong = await referral_service.list_campaigns(db)
        tham_so = await referral_service.params(db=db)
        menu = await dung_menu(
            db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/chiendich"
        )

    # Chiến dịch "bật mà vô hình": còn cờ `is_active` nhưng KHÔNG phải dòng đang chạy. Đây
    # là con số duy nhất trên màn hình này mà người vận hành phải hành động ngay.
    mo_coi = [d for d in dong if d.is_active and not d.dang_chay]

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "chiendich.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "cua_so": cua_so,
            "dong": dong,
            "mo_coi": mo_coi,
            "tham_so": tham_so,
        },
    )


__all__ = ["router"]
