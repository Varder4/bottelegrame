"""Màn hình kho code — tồn kho theo loại × mệnh giá.

Gác bằng `/tonkho`, đúng cái quyền mà lệnh Telegram tương ứng đòi. Vai trò `cskh` **không**
có quyền này: họ trả lời khách, không cần biết còn bao nhiêu mã 88K trong két.

Số liệu đọc từ `services/stock.py` — cùng một hàm mà `/tonkho` của bot gọi. Hai màn hình
cùng đọc một con số qua hai đoạn mã khác nhau là hai con số sẽ lệch nhau.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session
from televip.services import stock

router = APIRouter()

#: Nhãn cho người đọc, phủ đúng `CODE_TYPES`. Loại lạ (thêm sau, chưa kịp đặt nhãn) hiện
#: nguyên mã của nó — thà xấu còn hơn biến mất khỏi một bảng tiền bạc. Có bài kiểm khoá
#: bảng này với `CODE_TYPES` để nó không tụt lại.
NHAN_LOAI: Final[dict[str, str]] = {
    "tanthu": "Tân thủ",
    "moibanbe": "Mời bạn bè",
    "event": "Đập hộp",
    "diemdanh": "Điểm danh",
    "eventchiase": "Event chia sẻ",
}


@router.get("/kho", response_class=HTMLResponse)
async def man_hinh_kho(
    request: Request, nguoi: Annotated[NguoiDung, Depends(can_quyen("/tonkho"))]
) -> Response:
    async with session() as db:
        rows = await stock.read_stock(db)
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/kho")
    nguong = await stock.warn_threshold()
    tong = stock.summarize(rows, threshold=nguong)

    # Gom theo loại ở đây chứ không trong template: `read_stock` đã sắp theo
    # `(code_type, value_vnd)`, nên một vòng lặp là đủ, và template không phải mang logic.
    nhom: list[dict] = []
    for row in rows:
        if not nhom or nhom[-1]["ma_loai"] != row.code_type:
            nhom.append(
                {
                    "ma_loai": row.code_type,
                    "nhan": NHAN_LOAI.get(row.code_type, row.code_type),
                    "dong": [],
                }
            )
        nhom[-1]["dong"].append({"r": row, "thap": row.low(nguong)})

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "kho.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "nhom": nhom,
            "tong": tong,
            "nguong": nguong,
        },
    )


__all__ = ["NHAN_LOAI", "router"]
