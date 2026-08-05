"""Màn hình tra tín hiệu định danh. Gác bằng `/checkip`.

Miền này chạm dữ liệu riêng tư, và panel nằm trên tên miền công cộng. Hai luật của bản
Telegram được giữ nguyên, không nới:

1. **Không đường nào đi từ tín hiệu ngược về TÊN của người khác.** Tra theo IP trả
   `user_id`, không trả username. Muốn biết ai thì mở hồ sơ — và lượt đó là một đường dẫn
   khác, gác bằng một quyền khác. Có bài kiểm đọc template khẳng định trang này không chứa
   chuỗi `username`.
2. **IP hiện dạng rút gọn.** Việc che nằm trong `services/identity_admin.py`, ở tầng ĐỌC:
   IP đầy đủ không rời service, nên một template mới quên gọi hàm che cũng không lộ được.

Và một luật nữa của chính miền: màn hình này **chỉ ĐO**. Nó không chặn ai, không chấm điểm
ai. `risk_assessments.score` chưa có ai ghi — in ra `0` là bịa một con số, tệ hơn hẳn nói
thẳng "chưa chấm điểm".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session
from televip.services import identity_admin
from televip.services import users as users_service

router = APIRouter()


@router.get("/dinhdanh", response_class=HTMLResponse)
async def man_hinh(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/checkip"))],
    # Không `max_length`: FastAPI trả 422 khi vượt, mà 422 tự khai đường dẫn có thật.
    # `lam_sach_token` là hàm toàn phần — chuỗi dài hay rác đều chỉ ra "không tìm thấy".
    tim: str = "",
) -> Response:
    token = users_service.lam_sach_token(tim)

    ip = identity_admin.parse_ip(token) if token else None
    ket_qua: dict = {"kieu": None}

    async with session() as db:
        if ip is not None:
            ket_qua = {
                "kieu": "ip",
                "gia_tri": identity_admin.rut_gon_ip(ip),
                "chu": await identity_admin.chu_tin_hieu(db, loai="ip", gia_tri=ip),
                "tai_khoan": await identity_admin.tai_khoan_theo_tin_hieu(
                    db, loai="ip", gia_tri=ip
                ),
            }
        elif token:
            user_id = await users_service.resolve_user(db, token, doan_username=True)
            if user_id is None:
                ket_qua = {"kieu": "khong_thay"}
            else:
                ket_qua = {
                    "kieu": "nguoi",
                    "user_id": user_id,
                    "tin_hieu": await identity_admin.tin_hieu_cua_nguoi(db, user_id),
                    "xac_minh": await identity_admin.luot_xac_minh(db, user_id),
                }

        menu = await dung_menu(
            db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/dinhdanh"
        )

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "dinhdanh.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "tim": token,
            "kq": ket_qua,
            "dang_chu_y": identity_admin.DANG_CHU_Y,
        },
    )


__all__ = ["router"]
