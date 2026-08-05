"""Màn hình kho code — tồn kho theo loại × mệnh giá, và nạp thêm mã.

Hai quyền khác nhau trên cùng một trang: **xem** gác bằng `/tonkho`, **nạp** gác bằng
`/add_giffcode`. Đúng cặp quyền mà hai lệnh Telegram tương ứng đòi. Vai trò `cskh` không có
cái nào: họ trả lời khách, không cần biết còn bao nhiêu mã 88K trong két.

Số liệu đọc từ `services/stock.py` — cùng một hàm mà `/tonkho` của bot gọi. Hai màn hình
cùng đọc một con số qua hai đoạn mã khác nhau là hai con số sẽ lệch nhau.

## Nạp kho: bốn hàng rào nằm ở service, không ở đây

`stock.kiem_lo_nap()` giữ cả bốn (loại code hợp lệ · mệnh giá đọc được · mệnh giá trong
`code.allowed_values` · mệnh giá đúng loại theo `code.category_values`) cộng ngưỡng duyệt
hai người. Route này **không kiểm lại cái nào** — nó chỉ dịch ngoại lệ thành câu tiếng
Việt trên màn hình.

Và `stock.nap()` chỉ nhận `LoNapKho`, một kiểu mà chỉ `kiem_lo_nap()` dựng được. Nên
đường vào web không **bỏ qua** được hàng rào nào, kể cả khi ai đó sửa file này về sau: nó
không có cách nào gọi thẳng phần ghi.

Nạp kho là hành động tạo ra nghĩa vụ trả tiền — 1.000 mã 88K là 88 triệu sinh ra bằng một
lần bấm — nên hàng rào phải ở nơi cả hai đường vào buộc phải đi qua.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.db.models.codes import CODE_TYPES
from televip.domain import texts
from televip.services import admin as admin_service
from televip.services import settings_service, stock

router = APIRouter()

log = get_logger(__name__)

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
    return await _man_hinh(request, nguoi)


async def _man_hinh(
    request: Request,
    nguoi: NguoiDung,
    *,
    loi: str | None = None,
    xong: str | None = None,
    giu: dict[str, str] | None = None,
) -> Response:
    """Dựng trang. ``giu`` là những gì vừa gõ, để một lô bị từ chối không phải dán lại.

    Dán lại một danh sách vài trăm mã là lúc người ta dán nhầm — nên form phải giữ nguyên
    nội dung khi báo lỗi.
    """
    async with session() as db:
        rows = await stock.read_stock(db)
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/kho")
        # Quyền thứ hai trên cùng trang. Hỏi lại ở mỗi request, không nhớ vào phiên —
        # cùng luật với `can_quyen`, xem `deps.py`.
        duoc_nap = await admin_service.can_run(db, nguoi.user_id, "/add_giffcode")
    nguong = await stock.warn_threshold()
    tong = stock.summarize(rows, threshold=nguong)

    # Mệnh giá nào hợp lệ cho loại nào — đọc từ CHÍNH hai khoá mà `kiem_lo_nap()` kiểm.
    # Dựng danh sách chọn từ một nguồn khác nghĩa là màn hình mời người ta chọn một mệnh
    # giá rồi mới từ chối nó.
    menh_gia_theo_loai: dict[str, list[dict]] = {}
    if duoc_nap:
        try:
            cho_phep = [int(v) for v in await settings_service.get_list("code.allowed_values")]
            theo_loai = await settings_service.get_dict("code.category_values")
        except (ConfigError, TypeError, ValueError):
            # Thiếu hoặc hỏng cấu hình: KHÔNG đoán. Form nạp biến mất và người vận hành
            # thấy lý do, thay vì thấy một ô chọn rỗng không giải thích được.
            log.error("kho_thieu_cau_hinh_menh_gia")
            duoc_nap = False
            cho_phep, theo_loai = [], {}
        for loai in CODE_TYPES:
            rieng = theo_loai.get(loai)
            danh_sach = [int(v) for v in rieng] if rieng is not None else cho_phep
            menh_gia_theo_loai[loai] = [
                {"vnd": v, "nhan": texts.value_label(v)} for v in sorted(danh_sach)
            ]

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
            "duoc_nap": duoc_nap,
            "nhan_loai": NHAN_LOAI,
            "menh_gia_theo_loai": menh_gia_theo_loai,
            "loi": loi,
            "xong": xong,
            "giu": giu or {},
        },
    )


# ── Nạp kho ─────────────────────────────────────────────────────────


@router.post("/kho/nap")
async def nap_kho(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/add_giffcode"))],
    loai: Annotated[str, Form()] = "",
    menh_gia: Annotated[str, Form()] = "",
    ma: Annotated[str, Form()] = "",
) -> Response:
    """Nạp một lô mã vào kho.

    Trả về CHÍNH trang kho (200) thay vì chuyển hướng, cả khi thành công: kết quả nạp
    ("vào kho 48, bỏ qua 2 vì trùng") là con số người vận hành phải đọc, và một `303` sẽ
    thổi bay nó cùng với nội dung vừa dán.
    """
    giu = {"loai": loai, "menh_gia": menh_gia, "ma": ma}
    try:
        lo = await stock.kiem_lo_nap(
            code_type=loai.strip().lower(), menh_gia_tho=menh_gia, ma_tho=ma
        )
    except stock.VuotNguongDuyetHaiNguoi as exc:
        log.warning(
            "nap_code_vuot_nguong",
            actor_id=nguoi.user_id,
            batch_value_vnd=exc.batch_value_vnd,
            threshold_vnd=exc.threshold_vnd,
            so_ma=exc.so_ma,
        )
        return await _man_hinh(
            request,
            nguoi,
            giu=giu,
            loi=(
                f"Lô này trị giá {texts.format_vnd(exc.batch_value_vnd)}đ ({exc.so_ma} mã), "
                f"vượt ngưỡng cần duyệt hai người là "
                f"{texts.format_vnd(exc.threshold_vnd)}đ. Luồng ký thứ hai chưa được xây, "
                f"nên tạm thời hãy chia thành nhiều lô nhỏ hơn ngưỡng."
            ),
        )
    except stock.NapKhoBiTuChoi as exc:
        return await _man_hinh(request, nguoi, giu=giu, loi=str(exc))
    except ConfigError:
        log.error("nap_code_thieu_cau_hinh", actor_id=nguoi.user_id)
        return await _man_hinh(
            request,
            nguoi,
            giu=giu,
            loi="Thiếu cấu hình mệnh giá (code.allowed_values / code.category_values). "
            "Chưa nạp được — báo kỹ thuật.",
        )

    ip = await ip_cua(request)
    async with transaction() as db:
        ket_qua = await stock.nap(db, lo, actor_id=nguoi.user_id, ip=ip)

    log.info(
        "nap_code",
        actor_id=nguoi.user_id,
        batch_id=ket_qua.batch_id,
        code_type=lo.code_type,
        value_vnd=lo.value_vnd,
        added=ket_qua.added,
        skipped=ket_qua.skipped,
    )
    return await _man_hinh(
        request,
        nguoi,
        xong=(
            f"Đã nạp {ket_qua.added} mã {NHAN_LOAI.get(lo.code_type, lo.code_type)} "
            f"{texts.value_label(lo.value_vnd)}"
            + (f", bỏ qua {ket_qua.skipped} mã trùng" if ket_qua.skipped else "")
            + (f" — lô #{ket_qua.batch_id}" if ket_qua.batch_id is not None else "")
            + f". Tồn kho mệnh giá này hiện: {ket_qua.stock_after}."
        ),
    )


__all__ = ["NHAN_LOAI", "router"]
