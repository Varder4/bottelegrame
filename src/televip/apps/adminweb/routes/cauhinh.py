"""Màn hình cấu hình — xem mọi khoá đang chạy, và đổi khoá không nhạy cảm.

## Vì sao màn này được phép ghi

Vì hàng rào không nằm ở đây. `settings_service` sở hữu cả bốn:

- **Kiểm `sensitive`** — nằm trong `_write()`, ném `SensitiveSettingLocked`. Route này
  không tự kiểm; nó chỉ *không dựng form* cho khoá bị khoá, và đó là chuyện giao diện chứ
  không phải hàng rào. Người gửi POST tay vẫn bị service chặn.
- **Kiểm kiểu và khoảng min/max** — `_validate()`.
- **Che khoá bí mật** — `list_settings()` trả `value=None`; giá trị thật không rời service.
- **Ghi sổ** — `set_by_admin()` gói `set()` + `write_audit()` trong một giao dịch.

## Hiệu lực: tối đa 60 giây

Cùng cơ chế với câu chữ, và cùng lý do phải nói ra trên màn hình: người vận hành đổi mệnh
giá, thử ngay trên bot, thấy giá cũ, rồi **đổi lần thứ hai**. Sổ `settings_audit` ghi hai
lần đổi cho một ý định, và người đọc sổ sau này không biết đó là một hay hai quyết định.

Con số lấy từ `settings_service.CACHE_TTL_SECONDS`, không viết cứng.

## Không đọc cache để HIỂN THỊ

`list_settings()` đọc thẳng bảng trong session của request. Đọc cache thì với nhiều worker,
một người lưu xong bấm F5 và rơi vào worker khác sẽ thấy giá trị cũ — trông y hệt "lưu
không ăn". Cũng vì lý do đó, **không có nút "làm mới cache"**: nó chỉ chạm đúng worker phục
vụ request đó, nên nó là một cái nút nói dối.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, khong_thay, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.core.errors import ConfigError
from televip.db.engine import session, transaction
from televip.services import admin as admin_service
from televip.services import settings_service

router = APIRouter()


async def _dung_trang(
    request: Request,
    nguoi: NguoiDung,
    key: str,
    *,
    dang_go: str | None = None,
    loi: str | None = None,
) -> Response:
    async with session() as db:
        dong = await settings_service.get_setting(db, key)
        if dong is None:
            raise khong_thay()
        menu = await dung_menu(
            db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/cauhinh"
        )
        sua_duoc = await admin_service.can_run(db, nguoi.user_id, "/setcauhinh")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "cauhinh_chitiet.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "d": dong,
            # Giá trị đầy đủ, KHÔNG cắt 160 ký tự: sửa theo một bảng tỉ lệ đã bị cắt là
            # đường thẳng tới một bảng tỉ lệ sai.
            "hien_tai": (
                settings_service.MASKED
                if dong.bi_che
                else settings_service.render_value(dong.value, dong.value_type, full=True)
            ),
            "dang_go": dang_go,
            "loi": loi,
            "goi_y": settings_service.TYPE_HINTS.get(dong.value_type, dong.value_type),
            "ttl": int(settings_service.CACHE_TTL_SECONDS),
            "sua_duoc": sua_duoc,
        },
    )


@router.get("/cauhinh", response_class=HTMLResponse)
async def danh_sach(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/cauhinh"))],
    prefix: str = "",
) -> Response:
    tien_to = prefix.strip()
    async with session() as db:
        dong = await settings_service.list_settings(db, prefix=tien_to)
        menu = await dung_menu(
            db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/cauhinh"
        )

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "cauhinh.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "dong": dong,
            "prefix": tien_to,
            "so_khoa_ky": sum(1 for d in dong if d.sensitive),
            "ttl": int(settings_service.CACHE_TTL_SECONDS),
            "render": settings_service.render_value,
            "MASKED": settings_service.MASKED,
        },
    )


@router.get("/cauhinh/{key}", response_class=HTMLResponse)
async def chi_tiet(
    request: Request, key: str, nguoi: Annotated[NguoiDung, Depends(can_quyen("/cauhinh"))]
) -> Response:
    return await _dung_trang(request, nguoi, key)


@router.post("/cauhinh/{key}")
async def luu(
    request: Request,
    key: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/setcauhinh"))],
    gia_tri: Annotated[str, Form()] = "",
) -> Response:
    async with session() as db:
        dong = await settings_service.get_setting(db, key)
    if dong is None:
        raise khong_thay()

    try:
        gia_tri_moi = settings_service.parse_setting_value(gia_tri, dong.value_type)
    except settings_service.ValueParseError as exc:
        return await _dung_trang(
            request,
            nguoi,
            key,
            dang_go=gia_tri,
            loi=f"Giá trị không đúng kiểu {dong.value_type} — mong đợi: {exc.expected}",
        )

    ip = await ip_cua(request)
    try:
        async with transaction() as db:
            # KHÔNG tự kiểm `sensitive` ở đây: hàng rào thật nằm trong `_write()` và ném
            # `SensitiveSettingLocked`. Kiểm hai chỗ là hai luật, và cái ở đây sẽ trôi.
            await settings_service.set_by_admin(db, key, gia_tri_moi, actor_id=nguoi.user_id, ip=ip)
    except settings_service.SensitiveSettingLocked:
        return await _dung_trang(
            request,
            nguoi,
            key,
            dang_go=gia_tri,
            loi="Khoá này cần người duyệt thứ hai — chưa đổi được từ panel.",
        )
    except ConfigError as exc:
        return await _dung_trang(request, nguoi, key, dang_go=gia_tri, loi=str(exc))

    # Xem ghi chú cùng chỗ trong `routes/cauchu.py`: `set()` gọi `invalidate()` TRƯỚC khi
    # giao dịch của ta commit, nên phải gọi lại sau commit.
    settings_service.invalidate()

    return RedirectResponse(f"/cauhinh/{key}", status_code=303)


__all__ = ["router"]
