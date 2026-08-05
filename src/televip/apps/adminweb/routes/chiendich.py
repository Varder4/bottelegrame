"""Màn hình chiến dịch mời bạn — CHỈ ĐỌC trong đợt này.

Gác bằng `/chiendich`.

## Ba nút, và cả ba đi qua CÙNG hàm mà bot gọi

`campaign_service.mo()` / `gia_han()` / `dung()` giữ thứ tự bắt buộc **KHOÁ → DỪNG MỌI
CHIẾN DỊCH ĐANG BẬT → MỞ CÁI MỚI**, cùng một `pg_advisory_xact_lock(0x4344)`. Route này
không chép một bước nào.

Chép hụt thứ tự đó là hai dòng cùng `is_active`, mà `campaign_window()` chỉ đọc **dòng mới
nhất** — nên dòng cũ thành "bật nhưng vô hình" và vẫn phát thưởng sau khi người vận hành đã
đọc "đã dừng". Mỗi người mời đủ ăn tối đa `max_claims × reward`; trên tệp 19.151 người đó
là một đường chi không ai nhìn ra.

Màn hình vẫn đếm và cảnh báo số chiến dịch "bật mà không chạy" — hàng rào nào cũng có thể
hỏng, và một màn hình nói ra được tình trạng đó rẻ hơn nhiều so với việc phát hiện nó qua
sổ chi.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session, transaction
from televip.services import campaign as campaign_service
from televip.services import referral as referral_service

router = APIRouter()


@router.get("/chiendich", response_class=HTMLResponse)
async def trang(
    request: Request, nguoi: Annotated[NguoiDung, Depends(can_quyen("/chiendich"))]
) -> Response:
    return await _man_hinh(request, nguoi)


async def _man_hinh(request: Request, nguoi: NguoiDung, *, loi: str | None = None) -> Response:
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
            "max_days": campaign_service.MAX_DAYS,
            "loi": loi,
        },
    )


# ── Ba đường ghi ────────────────────────────────────────────────────


@router.post("/chiendich/mo")
async def mo(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/chiendich"))],
    so_ngay: Annotated[str, Form()] = "",
    ten: Annotated[str, Form()] = "",
) -> Response:
    """Mở chiến dịch — **thao tác MỞ VAN TIỀN**.

    Mọi chiến dịch đang bật bị dừng trước, trong cùng giao dịch. Đó là thứ tự bắt buộc, và
    nó nằm trong service.
    """
    try:
        days = campaign_service.doc_so_ngay(so_ngay)
    except campaign_service.SoNgayKhongHopLe as exc:
        return await _man_hinh(request, nguoi, loi=str(exc))

    ip = await ip_cua(request)
    async with transaction() as db:
        await campaign_service.mo(
            db,
            days=days,
            name=ten.strip() or "Chiến dịch mời bạn",
            actor_id=nguoi.user_id,
            ip=ip,
        )
    return RedirectResponse("/chiendich", status_code=303)


@router.post("/chiendich/giahan")
async def gia_han(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/chiendich"))],
    so_ngay: Annotated[str, Form()] = "",
) -> Response:
    """Kéo dài chiến dịch đang chạy. Cộng dồn từ hạn CŨ, không tính từ bây giờ."""
    try:
        days = campaign_service.doc_so_ngay(so_ngay)
    except campaign_service.SoNgayKhongHopLe as exc:
        return await _man_hinh(request, nguoi, loi=str(exc))

    ip = await ip_cua(request)
    try:
        async with transaction() as db:
            await campaign_service.gia_han(db, days=days, actor_id=nguoi.user_id, ip=ip)
    except campaign_service.KhongCoChienDichDangChay:
        return await _man_hinh(
            request,
            nguoi,
            loi="Không có chiến dịch nào đang chạy để gia hạn. Chiến dịch đã hết hạn thì "
            "phải MỞ LẠI — gia hạn một cửa sổ đã đóng chính là mở lại van tiền, và hai "
            "việc đó phải nhìn khác nhau trong sổ.",
        )
    return RedirectResponse("/chiendich", status_code=303)


@router.post("/chiendich/dung")
async def dung(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/chiendich"))],
) -> Response:
    """Dừng MỌI chiến dịch đang bật — kể cả những cái đang "bật mà vô hình"."""
    ip = await ip_cua(request)
    async with transaction() as db:
        await campaign_service.dung(db, actor_id=nguoi.user_id, ip=ip)
    return RedirectResponse("/chiendich", status_code=303)


__all__ = ["router"]
