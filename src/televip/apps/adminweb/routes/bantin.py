"""Màn hình bắn tin — đọc, và đúng MỘT nút: Huỷ.

## Web bấm Gửi được — nhưng web không TỰ gửi

Tiến trình này **không có gì để gửi bằng**.

Tin chỉ rời hàng đợi khi `run_outbox_worker` chạy trong **tiến trình bot**: nó dựng
`Sender(app.bot, …)` và cần một `telegram.Bot` sống. Tiến trình `adminweb` không có
`Application`, không có `Bot`, không có token — vòng đời của nó chỉ đóng engine và Redis.

Và mắt xích đứt **im lặng**. `outbox_messages` chỉ sinh ra bởi vòng bơm, mà vòng bơm chỉ
chạy khi có ai gọi `start_pump()` trong tiến trình của chính nó. Một route web lật
`state='running'` mà không bơm sẽ để lại: đợt ghi là "đang chạy", tệp đích đầy, **không một
dòng outbox nào**, **không một tin nào bay**, **không một dòng log lỗi nào**. Đợt đứng im
tới lần restart bot kế tiếp — lúc đó `resume_running_jobs()` chạy và cả đợt bỗng bắn đi,
có thể là 3 giờ sáng.

**Nên web không bao giờ gọi `start_pump`.** Nó ghi ĐÚNG hai thứ vào database: một hàng
`broadcast_jobs` ở `draft` kèm cả tệp đích (bước Tạo nháp), rồi một lần lật `draft → running`
nguyên tử (bước Gửi). Trong tiến trình bot, job định kỳ `bom_dot_bantin` nhặt mọi đợt
`running` lên và bơm. Độ trễ tối đa từ lúc bấm Gửi tới lúc tin đầu tiên bay là **một chu kỳ
job**, đọc từ `settings['jobs.broadcast_pump_seconds']`.

Luật "web không tự bơm" được một bài kiểm ĐỌC MÃ giữ, không phải một dòng ghi chú.

## Bốn hàng rào của lần bấm Gửi, và cả bốn đều ở tầng database

`broadcast_service.start()` là **một câu UPDATE có điều kiện**. Postgres khoá hàng, nên hai
request đồng thời (bấm lại nút, F5 trên POST, hai tab) chỉ đúng MỘT cái đổi được trạng
thái. Ba điều kiện còn lại — `kind`, người tạo, và số đích đã nhìn thấy — chặn ba kịch bản
khác nhau; xem docstring của `start()`.

Không hàng rào nào trong số đó được chép lại ở đây. Một nút bị vô hiệu bằng JavaScript
không chặn được `curl`.

## Vì sao KHÔNG có Tạm dừng / Chạy tiếp

`resume` lật `paused → running` mà không khởi động vòng bơm nào. Trên web nó sẽ trông như
"đã chạy tiếp" trong khi thực tế phải đợi tới tick kế tiếp — và nếu ai đó gỡ job định kỳ
thì nó không bao giờ chạy tiếp. Huỷ đã là đường đóng van đủ mạnh; `pause` không thêm an
toàn nào mà Huỷ chưa có.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from televip.apps.adminweb.deps import NguoiDung, can_quyen, ip_cua, khong_thay, kiem_csrf
from televip.apps.adminweb.menu import dung_menu
from televip.db.engine import session, transaction
from televip.services import admin as admin_service
from televip.services import broadcast as broadcast_service
from televip.services import media

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


# ⚠️ THỨ TỰ QUAN TRỌNG: mọi đường dẫn cụ thể (`/bantin/moi`) phải khai TRƯỚC đường dẫn
# có tham số (`/bantin/{job_id}`). FastAPI khớp theo thứ tự đăng ký, nên đảo lại là
# `/bantin/moi` bị đọc thành job_id="moi" và trả 404 — một lỗi không có dòng log nào.
# ── Soạn và gửi ─────────────────────────────────────────────────────


@router.get("/bantin/moi", response_class=HTMLResponse)
async def man_soan(
    request: Request, nguoi: Annotated[NguoiDung, Depends(can_quyen("/broadcast"))]
) -> Response:
    """Ô soạn nội dung. Gác bằng `/broadcast` — màn này TẠO được đợt."""
    async with session() as db:
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/bantin")
        nguong = await broadcast_service.min_audience(db)

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "bantin_soan.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "tran_ky_tu": broadcast_service.MAX_CONTENT_CHARS,
            "tran_anh_mb": media.MAX_ANH_BYTES // 1024 // 1024,
            "nguong": nguong,
            "nhan_tep": broadcast_service.AUDIENCE_LABEL,
            # Thứ tự quyết định giá trị MẶC ĐỊNH của ô chọn. Tệp hẹp đứng trước: một
            # `<select>` mà mục đầu là "TOÀN BỘ" biến hàng rào "mặc định là tệp hẹp" thành
            # cái ngược lại, và người vội bấm sẽ gửi 19.151 tin thay vì vài nghìn.
            "cac_tep": [broadcast_service.DEFAULT_AUDIENCE, "all"],
            "loi": None,
            "noi_dung": "",
        },
    )


@router.post("/bantin/moi")
async def tao_nhap(
    request: Request,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/broadcast"))],
    noi_dung: Annotated[str, Form()] = "",
    tep: Annotated[str, Form()] = "",
    anh: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Tạo nháp + dựng tệp đích. **Không gửi gì, và không gọi Telegram.**

    Mọi hàng rào nằm trong service: trần độ dài và ngưỡng số đích ở
    `create_and_materialize()`, cỡ và định dạng ảnh ở `media.xin_tai_anh()`. Route chỉ
    dịch lỗi thành câu chữ.

    Ảnh đi qua PHÒNG CHỜ: web ghi bytes, một job trong tiến trình bot tải lên Telegram rồi
    lấy `file_id`. Nháp chỉ tạo được khi ảnh ĐÃ có `file_id`, và đó là hàng rào ở tầng dữ
    liệu chứ không phải một cái nút bị vô hiệu — `media.file_id_cua()` đọc từ
    `media_assets`, bảng chỉ có hàng khi `file_id` tồn tại thật.
    """
    body = noi_dung.replace("\r\n", "\n").replace("\r", "\n").strip()
    audience = tep.strip() or broadcast_service.DEFAULT_AUDIENCE
    ip = await ip_cua(request)

    if not body:
        return await _man_soan_loi(request, nguoi, body, "Chưa có nội dung.")

    # ── Ảnh (không bắt buộc) ────────────────────────────────────────
    asset_key: str | None = None
    if anh is not None and anh.filename:
        du_lieu = await anh.read()
        try:
            async with transaction() as db:
                kq_anh = await media.xin_tai_anh(
                    db, du_lieu=du_lieu, ten_tep=anh.filename, created_by=nguoi.user_id
                )
                await admin_service.write_audit(
                    db,
                    actor_id=nguoi.user_id,
                    action="adminweb.tai_anh",
                    entity_type="media_uploads",
                    entity_id=str(kq_anh.upload_id),
                    after={"sha256": kq_anh.sha256, "so_byte": len(du_lieu), "ip": ip},
                )
        except media.AnhKhongHopLe as exc:
            return await _man_soan_loi(request, nguoi, body, str(exc))

        if not kq_anh.san_sang:
            # Ảnh mới, chưa có `file_id`. KHÔNG tạo nháp: một nháp mang `photo` rỗng sẽ
            # lặng lẽ thành đợt gửi tin chữ, và người soạn không biết ảnh đã rơi mất.
            return await _man_soan_loi(
                request,
                nguoi,
                body,
                f"Ảnh đang được tải lên Telegram (mã #{kq_anh.upload_id}). "
                "Bấm Tạo bản nháp lại sau vài giây — ảnh không phải chọn lại.",
            )
        asset_key = kq_anh.asset_key

    try:
        async with transaction() as db:
            # `photo` chỉ có mặt khi ảnh đã sẵn sàng; `outbox_method()` tự thấy khoá đó
            # và chọn `sendPhoto`, không cần cột mới hay cờ mới.
            payload: dict[str, object] = {"text": body}
            if asset_key is not None:
                file_id = await media.file_id_cua(db, asset_key)
                if file_id is None:  # pragma: no cover - vừa kiểm san_sang ở trên
                    return await _man_soan_loi(request, nguoi, body, "Ảnh chưa sẵn sàng.")
                payload = {"photo": file_id, "caption": body}

            kq = await broadcast_service.create_and_materialize(
                db,
                kind="broadcast",
                audience=audience,
                payload=payload,
                created_by=nguoi.user_id,
            )
            await admin_service.write_audit(
                db,
                actor_id=nguoi.user_id,
                action="adminweb.broadcast_nhap",
                entity_type="broadcast_jobs",
                entity_id=str(kq.job_id),
                after={
                    "audience": audience,
                    "so_dich": kq.total,
                    "bi_tu_choi": kq.refused,
                    "co_anh": asset_key is not None,
                    "da_huy_nhap_cu": kq.da_huy_nhap_cu,
                    "ip": ip,
                },
            )
    except ValueError as exc:
        return await _man_soan_loi(request, nguoi, body, str(exc))

    if kq.refused:
        # Nháp đã bị HUỶ trong cùng giao dịch — không còn nút Gửi nào treo lơ lửng.
        return await _man_soan_loi(
            request,
            nguoi,
            body,
            f"Chỉ có {kq.total} người nhận, dưới ngưỡng {kq.minimum} "
            f"({broadcast_service.MIN_AUDIENCE_KEY}). Số đích thấp bất thường thường là "
            f"bộ lọc sai, không phải tệp nhỏ thật — bản nháp đã bị huỷ.",
        )

    return RedirectResponse(f"/bantin/{kq.job_id}/xemthu", status_code=303)


