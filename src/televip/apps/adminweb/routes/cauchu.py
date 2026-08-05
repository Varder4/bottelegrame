"""Màn hình câu chữ bot — xem và sửa nội dung tin nhắn gửi cho người dùng.

Đây là màn hình CÓ GHI đầu tiên của panel. Ba điều làm nó an toàn, và không điều nào trong
số đó do file này tự dựng:

1. **`text_service.set_content()` kiểm trước khi ghi.** Nó gọi `validate()` — thiếu một
   biến bắt buộc là từ chối, không phải cảnh báo. File này không kiểm lại: kiểm lại là
   dựng bản sao thứ hai của một luật, và bản sao thứ hai luôn là bản trôi đi trước.
2. **Ghi `message_templates` và `message_templates_audit` trong cùng giao dịch.** Đường
   quay lui có sẵn, không phải thứ panel phải nhớ thêm.
3. **`kiem_csrf` trên mọi POST**, và một dòng `audit_log` kèm IP trong cùng giao dịch.

## Hai bẫy cụ thể của một form HTML

**CRLF.** Trình duyệt gửi nội dung `<textarea>` với `\\r\\n` theo đúng chuẩn HTML. Ghi thẳng
vào database thì **mọi khoá đều thành "đã sửa"** ngay lần lưu đầu tiên dù người ta không
đổi một chữ nào — vì bản mặc định trong mã dùng `\\n`. Nên chuẩn hoá trước khi gọi service.

**F5 sau khi lưu.** POST rồi trả HTML thẳng thì F5 gửi lại form. Ở đây trả 303 về chính
trang chi tiết (POST-Redirect-GET).

## Hiệu lực: tối đa 60 giây, và màn hình phải NÓI RA

Hai tiến trình, hai bản cache riêng. Bot tự nạp lại khi bản sao của **chính nó** quá TTL.
Không có `LISTEN/NOTIFY`, không có kênh Redis — không có đường nào chạm tới cache của tiến
trình bot từ đây.

Màn hình im lặng về điều đó sẽ khiến người vận hành sửa xong, thử ngay trên bot, thấy câu
cũ, rồi **sửa lần thứ hai** — và sổ ghi hai lần đổi cho một ý định. Con số in ra lấy từ
`text_service.CACHE_TTL_SECONDS`, không viết cứng: viết cứng là dựng một hằng số thứ hai.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, khong_thay, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session, transaction
from televip.services import admin as admin_service
from televip.services import text_service

router = APIRouter()


def _chuan_hoa(raw: str) -> str:
    """`\\r\\n` của form HTML → `\\n`.

    Bỏ bước này thì mọi khoá thành "đã sửa" vĩnh viễn ngay lần lưu đầu tiên, và mỗi dòng
    dài thêm một byte trong mọi tin nhắn gửi đi sau đó.
    """
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def _phai_co(key: str) -> None:
    """Khoá phải nằm trong bảng đăng ký. Không nhận khoá tuỳ ý từ URL."""
    if key not in text_service.TEMPLATES:
        raise khong_thay()


async def _dung_trang(
    request: Request,
    nguoi: NguoiDung,
    key: str,
    *,
    noi_dung: str | None = None,
    loi: str | None = None,
) -> Response:
    """Trang chi tiết một khoá. Dùng lại cho cả lượt xem lẫn lượt lưu hỏng.

    Khi từ chối, `noi_dung` là **bản người ta vừa gõ** chứ không phải bản đang chạy: bắt
    soạn lại từ đầu vì một biến gõ thiếu là cách nhanh nhất để người ta bỏ panel.
    """
    async with session() as db:
        info = await text_service.get_info(key, db=db)
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/cauchu")

    dang_soan = info.content if noi_dung is None else noi_dung
    try:
        xem_thu = text_service.preview(key, dang_soan)
    except text_service.TemplateError as exc:
        # Bản đang soạn hỏng tới mức không render nổi. Vẫn phải hiện được trang, nếu không
        # thì người ta mất luôn chữ vừa gõ.
        xem_thu = f"(không dựng được bản xem thử: {exc})"

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "cauchu_chitiet.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "info": info,
            "noi_dung": dang_soan,
            "xem_thu": xem_thu,
            "loi": loi,
            "ttl": int(text_service.CACHE_TTL_SECONDS),
            "sua_duoc": await _duoc(nguoi, "/suanoidung"),
            "reset_duoc": await _duoc(nguoi, "/resetnoidung"),
        },
    )


async def _duoc(nguoi: NguoiDung, lenh: str) -> bool:
    async with session() as db:
        return await admin_service.can_run(db, nguoi.user_id, lenh)


@router.get("/cauchu", response_class=HTMLResponse)
async def danh_sach(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/noidung"))],
    prefix: str = "",
) -> Response:
    tien_to = prefix.strip()
    async with session() as db:
        khoa = await text_service.list_keys(tien_to or None, db=db)
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/cauchu")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "cauchu.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "khoa": khoa,
            "prefix": tien_to,
            "da_sua": sum(1 for k in khoa if k.customized),
            "ttl": int(text_service.CACHE_TTL_SECONDS),
        },
    )


@router.get("/cauchu/{key}", response_class=HTMLResponse)
async def chi_tiet(
    request: Request, key: str, nguoi: Annotated[NguoiDung, Depends(can_quyen("/xemnoidung"))]
) -> Response:
    _phai_co(key)
    return await _dung_trang(request, nguoi, key)


@router.post("/cauchu/{key}")
async def luu(
    request: Request,
    key: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/suanoidung"))],
    noi_dung: Annotated[str, Form()] = "",
) -> Response:
    _phai_co(key)
    moi = _chuan_hoa(noi_dung)
    ip = await ip_cua(request)

    async with session() as db:
        cu = (await text_service.get_info(key, db=db)).content

    try:
        async with transaction() as db:
            await text_service.set_content(key, moi, updated_by=nguoi.user_id, db=db)
            await admin_service.write_audit(
                db,
                actor_id=nguoi.user_id,
                action="adminweb.suanoidung",
                entity_type="message_templates",
                entity_id=key,
                before={"content": cu},
                after={"content": moi, "ip": ip},
            )
    except text_service.TemplateError as exc:
        return await _dung_trang(request, nguoi, key, noi_dung=moi, loi=str(exc))

    # `set_content` gọi `invalidate()` ở cuối thân hàm — tức TRƯỚC khi giao dịch của ta
    # commit. Một coroutine khác đọc trong khe hở đó sẽ nạp dữ liệu chưa commit (bản CŨ) và
    # đặt lại mốc cache, khiến chính tiến trình vừa ghi phục vụ bản cũ thêm 60 giây nữa.
    text_service.invalidate()

    return RedirectResponse(f"/cauchu/{key}", status_code=303)


@router.post("/cauchu/{key}/mac-dinh")
async def ve_mac_dinh(
    request: Request,
    key: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/resetnoidung"))],
) -> Response:
    _phai_co(key)
    ip = await ip_cua(request)

    async with session() as db:
        cu = (await text_service.get_info(key, db=db)).content

    async with transaction() as db:
        # `reset` GHI bản mặc định vào bảng, không xoá dòng — đường quay lui còn nguyên.
        moi = await text_service.reset(key, updated_by=nguoi.user_id, db=db)
        await admin_service.write_audit(
            db,
            actor_id=nguoi.user_id,
            action="adminweb.resetnoidung",
            entity_type="message_templates",
            entity_id=key,
            before={"content": cu},
            after={"content": moi, "ip": ip},
        )
    text_service.invalidate()

    return RedirectResponse(f"/cauchu/{key}", status_code=303)


__all__ = ["router"]
