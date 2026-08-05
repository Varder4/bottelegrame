"""Thu hồi mã trên panel web — huỷ nghĩa vụ, một lần bấm chạm được cả nghìn mã.

Panel chỉ người trong nhà dùng, nên ở đây KHÔNG đo khả năng chống kẻ tấn công. Cái thật sự
xảy ra là **người vận hành bấm nhầm**, và đó là thứ mọi bài dưới đây đo:

* mã **đã phát** cho người dùng không bao giờ bị đụng tới — kể cả khi gõ đúng tên nó;
* cái bị xoá **không rộng hơn** cái vừa nhìn thấy trên trang xác nhận;
* bấm hai lần (F5, nút Back, hai tab) chỉ tính **một** lần;
* ô mệnh giá bỏ trống **không** có nghĩa là "toàn bộ kho".

Mỗi bài phải ĐỎ khi hàng rào tương ứng bị gỡ; danh sách đột biến ở
`scratchpad/dot_bien_thuhoi.py`.
"""

from __future__ import annotations

import httpx
import pytest

from tests.test_adminweb import _dang_nhap, _dung_admin, run_sql, scalar

QUYEN_XEM = "/tonkho"
QUYEN_MOT = "/del_code"
QUYEN_LOAT = "/del_all_code"

OWNER = 990_001


async def _gieo_cau_hinh(*, nguong: int = 10_000_000) -> None:
    """Ngưỡng duyệt hai người. Mặc định để rất cao để nó không cắn nhầm vào bài khác."""
    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi, sensitive)
             VALUES ('admin.dual_approval_threshold_vnd', CAST(:n AS jsonb), 'money_vnd',
                     'Ngưỡng duyệt hai người', false)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {"n": str(nguong)},
    )
    from televip.services import settings_service

    settings_service.invalidate()


async def _chi_giu_quyen(*lenh: str) -> None:
    """Đặt quyền của `owner` thành ĐÚNG danh sách này — `_dung_admin()` cấp sẵn nhiều hơn."""
    from televip.services import admin as admin_service

    await run_sql(
        "DELETE FROM admin_permissions WHERE role = 'owner' "
        "AND NOT (command = ANY(CAST(:giu AS text[])))",
        {"giu": list(lenh)},
    )
    for ten in lenh:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": ten},
        )
    admin_service.invalidate_role(OWNER)


async def _gieo_ma(loai: str, value_vnd: int, *ma: str, status: str = "available") -> None:
    for m in ma:
        await run_sql(
            "INSERT INTO codes (code_value, code_type, value_vnd, status, created_by) "
            "VALUES (:v, :t, :g, :s, :u) ON CONFLICT (code_value) DO NOTHING",
            {"v": m, "t": loai, "g": value_vnd, "s": status, "u": OWNER},
        )


async def _phat_ma(code_value: str, *, user_id: int = 777_001) -> None:
    """Gắn một `code_grants` vào mã — tức mã ĐÃ PHÁT cho người dùng."""
    from televip.db.engine import session as db_session
    from tests.conftest import make_user

    # Fixture của panel `TRUNCATE` cả `grant_types`, nên bài kiểm phải tự gieo lại loại
    # grant mình dùng — `code_grants.grant_type` có khoá ngoại sang bảng đó.
    await run_sql(
        "INSERT INTO grant_types (code, label_vi, once_per_life) "
        "VALUES ('tanthu', 'Code tan thu', true) ON CONFLICT DO NOTHING"
    )
    async with db_session() as s:
        await make_user(s, user_id)
        await s.commit()
    await run_sql(
        """
        INSERT INTO code_grants (grant_key, user_id, grant_type, value_vnd, state,
                                 idempotency_key, code_id, delivered_at)
        SELECT :k, :u, 'tanthu', c.value_vnd, 'delivered', :k, c.code_id, now()
          FROM codes c WHERE c.code_value = :v
        """,
        {"k": f"test-{code_value}", "u": user_id, "v": code_value},
    )
    await run_sql("UPDATE codes SET status = 'issued' WHERE code_value = :v", {"v": code_value})