async def _man_soan_loi(request: Request, nguoi: NguoiDung, noi_dung: str, loi: str) -> Response:
    """Trả lại ô soạn kèm chữ đã gõ. Bắt soạn lại từ đầu là cách nhanh nhất mất người dùng."""
    async with session() as db:
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/bantin")
        nguong = await broadcast_service.min_audience(db)

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "bantin_soan.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "tran_ky_tu": broadcast_service.MAX_CONTENT_CHARS,
            "tran_anh_mb": media.MAX_ANH_BYTES // 1024 // 1024,
            "nguong": nguong,
            "nhan_tep": broadcast_service.AUDIENCE_LABEL,
            "cac_tep": [broadcast_service.DEFAULT_AUDIENCE, "all"],
            "loi": loi,
            "noi_dung": noi_dung,
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


@router.get("/bantin/{job_id}/xemthu", response_class=HTMLResponse)
async def xem_thu(
    request: Request,
    job_id: str,
    nguoi: Annotated[NguoiDung, Depends(can_quyen("/broadcast"))],
) -> Response:
    """Bước xem thử — **không bỏ qua được**, vì nó là nơi sinh ra con số `so_dich`.

    Không có nó thì "tạo nháp" và "gửi" gộp thành một lần bấm, và hệ cũ đã trả giá đúng cho
    việc đó: gõ lệnh là tin bay ngay.
    """
    jid = _doc_job_id(job_id)
    if jid is None:
        raise khong_thay()

    async with session() as db:
        tt = await broadcast_service.status(db, jid)
        if tt is None or tt.kind != "broadcast":
            raise khong_thay()
        nd = await broadcast_service.job_content(db, jid)
        if nd is None:
            raise khong_thay()
        menu = await dung_menu(db, user_id=nguoi.user_id, role=nguoi.role, duong_hien_tai="/bantin")

    from televip.apps.adminweb.app import templates

    return templates.TemplateResponse(
        request,
        "bantin_xemthu.html",
        {
            "csrf": nguoi.csrf_token,
            "nguoi": nguoi,
            "menu": menu,
            "tt": tt,
            "noi_dung": nd.text or nd.payload.get("caption", ""),
            "co_anh": "photo" in nd.payload,
            "nhan_tep": broadcast_service.AUDIENCE_LABEL,
            "uoc_tinh": broadcast_service.format_duration(
                broadcast_service.estimate_seconds(tt.total)
            ),
            "cua_minh": nd.created_by == nguoi.user_id,
            "qua_han": nd.qua_han,
            "han_phut": broadcast_service.DRAFT_MAX_AGE_SECONDS // 60,
        },
    )


