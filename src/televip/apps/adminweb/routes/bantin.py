"""Màn hình bắn tin — đọc, và đúng MỘT nút: Huỷ.

## Vì sao web KHÔNG có nút Gửi

Không phải vì thận trọng. Vì tiến trình này **không có gì để gửi bằng**.

Tin chỉ rời hàng đợi khi `run_outbox_worker` chạy trong **tiến trình bot**: nó dựng
`Sender(app.bot, …)` và cần một `telegram.Bot` sống. Tiến trình `adminweb` không có
`Application`, không có `Bot`, không có token — vòng đời của nó chỉ đóng engine và Redis.

Và mắt xích đứt **im lặng**. `outbox_messages` chỉ sinh ra bởi vòng bơm, mà vòng bơm chỉ
chạy khi có ai gọi `start_pump()` trong tiến trình của chính nó. Một route web lật
`state='running'` mà không bơm sẽ để lại: đợt ghi là "đang chạy", tệp đích đầy, **không một
dòng outbox nào**, **không một tin nào bay**, **không một dòng log lỗi nào**. Đợt đứng im
tới lần restart bot kế tiếp — lúc đó `resume_running_jobs()` chạy và cả đợt bỗng bắn đi,
có thể là 3 giờ sáng.

Cộng thêm một lý do không thuộc broadcast: người bấm nút trên web **không có chat Telegram
để nhận báo lỗi bất đồng bộ**. Thành luật thi công của cả panel: *nếu kết quả một thao tác
không quan sát được ngay trong request đó, thao tác đó không được làm từ web.*

## Vì sao nút Huỷ thì được

Bốn điều cùng lúc:

1. Nó đi trọn vẹn qua `broadcast_service.cancel()` — không chép một hàng rào nào.
2. Nó là hướng **đóng van**, không phải mở.
3. Nó không cần vòng bơm: vòng bơm trong tiến trình bot đọc `state` ở đầu mỗi vòng và tự
   thoát khi trạng thái khác `running`.
4. `cancel()` cố ý chừa những dòng đang bị worker giữ, nên nó không đua với outbox worker.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, khong_thay, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session, transaction
from televip.services import admin as admin_service
from televip.services import broadcast as broadcast_service

router = APIRouter()


def _doc_job_id(raw: str) -> int | None:
    token = raw.strip()
    if not token.isdecimal():
        return None
    value = int(token)
    return value if value > 0 else None


@router.get("/bantin", response_class=HTMLResponse)
async def danh_sach(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/broadcast_status"))],
    truoc: str = "",
) -> Response:
    con_tro = _doc_job_id(truoc)
    async with session() as db:
        dong = await broadcast_service.list_jobs(db, cursor=con_tro)
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/bantin")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "bantin.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "dong": dong,
            "nhan_tep": broadcast_service.AUDIENCE_LABEL,
            "nhan_trang_thai": broadcast_service.JOB_STATE_LABEL,
            "con_tro_sau": dong[-1].job_id if len(dong) == 30 else None,
        },
    )


@router.get("/bantin/{job_id}", response_class=HTMLResponse)
async def chi_tiet(
    request: Request,
    job_id: str,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/broadcast_status"))],
) -> Response:
    jid = _doc_job_id(job_id)
    if jid is None:
        raise khong_thay()

    async with session() as db:
        tt = await broadcast_service.status(db, jid)
        if tt is None:
            raise khong_thay()
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/bantin")
        huy_duoc = await admin_service.can_run(db, nguoi.user_id, "/broadcast_cancel")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "bantin_chitiet.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "tt": tt,
            "nhan_tep": broadcast_service.AUDIENCE_LABEL,
            "nhan_trang_thai": broadcast_service.JOB_STATE_LABEL,
            # Nút Huỷ chỉ dựng khi CẢ hai đúng: có quyền, và đợt còn huỷ được. Hàng rào
            # thật vẫn nằm trong `cancel()` — nó trả `cancelled=False` cho đợt đã kết thúc.
            "huy_duoc": huy_duoc and tt.state in {"draft", "running", "paused"},
        },
    )


@router.post("/bantin/{job_id}/huy")
async def huy(
    request: Request,
    job_id: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/broadcast_cancel"))],
) -> Response:
    jid = _doc_job_id(job_id)
    if jid is None:
        raise khong_thay()
    ip = await ip_cua(request)

    async with transaction() as db:
        ket_qua = await broadcast_service.cancel(db, job_id=jid)
        # Ghi sổ CẢ KHI không huỷ được. Một lượt bấm không đổi gì vẫn là một lượt có người
        # định đổi, và đó đúng là thứ cần trả lời khi soát lại về sau.
        await admin_service.write_audit(
            db,
            actor_id=nguoi.user_id,
            action="adminweb.broadcast_cancel",
            entity_type="broadcast_jobs",
            entity_id=str(jid),
            after={
                "huy_duoc": ket_qua.cancelled,
                "bo_qua": ket_qua.skipped_targets,
                "xoa_khoi_hang_doi": ket_qua.dropped_outbox,
                "ip": ip,
            },
        )

    return RedirectResponse(f"/bantin/{jid}", status_code=303)


__all__ = ["router"]