async def _vao(
    client: httpx.AsyncClient,
    *,
    quyen: tuple[str, ...] = (QUYEN_XEM, QUYEN_MOT, QUYEN_LOAT),
    nguong: int = 10_000_000,
) -> str:
    await _dung_admin()
    await _chi_giu_quyen(*quyen)
    await _gieo_cau_hinh(nguong=nguong)
    await _dang_nhap(client)
    return await scalar(
        "SELECT csrf_token FROM admin_sessions WHERE revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


def _goc() -> dict[str, str]:
    return {"origin": "http://testserver"}


async def _thu_hoi_ma(c: httpx.AsyncClient, csrf: str, ma: str) -> httpx.Response:
    return await c.post("/kho/thuhoi-ma", data={"ma": ma, "_csrf": csrf}, headers=_goc())


async def _de_nghi(
    c: httpx.AsyncClient, csrf: str, *, loai: str = "event", menh_gia: str = "__tatca__"
) -> httpx.Response:
    return await c.post(
        "/kho/thuhoi/denghi",
        data={"loai": loai, "menh_gia": menh_gia, "_csrf": csrf},
        headers=_goc(),
    )


def _ve_tu(r: httpx.Response) -> str:
    import re

    m = re.search(r'name="ve" value="([^"]+)"', r.text)
    assert m is not None, f"trang xác nhận không có vé:\n{r.text[:500]}"
    return m.group(1)


async def _xac_nhan(c: httpx.AsyncClient, csrf: str, ve: str, **them: str) -> httpx.Response:
    return await c.post(
        "/kho/thuhoi/xacnhan", data={"ve": ve, "_csrf": csrf, **them}, headers=_goc()
    )


async def _dem(status: str) -> int:
    return await scalar("SELECT count(*) FROM codes WHERE status = :s", {"s": status})


# ── Thu hồi một mã ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_thu_hoi_mot_ma_va_ghi_so_co_ip(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "AAA111")

    r = await _thu_hoi_ma(app_client, csrf, "AAA111")
    assert r.status_code == 200, r.text[:300]
    assert await scalar("SELECT status FROM codes WHERE code_value = 'AAA111'") == "revoked"

    sau = await scalar(
        "SELECT after FROM audit_log WHERE action = 'del_code' ORDER BY log_id DESC LIMIT 1"
    )
    assert sau["status"] == "revoked"
    assert sau["code_value"] == "AAA111"
    assert "ip" in sau


@pytest.mark.asyncio
async def test_ma_da_phat_bi_tu_choi_va_khong_doi_trang_thai(app_client: httpx.AsyncClient):
    """Hàng rào chính. Mã đã vào sổ cái thì thu hồi là bút toán ngược, không phải xoá dòng."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "DAPHAT1")
    await _phat_ma("DAPHAT1")

    r = await _thu_hoi_ma(app_client, csrf, "DAPHAT1")
    assert r.status_code == 200
    assert "ĐÃ PHÁT" in r.text
    assert await scalar("SELECT status FROM codes WHERE code_value = 'DAPHAT1'") == "issued"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_code'") == 0


@pytest.mark.asyncio
async def test_dan_hai_ma_bi_tu_choi(app_client: httpx.AsyncClient):
    """Nói ra thay vì im lặng xoá cái đầu tiên."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "M1", "M2")

    r = await _thu_hoi_ma(app_client, csrf, "M1 M2")
    assert r.status_code == 200
    assert await _dem("revoked") == 0


@pytest.mark.asyncio
async def test_khoang_trang_thua_van_thu_hoi_dung_ma(app_client: httpx.AsyncClient):
    """Ô nhập gửi lên `"  ABC  \\r\\n"` vẫn là MỘT mã, và phải khớp đúng dòng đó.

    Không cắt khoảng trắng thì người vận hành nhận "không tìm thấy" cho một mã CÓ THẬT —
    câu trả lời sai đẩy họ quay ra gõ tay lệnh hàng loạt với phạm vi rộng hơn.
    """
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "SACH1")

    r = await _thu_hoi_ma(app_client, csrf, "  SACH1  \r\n")
    assert r.status_code == 200
    assert await scalar("SELECT status FROM codes WHERE code_value = 'SACH1'") == "revoked"