@router.post("/bantin/{job_id}/gui")
async def gui(
    request: Request,
    job_id: str,
    nguoi: Annotated[NguoiDung, Depends(kiem_csrf)],
    _: Annotated[NguoiDung, Depends(can_quyen("/broadcast"))],
    so_dich: Annotated[str, Form()] = "",
) -> Response:
    """Lật `draft → running`. **Không bơm gì** — job trong tiến trình bot làm việc đó.

    `so_dich` là số đích người bấm ĐÃ NHÌN THẤY trên màn xem thử. Nó đi thẳng vào
    `start()` làm điều kiện của câu `UPDATE`: lệch thì không đổi được trạng thái.
    """
    jid = _doc_job_id(job_id)
    if jid is None:
        raise khong_thay()
    mong_doi = int(so_dich) if so_dich.strip().isdecimal() else None
    ip = await ip_cua(request)

    async with transaction() as db:
        started = await broadcast_service.start(
            db,
            job_id=jid,
            kind="broadcast",
            actor_id=nguoi.user_id,
            expect_total=mong_doi,
        )
        await admin_service.write_audit(
            db,
            actor_id=nguoi.user_id,
            action="adminweb.broadcast_start",
            entity_type="broadcast_jobs",
            entity_id=str(jid),
            after={"bat_dau_duoc": started is not None, "so_dich_da_xem": mong_doi, "ip": ip},
        )

    return RedirectResponse(f"/bantin/{jid}", status_code=303)


__all__ = ["router"]
