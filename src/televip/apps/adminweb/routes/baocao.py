"""Màn hình báo cáo chi theo kỳ, và tải CSV.

Gác bằng `/baocao`. Số liệu lấy từ `services/report.py` — cùng ba truy vấn mà lệnh
`/baocao` của bot chạy, nên hai màn hình không thể ra hai con số.

## CSV: một truy vấn, không phải hai

Nút tải gọi lại `collect()` cho **cùng một kỳ**, rồi `to_csv()` trên chính kết quả đó. Đây
không phải hai truy vấn cho cùng một báo cáo theo nghĩa xấu — bảng trên màn hình và tệp
tải về là hai *request* khác nhau, nên chúng vốn đã cách nhau về thời gian. Điều bắt buộc
là **trong một request, số trên màn hình và số trong tệp phải từ cùng một lần đọc**, và
`to_csv(report)` bảo đảm đúng điều đó.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, khong_thay
from televip.apps.adminweb.menu import dung_menu
from televip.core.clock import business_date
from televip.db.engine import session
from televip.services import report as report_service

router = APIRouter()

#: Kiểu MIME kèm charset. Thiếu charset thì một số trình duyệt đoán bảng mã theo locale máy
#: và tiếng Việt vỡ ngay trước khi tệp kịp tới Excel.
CSV_MIME: Final = "text/csv; charset=utf-8"


def _ky(raw: str) -> str:
    """Tham số kỳ từ URL. Kỳ lạ ⇒ 404, KHÔNG âm thầm rơi về kỳ mặc định.

    Im lặng chạy kỳ mặc định sẽ trả về một báo cáo trông hoàn toàn hợp lệ cho một khoảng
    thời gian khác với ý định của người đọc — cùng lý do mà `parse_args` của bot từ chối
    token lạ thay vì đoán.
    """
    ky = raw.strip().lower() or report_service.DEFAULT_PERIOD
    if ky not in report_service.PERIODS:
        raise khong_thay()
    return ky


@router.get("/baocao", response_class=HTMLResponse)
async def man_hinh(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/baocao"))],
    ky: str = "",
) -> Response:
    period = _ky(ky)
    async with session() as db:
        bao_cao = await report_service.collect(db, period=period, today=business_date())
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/baocao")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "baocao.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "bc": bao_cao,
            "ky": period,
            "cac_ky": [(ma, nhan) for ma, (_, nhan) in report_service.PERIODS.items()],
        },
    )


@router.get("/baocao/csv")
async def tai_csv(
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/baocao"))],
    ky: str = "",
) -> Response:
    period = _ky(ky)
    hom_nay = business_date()
    async with session() as db:
        bao_cao = await report_service.collect(db, period=period, today=hom_nay)

    ten = f"baocao_{period}_{hom_nay.isoformat()}.csv"
    return Response(
        content=report_service.to_csv(bao_cao),
        media_type=CSV_MIME,
        headers={
            # `filename*=UTF-8''…` không cần ở đây vì tên tệp chỉ có ASCII; giữ dạng đơn
            # giản để không có trình duyệt nào phải đoán.
            "content-disposition": f'attachment; filename="{ten}"',
            # Báo cáo thay đổi theo từng phút. Một bản trong cache trình duyệt là một bản
            # người đọc tưởng mới mà không phải.
            "cache-control": "no-store",
        },
    )


__all__ = ["CSV_MIME", "router"]