@pytest.mark.asyncio
async def test_so_ghi_code_value_tu_DATABASE(app_client: httpx.AsyncClient):
    """Chuỗi vào sổ kiểm toán đến từ bảng `codes`, không từ ô nhập."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "SACH2")

    await _thu_hoi_ma(app_client, csrf, "  SACH2\r\n")
    ghi = await scalar(
        "SELECT after->>'code_value' FROM audit_log WHERE action = 'del_code' "
        "ORDER BY log_id DESC LIMIT 1"
    )
    assert ghi == "SACH2", f"sổ ghi lại cái người ta GÕ: {ghi!r}"


@pytest.mark.asyncio
async def test_ma_dang_giu_cho_khong_thu_hoi_duoc(app_client: httpx.AsyncClient):
    """`reserved` nằm ngoài phạm vi — điều kiện trong câu UPDATE mới là thứ chặn."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "GIUCHO", status="reserved")

    r = await _thu_hoi_ma(app_client, csrf, "GIUCHO")
    assert r.status_code == 200
    assert await scalar("SELECT status FROM codes WHERE code_value = 'GIUCHO'") == "reserved"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_code'") == 0


@pytest.mark.asyncio
async def test_thu_hoi_lai_ma_da_thu_hoi_la_vo_hai(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "HAILAN")

    await _thu_hoi_ma(app_client, csrf, "HAILAN")
    r = await _thu_hoi_ma(app_client, csrf, "HAILAN")
    assert r.status_code == 200
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_code'") == 1


# ── Thu hồi hàng loạt: xem thử ──────────────────────────────────────


@pytest.mark.asyncio
async def test_de_nghi_KHONG_cham_ma_nao_nhung_co_dong_so(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "B1", "B2", "B3")

    r = await _de_nghi(app_client, csrf)
    assert r.status_code == 200
    assert "Xác nhận thu hồi" in r.text
    assert await _dem("available") == 3, "bước xem thử KHÔNG được xoá gì"
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_all_code.denghi'") == 1


@pytest.mark.asyncio
async def test_menh_gia_rong_KHONG_duoc_hieu_la_TAT_CA(app_client: httpx.AsyncClient):
    """Trình duyệt khôi phục ô về rỗng khi bấm Back. Rỗng phải là LỖI, không phải toàn kho."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "C1", "C2")

    r = await _de_nghi(app_client, csrf, menh_gia="")
    assert r.status_code == 200
    assert "Xác nhận thu hồi" not in r.text, "ô rỗng mở ra phạm vi rộng nhất có thể"
    assert await _dem("available") == 2


@pytest.mark.asyncio
async def test_pham_vi_rong_bi_tu_choi(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)

    r = await _de_nghi(app_client, csrf)
    assert r.status_code == 200
    assert "Xác nhận thu hồi" not in r.text


@pytest.mark.asyncio
async def test_vuot_nguong_thi_KHONG_phat_ve(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client, nguong=25_000)
    await _gieo_ma("event", 10_000, "D1", "D2", "D3")

    r = await _de_nghi(app_client, csrf)
    assert r.status_code == 200
    assert "duyệt hai người" in r.text
    assert "Xác nhận thu hồi" not in r.text


# ── Thu hồi hàng loạt: thi hành ─────────────────────────────────────


@pytest.mark.asyncio
async def test_xac_nhan_thu_hoi_va_ghi_so(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "E1", "E2", "E3")
    await _gieo_ma("tanthu", 10_000, "KHAC1")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200, r.text[:300]

    assert (
        await scalar("SELECT count(*) FROM codes WHERE code_type='event' AND status='revoked'") == 3
    )
    assert await scalar("SELECT status FROM codes WHERE code_value = 'KHAC1'") == "available", (
        "loại khác không được đụng tới"
    )

    sau = await scalar(
        "SELECT after FROM audit_log WHERE action = 'del_all_code' ORDER BY log_id DESC LIMIT 1"
    )
    assert sau["so_ma_da_xoa"] == 3
    assert sau["tong_vnd"] == 30_000
    assert "ip" in sau


@pytest.mark.asyncio
async def test_hang_loat_khong_dung_toi_ma_da_phat(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "F1", "F2", "DAPHAT2")
    await _phat_ma("DAPHAT2")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await _xac_nhan(app_client, csrf, ve)

    assert await scalar("SELECT status FROM codes WHERE code_value = 'DAPHAT2'") == "issued"
    assert await _dem("revoked") == 2


@pytest.mark.asyncio
async def test_bam_hai_lan_chi_tinh_MOT_lan(app_client: httpx.AsyncClient):
    """F5 hoặc nút Back. Lượt hai phải vô hại, kể cả khi kho đã được nạp lại."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "G1", "G2")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await _xac_nhan(app_client, csrf, ve)
    await _gieo_ma("event", 10_000, "G3", "G4")

    r2 = await _xac_nhan(app_client, csrf, ve)
    assert r2.status_code == 200
    assert "đã đóng" in r2.text
    assert await _dem("revoked") == 2, "lượt bấm thứ hai đã xoá thêm"
    assert await _dem("available") == 2


