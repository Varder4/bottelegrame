"""Màn hình nhật ký — cửa sổ đầu tiên nhìn vào `audit_log`.

Gác bằng `/cauhinh`: sổ này chứa `before`/`after` của mọi lần đổi cấu hình, tức chính nội
dung mà quyền `/cauhinh` đang che. Gác bằng một quyền lỏng hơn là mở cửa sau.

Cho tới migration `0014`, trong toàn bộ `src/` **không có một câu SELECT nào** trên bảng
này. Sổ được ghi rất kỹ và không ai đọc được — đúng trạng thái của hệ cũ, chỉ khác là ở đó
sổ nghèo, còn ở đây sổ giàu mà không có cửa sổ nhìn vào.

## Phân trang keyset, không OFFSET

`audit_log` chỉ tăng, và tăng **một dòng cho mỗi lượt gõ lệnh có `mutates=True`**.
`OFFSET 10000` là một lần quét bỏ 10.000 dòng cho mỗi lần bấm "trang sau". `log_id` tăng
đơn điệu nên nó vừa là con trỏ vừa là thứ tự — và không dòng nào bị nhảy khi có thao tác
mới chen vào giữa hai lần bấm.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen
from televip.apps.adminweb.menu import dung_menu
from televip.core.clock import vn_day_bounds
from televip.db.engine import session
from televip.services import admin as admin_service

router = APIRouter()

SO_DONG: Final = 50


def _mocvn(raw: str, *, cuoi_ngay: bool) -> datetime | None:
    """`YYYY-MM-DD` từ ô ngày của trình duyệt → mốc UTC của **ngày nghiệp vụ giờ VN**.

    Người vận hành ở VN và gõ ngày theo giờ VN; database lưu UTC. Đọc thẳng chuỗi thành
    một mốc UTC là lệch 7 tiếng — và lệch theo hướng **cắt mất buổi tối**, tức đúng khoảng
    cao điểm mỗi ngày.

    Dùng `vn_day_bounds` của `core/clock.py` chứ không tự tính: đó là cùng hàm mà báo cáo,
    bảng xếp hạng và điểm danh dùng để cắt ngày. Hai cách cắt ngày trong một hệ thống là
    hai kết quả khác nhau cho cùng một câu hỏi.

    `cuoi_ngay=True` lấy **cận trên** của ngày đó, tức 00:00 hôm sau theo giờ VN — truy vấn
    dùng `<` chứ không `<=`, nên lấy 23:59:59 sẽ bỏ sót giây cuối cùng.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        ngay = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    dau, cuoi = vn_day_bounds(ngay)
    return cuoi if cuoi_ngay else dau


def _so(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isdecimal() else None


@router.get("/nhatky", response_class=HTMLResponse)
async def man_hinh(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/cauhinh"))],
    # KHÔNG đặt `max_length`: FastAPI trả **422** khi vượt, và 422 là một mã khác 404 —
    # nó tự khai đường dẫn này có thật. Bốn tham số dưới đây đều là bộ lọc CHỈ ĐỌC, và cả
    # `_mocvn` lẫn `_so` đều là hàm toàn phần: rác vào thì `None` ra và bộ lọc bị bỏ qua.
    action: str = "",
    nguoi_lam: str = "",
    tu: str = "",
    den: str = "",
    truoc: str = "",
) -> Response:
    loc_action = action.strip() or None
    loc_nguoi = _so(nguoi_lam)
    moc_tu = _mocvn(tu, cuoi_ngay=False)
    moc_den = _mocvn(den, cuoi_ngay=True)
    con_tro = _so(truoc)

    async with session() as db:
        dong = await admin_service.list_audit(
            db,
            action=loc_action,
            actor_id=loc_nguoi,
            tu=moc_tu,
            den=moc_den,
            cursor=con_tro,
            limit=SO_DONG,
        )
        cac_action = await admin_service.audit_actions(db)
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/nhatky")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "nhatky.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "dong": dong,
            "cac_action": cac_action,
            "loc": {
                "action": loc_action or "",
                "nguoi": nguoi_lam.strip(),
                "tu": tu.strip(),
                "den": den.strip(),
            },
            # Trang sau tồn tại khi trang này đầy. Không đếm tổng: `count(*)` trên một bảng
            # chỉ tăng là một lần quét toàn bảng cho một con số không ai dùng để làm gì.
            "con_tro_sau": dong[-1].log_id if len(dong) == SO_DONG else None,
            "so_dong": SO_DONG,
        },
    )


__all__ = ["SO_DONG", "router"]
