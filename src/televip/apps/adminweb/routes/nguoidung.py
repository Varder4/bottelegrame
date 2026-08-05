"""Màn hình người dùng — danh sách và hồ sơ.

## Hai quyền, không phải một

Danh sách gác bằng `/users`, hồ sơ gác bằng `/user`. Đó là hai lệnh bot có thật, và cả hai
đều được cấp cho `owner`, `admin`, `cskh` (migration `0003`). Gộp cả màn hình về một quyền
là sai theo cả hai hướng: gộp về `/user` thì người chỉ được tra lẻ lại xem được cả bảng;
gộp về `/users` thì chặn nhầm vai trò chỉ được tra lẻ.

## Tra cứu là GET, không phải POST

Ô tra cứu không đổi gì trong database, nên nó là một tham số truy vấn trên chính route
danh sách — không route riêng, không form POST, không CSRF. Tra ra đúng một người thì
chuyển hướng sang hồ sơ; tra không ra thì **ở lại trang danh sách** kèm một dải báo, để
người tra không bị đẩy vào ngõ cụt.

## `code_value` hiện nguyên văn — một lựa chọn có chữ ký

Hồ sơ in mã quà tặng có giá trị tiền thật. Lệnh `/user` của bot đã in đủ cho đúng nhóm
quyền này, nên đây là **giữ nguyên hành vi**, không phải mở rộng. Nhưng bề mặt rò rỉ khác
nhau: một trang web nằm trên tên miền công cộng, ảnh chụp màn hình dễ lan hơn một tin
nhắn. Nếu vòng sau quyết định che thì phải che ở CẢ HAI màn hình cùng lúc — che một bên là
tự tạo ảo giác an toàn.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, khong_thay, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session, transaction
from televip.services import admin as admin_service
from televip.services import code_issuance
from televip.services import users as users_service

router = APIRouter()

#: Số dòng của danh sách. **Hằng số trong mã, không đọc từ query string.**
#:
#: Bot cũng chỉ có `LIMIT`, không `OFFSET` — và `users.joined_at` chưa có index, nên
#: `OFFSET` sâu là một lần sắp toàn bảng cho mỗi lần bấm "trang sau". Đường đi tới người
#: thứ 51 trở đi trong vòng này là Ô TRA CỨU. Vòng nào thêm phân trang phải thêm index
#: trước, không phải sau.
SO_DONG: Final = 50

#: Số mã gần nhất hiện trong hồ sơ — bằng đúng `USER_GRANT_LIMIT` của `/user`.
SO_MA_HIEN: Final = 10

#: Nhãn tiếng Việt cho `code_grants.state`. Trạng thái lạ in nguyên mã của nó: thà xấu còn
#: hơn biến mất khỏi một bảng tiền.
NHAN_TRANG_THAI: Final[dict[str, str]] = {
    "reserved": "đang giữ chỗ",
    "delivered": "đã giao",
    "revoked": "đã thu hồi",
}


def _doc_user_id(raw: str) -> int | None:
    """Path param → `user_id`. None nếu không đọc được.

    Path param khai kiểu `str` rồi tự đọc, **không** khai `int`: FastAPI trả 422 cho
    `/nguoidung/abc`, và 422 là một mã KHÁC 404 — nó tự khai đường dẫn này có tồn tại.
    Toàn bộ tư thế của panel là mọi đường dẫn không dùng được đều trả 404.
    """
    token = raw.strip()
    if not token.isdecimal():  # `isdigit` cho `"²"` là True nhưng `int("²")` ném
        return None
    value = int(token)
    return value if value > 0 else None


@router.get("/nguoidung", response_class=HTMLResponse)
async def danh_sach(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/users"))],
    # Không `max_length`: FastAPI trả 422 khi vượt, mà 422 tự khai đường dẫn có thật.
    # `lam_sach_token` là hàm toàn phần — chuỗi dài hay rác đều chỉ ra "không tìm thấy".
    tim: str = "",
) -> Response:
    """Danh sách người mới nhất, kèm ô tra cứu."""
    token = users_service.lam_sach_token(tim)

    async with session() as db:
        if token:
            found = await users_service.resolve_user(db, token, doan_username=True)
            if found is not None:
                return RedirectResponse(f"/nguoidung/{found}", status_code=303)

        tong = await users_service.user_totals(db)
        dong = await users_service.recent_users(db, limit=SO_DONG)
        menu = await dung_menu(
            db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/nguoidung"
        )

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "nguoidung.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "tong": tong,
            "dong": dong,
            "so_dong": SO_DONG,
            # Echo lại token ĐÃ LÀM SẠCH, không phải chuỗi thô: nếu in lại chuỗi thô thì
            # người dán nhầm ký tự vô hình nhìn thấy đúng thứ họ đã gõ và không hiểu vì sao
            # nó không ra. Autoescape của Jinja lo phần HTML.
            "tim": token,
            "khong_thay": bool(token),
        },
    )


@router.get("/nguoidung/{user_id}", response_class=HTMLResponse)
async def ho_so(
    request: Request,
    user_id: str,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/user"))],
) -> Response:
    """Hồ sơ một người."""
    uid = _doc_user_id(user_id)
    if uid is None:
        raise khong_thay()

    async with session() as db:
        hs = await users_service.admin_profile(db, uid)
        if hs is None:
            # Cùng mã 404 với người không có quyền, nên không dò ra được user_id nào tồn tại.
            raise khong_thay()
        ma = await code_issuance.grants_of_user(db, uid, limit=SO_MA_HIEN)
        menu = await dung_menu(
            db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/nguoidung"
        )

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "nguoidung_chitiet.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "hs": hs,
            "ma": ma,
            "nhan_trang_thai": NHAN_TRANG_THAI,
            "khoa_duoc": await _duoc(nguoi, "/ban"),
            "go_khoa_duoc": await _duoc(nguoi, "/unban"),
        },
    )


async def _duoc(nguoi: NguoiDung, lenh: str) -> bool:
    async with session() as db:
        return await admin_service.can_run(db, nguoi.user_id, lenh)


# ── Khoá và gỡ khoá ─────────────────────────────────────────────────
#
# Hai đường ghi. Luật "KHÔNG BAO GIỜ `DELETE FROM users`" nằm trong service, không ở đây:
# panel không có nút xoá người dùng, và sẽ không bao giờ có.


@router.post("/nguoidung/{user_id}/khoa")
async def khoa(
    request: Request,
    user_id: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/ban"))],
    ly_do: Annotated[str, Form()] = "",
) -> Response:
    uid = _doc_user_id(user_id)
    if uid is None:
        raise khong_thay()
    ip = await ip_cua(request)

    try:
        async with transaction() as db:
            await users_service.ban_user(
                db, user_id=uid, reason=ly_do, actor_id=nguoi.user_id, ip=ip
            )
    except users_service.UserKhongTonTai:
        raise khong_thay() from None

    return RedirectResponse(f"/nguoidung/{uid}", status_code=303)


@router.post("/nguoidung/{user_id}/mo-khoa")
async def mo_khoa(
    request: Request,
    user_id: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/unban"))],
) -> Response:
    uid = _doc_user_id(user_id)
    if uid is None:
        raise khong_thay()
    ip = await ip_cua(request)

    async with transaction() as db:
        # `None` nghĩa là người này không đang bị khoá — lượt bấm thứ hai, không phải lỗi.
        await users_service.unban_user(db, user_id=uid, actor_id=nguoi.user_id, ip=ip)

    return RedirectResponse(f"/nguoidung/{uid}", status_code=303)


__all__ = ["NHAN_TRANG_THAI", "SO_DONG", "SO_MA_HIEN", "router"]