@pytest.mark.asyncio
async def test_kho_LON_LEN_giua_hai_buoc_thi_TU_CHOI(app_client: httpx.AsyncClient):
    """Mở trang xác nhận lúc kho có 1 mã, đi nạp thêm, quay lại bấm.

    Bản cũ chỉ *cảnh báo* lệch, và cảnh báo đó in ra SAU khi đã xoá.
    """
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "H1")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await _gieo_ma("event", 10_000, *[f"MOI{i}" for i in range(200)])

    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200
    assert "TỪ CHỐI" in r.text
    assert await _dem("revoked") == 0, "đã xoá 200 mã vừa nạp"
    assert await _dem("available") == 201


@pytest.mark.asyncio
async def test_cung_so_ma_nhung_TIEN_tang_thi_TU_CHOI(app_client: httpx.AsyncClient):
    """10 mã 5.000đ và 10 mã 88.000đ có CÙNG số mã. Canh lệch bằng số mã thì 830.000đ
    biến mất mà không dòng nào kêu."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 5_000, *[f"RE{i}" for i in range(10)])

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    # Đúng 10 mã như cũ, nhưng đắt gấp 17,6 lần.
    await run_sql("DELETE FROM codes WHERE code_type = 'event'")
    await _gieo_ma("event", 88_000, *[f"DAT{i}" for i in range(10)])

    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200
    assert "TỪ CHỐI" in r.text
    assert await _dem("revoked") == 0


@pytest.mark.asyncio
async def test_SO_MA_tang_nhung_TIEN_giam_thi_van_TU_CHOI(app_client: httpx.AsyncClient):
    """Bài DUY NHẤT tách được hai vế của hàng rào "lớn lên".

    Xem thử 2 mã 88.000đ (176.000đ). Lúc bấm là 3 mã 50.000đ (150.000đ): tiền GIẢM nhưng
    số mã TĂNG — bạn duyệt xoá 2 mã và sắp xoá 3. Canh một vế thôi là lọt.
    """
    csrf = await _vao(app_client)
    await _gieo_ma("event", 88_000, "P1", "P2")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await run_sql("DELETE FROM codes WHERE code_type = 'event'")
    await _gieo_ma("event", 50_000, "Q1", "Q2", "Q3")

    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200
    assert "TỪ CHỐI" in r.text
    assert await _dem("revoked") == 0


@pytest.mark.asyncio
async def test_tran_HA_XUONG_giua_hai_buoc_thi_TU_CHOI(app_client: httpx.AsyncClient):
    """Phạm vi KHÔNG lớn lên, nhưng ngưỡng bị hạ trong lúc chờ.

    Đây là ca duy nhất mà lượt đọc ngưỡng bên trong giao dịch ghi là thứ chặn — hàng rào
    "lớn lên" không nói gì về ca này.
    """
    csrf = await _vao(app_client, nguong=10_000_000)
    await _gieo_ma("event", 10_000, "R1", "R2", "R3")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await _gieo_cau_hinh(nguong=20_000)

    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200
    assert "TỪ CHỐI" in r.text
    assert await _dem("revoked") == 0


@pytest.mark.asyncio
async def test_pham_vi_can_kho_giua_hai_buoc_thi_KHONG_ghi_so(app_client: httpx.AsyncClient):
    """0 mã bị xoá thì không có việc gì xảy ra để mà ghi — nhưng vẫn phải nói ra."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "S1", "S2")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await run_sql("UPDATE codes SET status = 'issued' WHERE code_type = 'event'")

    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200
    assert "Đã thu hồi 0 mã" in r.text
    assert "⚠️" in r.text
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'del_all_code'") == 0


@pytest.mark.asyncio
async def test_kho_VOI_di_thi_van_xoa_va_CO_canh_bao(app_client: httpx.AsyncClient):
    """Vơi đi là bình thường (có người vừa nhận mã) — chỉ cần nói ra con số thật."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "I1", "I2", "I3")

    ve = _ve_tu(await _de_nghi(app_client, csrf))
    await run_sql("UPDATE codes SET status = 'issued' WHERE code_value = 'I3'")

    r = await _xac_nhan(app_client, csrf, ve)
    assert r.status_code == 200
    assert "Đã thu hồi 2 mã" in r.text
    assert "⚠️" in r.text, "kho đã đổi mà không cảnh báo"
    assert await _dem("revoked") == 2


@pytest.mark.asyncio
async def test_truong_an_them_vao_form_KHONG_doi_duoc_pham_vi(app_client: httpx.AsyncClient):
    """Phạm vi đọc TỪ VÉ. Một trường ẩn `loai=...` trong tab cũ là bản web của cái nút
    Telegram đã xoá nhầm 17.605.000đ."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "J1")
    await _gieo_ma("tanthu", 10_000, *[f"QUY{i}" for i in range(50)])

    ve = _ve_tu(await _de_nghi(app_client, csrf, loai="event"))
    await _xac_nhan(app_client, csrf, ve, loai="tanthu", menh_gia="__tatca__")

    assert (
        await scalar("SELECT count(*) FROM codes WHERE code_type='tanthu' AND status='revoked'")
        == 0
    )
    assert await _dem("revoked") == 1


@pytest.mark.asyncio
async def test_de_nghi_moi_giet_de_nghi_cu_cua_cung_nguoi(app_client: httpx.AsyncClient):
    """Ba tab mở ba đề nghị là ba lần bấm mà người ta chỉ nhớ một."""
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "K1", "K2")

    ve_cu = _ve_tu(await _de_nghi(app_client, csrf))
    ve_moi = _ve_tu(await _de_nghi(app_client, csrf))
    assert ve_cu != ve_moi

    r = await _xac_nhan(app_client, csrf, ve_cu)
    assert "đã đóng" in r.text
    assert await _dem("revoked") == 0


# ── Quyền ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_co_del_code_nhung_khong_co_del_all_code_thi_404(app_client: httpx.AsyncClient):
    """Vai trò `admin` thật sự có cặp quyền này — migration seed cố ý tách chúng."""
    csrf = await _vao(app_client, quyen=(QUYEN_XEM, QUYEN_MOT))
    await _gieo_ma("event", 10_000, "L1")

    assert (await _de_nghi(app_client, csrf)).status_code == 404
    assert (await _xac_nhan(app_client, csrf, "ve-bia-ra")).status_code == 404

    r = await _thu_hoi_ma(app_client, csrf, "L1")
    assert r.status_code == 200
    assert await _dem("revoked") == 1
    # Người này KHÔNG có `/add_giffcode`. Thông báo thành công vẫn phải hiện — trước đây
    # nó nằm trong khối nạp kho, nên ai không nạp được thì không bao giờ đọc được kết quả
    # việc mình vừa làm, kể cả dòng cảnh báo "kho đã thay đổi giữa hai bước".
    assert "Đã thu hồi mã L1" in r.text, "thông báo thành công bị chôn trong khối quyền khác"


@pytest.mark.asyncio
async def test_chi_xem_duoc_kho_thi_khong_thu_hoi_duoc_gi(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client, quyen=(QUYEN_XEM,))
    await _gieo_ma("event", 10_000, "M1")

    assert (await _thu_hoi_ma(app_client, csrf, "M1")).status_code == 404
    assert (await _de_nghi(app_client, csrf)).status_code == 404
    assert await _dem("revoked") == 0

    trang = await app_client.get("/kho")
    assert trang.status_code == 200
    assert "/kho/thuhoi-ma" not in trang.text
    assert "/kho/thuhoi/denghi" not in trang.text


@pytest.mark.asyncio
async def test_co_quyen_thi_hai_o_deu_hien(app_client: httpx.AsyncClient):
    """Cặp đôi của bài trên — không có nó thì bài trên xanh cả khi form không bao giờ hiện."""
    await _vao(app_client)
    await _gieo_ma("event", 10_000, "N1")

    trang = await app_client.get("/kho")
    assert "/kho/thuhoi-ma" in trang.text
    assert "/kho/thuhoi/denghi" in trang.text
    assert 'data-thloai="event"' in trang.text
    assert 'value="__tatca__"' in trang.text


@pytest.mark.asyncio
@pytest.mark.parametrize("duong", ["/kho/thuhoi-ma", "/kho/thuhoi/denghi", "/kho/thuhoi/xacnhan"])
async def test_thieu_csrf_va_thieu_Origin_deu_404(app_client: httpx.AsyncClient, duong: str):
    csrf = await _vao(app_client)
    await _gieo_ma("event", 10_000, "O1")

    assert (await app_client.post(duong, data={"ma": "O1"}, headers=_goc())).status_code == 404
    assert (await app_client.post(duong, data={"ma": "O1", "_csrf": csrf})).status_code == 404
    assert await _dem("revoked") == 0


# ── Bot và web dùng chung một đoạn mã ───────────────────────────────


@pytest.mark.asyncio
async def test_hai_duong_vao_goi_cung_mot_ham():
    import inspect

    from televip.apps.adminweb.routes import kho as web
    from televip.apps.worker.handlers.admin import codes as bot
    from televip.services import code_issuance

    cap = [
        (bot.handle_del_code, web.thu_hoi_mot_ma, ("kiem_ma_thu_hoi", "thu_hoi_mot")),
        (bot.handle_del_all_code, web.de_nghi_thu_hoi, ("kiem_pham_vi_thu_hoi", "tao_de_nghi")),
        (bot._del_all_dispatch, web.xac_nhan_thu_hoi, ("nhan_de_nghi", "thu_hoi_hang_loat")),
    ]
    for ham_bot, ham_web, phai_co in cap:
        for ten, nguon in (
            ("bot", inspect.getsource(ham_bot)),
            ("web", inspect.getsource(ham_web)),
        ):
            for tu in phai_co:
                assert tu in nguon, f"{ten}.{ham_bot.__name__} không gọi {tu}"
            assert "UPDATE codes" not in nguon, f"{ten} giữ bản sao câu UPDATE"

    # Hàm ghi hàng loạt chỉ nhận `DeNghiThuHoi` — kiểu chỉ sinh ra sau một GETDEL thành
    # công. Đó là thứ khiến không có đường tắt từ "vừa xem thử" thẳng tới "đã xoá".
    tham_so = inspect.signature(code_issuance.thu_hoi_hang_loat).parameters
    assert tham_so["de_nghi"].annotation == "DeNghiThuHoi"
    assert "so_ma_da_hien" not in tham_so, "số đã duyệt không được là tham số tuỳ ý"
