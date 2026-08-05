"""Nạp kho code trên web — đường vào THỨ HAI cho thao tác sinh ra nghĩa vụ trả tiền.

1.000 mã 88K là 88 triệu đồng nghĩa vụ sinh ra bằng một lần bấm. Nên điều đáng đo ở đây
không phải "form có lưu được không", mà là: **bốn hàng rào của bot còn nguyên khi đi đường
web hay không.**

Bốn hàng rào, theo đúng thứ tự `stock.kiem_lo_nap()` chạy:

1. Loại code phải nằm trong `CODE_TYPES`.
2. Mệnh giá phải đọc được (`10k` · `10.000` · `10000`).
3. Mệnh giá phải nằm trong `code.allowed_values`.
4. Mệnh giá phải hợp với loại đó theo `code.category_values`.

Cộng ngưỡng **duyệt hai người** (`admin.dual_approval_threshold_vnd`), tính SAU khi khử
trùng trong lô — tính trên chuỗi thô là tính trên một tập không có thật.

Mỗi bài dưới đây phải ĐỎ khi hàng rào tương ứng bị gỡ. Đó là điều kiện để bài kiểm này có
nghĩa; xanh mà không đo gì là thứ đã xảy ra nhiều lần trên chính repo này.
"""

from __future__ import annotations

import httpx
import pytest

from tests.test_adminweb import _dang_nhap, _dung_admin, run_sql, scalar

#: Quyền cho từng đường: xem kho và nạp kho là HAI quyền khác nhau.
QUYEN_XEM = "/tonkho"
QUYEN_NAP = "/add_giffcode"

OWNER = 990_001

#: Cùng bộ giá trị đang chạy thật trên dev: `tanthu` chỉ dùng 10.000, `event` dùng năm mức.
#: Cặp này là tối thiểu để đo được hàng rào 4 (mệnh giá hợp lệ chung nhưng SAI loại).
ALLOWED = [5000, 10000, 20000, 50000, 88000]
PER_TYPE = {"tanthu": [10000], "event": [5000, 10000, 20000, 50000, 88000]}


async def _gieo_cau_hinh(*, nguong: int = 1_000_000) -> None:
    """Fixture panel `TRUNCATE` sạch `settings`, nên bài kiểm phải tự gieo ba khoá."""
    import json

    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi, sensitive)
             VALUES ('code.allowed_values', CAST(:allowed AS jsonb), 'json',
                     'Mệnh giá cho phép', false),
                    ('code.category_values', CAST(:per_type AS jsonb), 'json',
                     'Mệnh giá theo loại', false),
                    ('admin.dual_approval_threshold_vnd', CAST(:nguong AS jsonb), 'money_vnd',
                     'Ngưỡng duyệt hai người', false)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {"allowed": json.dumps(ALLOWED), "per_type": json.dumps(PER_TYPE), "nguong": str(nguong)},
    )
    from televip.services import settings_service

    settings_service.invalidate()


async def _chi_giu_quyen(*lenh: str) -> None:
    """Đặt quyền của vai trò `owner` thành ĐÚNG danh sách này, không hơn.

    Phải XOÁ chứ không chỉ thêm: `_dung_admin()` cấp sẵn cả `/tonkho` lẫn `/add_giffcode`,
    nên chỉ thêm thì bài "thiếu quyền nạp" sẽ xanh mà không đo gì.
    """
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


async def _vao(
    client: httpx.AsyncClient,
    *,
    quyen: tuple[str, ...] = (QUYEN_XEM, QUYEN_NAP),
    nguong: int = 10**6,
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


async def _nap(
    client: httpx.AsyncClient, csrf: str, *, loai: str = "tanthu", menh_gia: str = "10000", ma: str
) -> httpx.Response:
    return await client.post(
        "/kho/nap",
        data={"loai": loai, "menh_gia": menh_gia, "ma": ma, "_csrf": csrf},
        headers=_goc(),
    )


async def _dem_kho(loai: str = "tanthu") -> int:
    return await scalar("SELECT count(*) FROM codes WHERE code_type = :t", {"t": loai})


# ── Đường đi thành công ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nap_duoc_va_ghi_du_ba_thu(app_client: httpx.AsyncClient):
    """Mã vào kho · `batch_id` gán cho cả lô · một dòng `audit_log` có IP."""
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, ma="AAA111\nBBB222\nCCC333")
    assert r.status_code == 200, r.text[:400]

    assert await _dem_kho() == 3
    # Hai vế, và thiếu vế nào cũng lọt: "chỉ có một lô" đúng cả khi mới gán cho một mã,
    # còn "không mã nào thiếu lô" đúng cả khi mỗi mã một lô riêng. `/del_code` thu hồi
    # trọn lô qua `batch_id`, nên một mã sót lại ngoài lô là một mã không thu hồi được.
    assert (
        await scalar(
            "SELECT count(DISTINCT batch_id) FROM codes WHERE code_type = 'tanthu' "
            "AND batch_id IS NOT NULL"
        )
        == 1
    ), "cả lô phải mang chung một batch_id"
    assert await scalar("SELECT count(*) FROM codes WHERE batch_id IS NULL") == 0, (
        "mã không có batch_id thì /del_code không thu hồi trọn lô được"
    )

    assert (
        await scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'add_giffcode' AND actor_id = :a",
            {"a": OWNER},
        )
        == 1
    )
    sau = await scalar(
        "SELECT after FROM audit_log WHERE action = 'add_giffcode' ORDER BY log_id DESC LIMIT 1"
    )
    assert sau["added"] == 3
    assert sau["total_value_vnd"] == 30_000, "sổ phải ghi giá trị THẬT SỰ vào kho"
    assert "ip" in sau, "sổ kiểm toán của đường web phải có IP"


@pytest.mark.asyncio
async def test_ma_trung_trong_lo_va_trung_kho_deu_bi_bo_qua(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _nap(app_client, csrf, ma="AAA111")

    # AAA111 đã có trong kho; BBB222 lặp hai lần ngay trong lô này.
    r = await _nap(app_client, csrf, ma="AAA111, BBB222, BBB222, CCC333")
    assert r.status_code == 200

    assert await _dem_kho() == 3, "chỉ BBB222 và CCC333 được vào thêm"
    sau = await scalar(
        "SELECT after FROM audit_log WHERE action = 'add_giffcode' ORDER BY log_id DESC LIMIT 1"
    )
    assert sau["added"] == 2
    assert sau["skipped"] == 2, "một trùng kho + một trùng trong lô"
    # 2 mã × 10.000, KHÔNG phải 3 × 10.000 của lô đã gõ. Mã trùng không sinh nghĩa vụ nào,
    # và đây là con số dùng để đối soát tiền. Bài này là chỗ DUY NHẤT phân biệt được hai
    # cách tính đó — lô không có mã trùng thì hai cách cho cùng một số.
    assert sau["total_value_vnd"] == 20_000, "sổ phải đếm cái đã VÀO KHO, không phải cái đã gõ"


@pytest.mark.asyncio
async def test_phan_biet_hoa_thuong(app_client: httpx.AsyncClient):
    """`abc` và `ABC` là HAI mã. Gộp chúng là tự ý vứt một mã có giá trị tiền."""
    csrf = await _vao(app_client)

    await _nap(app_client, csrf, ma="abc123 ABC123")
    assert await _dem_kho() == 2


# ── Bốn hàng rào ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hang_rao_1_loai_code_la_bi_tu_choi(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, loai="khongcothat", ma="AAA111")
    assert r.status_code == 200
    assert "không hợp lệ" in r.text
    assert await scalar("SELECT count(*) FROM codes") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("menh_gia", ["", "abc", "0", "-5", "10k5", "1e4"])
async def test_hang_rao_2_menh_gia_khong_doc_duoc(app_client: httpx.AsyncClient, menh_gia: str):
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, menh_gia=menh_gia, ma="AAA111")
    assert r.status_code == 200
    assert await scalar("SELECT count(*) FROM codes") == 0, f"{menh_gia!r} lọt vào kho"


@pytest.mark.asyncio
async def test_hang_rao_3_menh_gia_ngoai_danh_sach_cho_phep(app_client: httpx.AsyncClient):
    """7.000đ đọc được, nhưng không nằm trong `code.allowed_values`."""
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, loai="event", menh_gia="7000", ma="AAA111")
    assert r.status_code == 200
    assert "danh sách cho phép" in r.text
    assert await scalar("SELECT count(*) FROM codes") == 0


@pytest.mark.asyncio
async def test_hang_rao_4_menh_gia_hop_le_nhung_SAI_loai(app_client: httpx.AsyncClient):
    """88.000 hợp lệ với `event`, KHÔNG hợp lệ với `tanthu`.

    Đây là hàng rào dễ chép hụt nhất: mệnh giá qua được hàng rào 3 nên trông như hợp lệ.
    """
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, loai="tanthu", menh_gia="88000", ma="AAA111")
    assert r.status_code == 200
    assert "không dùng mệnh giá" in r.text
    assert await scalar("SELECT count(*) FROM codes") == 0

    # Cùng mệnh giá đó với ĐÚNG loại thì phải qua — nếu không, bài trên xanh vì lý do khác.
    r2 = await _nap(app_client, csrf, loai="event", menh_gia="88000", ma="AAA111")
    assert r2.status_code == 200
    assert await scalar("SELECT count(*) FROM codes") == 1


@pytest.mark.asyncio
async def test_lo_rong_bi_tu_choi(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, ma="   \n\t  ")
    assert r.status_code == 200
    assert await scalar("SELECT count(*) FROM codes") == 0


# ── Ngưỡng duyệt hai người ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_vuot_nguong_duyet_hai_nguoi_thi_KHONG_ghi_gi(app_client: httpx.AsyncClient):
    """Ngưỡng 50.000 · 6 mã × 10.000 = 60.000 → từ chối, và kho phải TRỐNG."""
    csrf = await _vao(app_client, nguong=50_000)

    r = await _nap(app_client, csrf, ma="A1 A2 A3 A4 A5 A6")
    assert r.status_code == 200
    assert "duyệt hai người" in r.text
    assert await scalar("SELECT count(*) FROM codes") == 0
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'add_giffcode'") == 0


@pytest.mark.asyncio
async def test_dung_bang_nguong_thi_qua(app_client: httpx.AsyncClient):
    """5 × 10.000 = 50.000 = ngưỡng. So sánh là `>`, không phải `>=` — biên phải đi qua."""
    csrf = await _vao(app_client, nguong=50_000)

    await _nap(app_client, csrf, ma="A1 A2 A3 A4 A5")
    assert await _dem_kho() == 5


@pytest.mark.asyncio
async def test_nguong_tinh_SAU_khi_khu_trung(app_client: httpx.AsyncClient):
    """8 token nhưng chỉ 5 mã thật → 50.000, không phải 80.000. Không được chặn nhầm.

    Tính ngưỡng trên chuỗi thô là tính trên một tập không có thật: lô hợp lệ có mã lặp sẽ
    bị từ chối, và người vận hành sẽ đi tìm đường vòng.
    """
    csrf = await _vao(app_client, nguong=50_000)

    await _nap(app_client, csrf, ma="A1 A2 A3 A4 A5 A1 A2 A3")
    assert await _dem_kho() == 5, "khử trùng phải chạy TRƯỚC khi tính giá trị lô"


# ── Quyền và CSRF ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_khong_co_quyen_nap_thi_404_du_xem_duoc_kho(app_client: httpx.AsyncClient):
    """Xem kho và nạp kho là hai quyền tách rời. Có cái đầu không cho cái sau."""
    csrf = await _vao(app_client, quyen=(QUYEN_XEM,))

    assert (await app_client.get("/kho")).status_code == 200
    r = await _nap(app_client, csrf, ma="AAA111")
    assert r.status_code == 404, "thiếu /add_giffcode mà vẫn nạp được"
    assert await scalar("SELECT count(*) FROM codes") == 0


@pytest.mark.asyncio
async def test_khong_co_quyen_nap_thi_trang_kho_KHONG_hien_form(app_client: httpx.AsyncClient):
    await _vao(app_client, quyen=(QUYEN_XEM,))

    r = await app_client.get("/kho")
    assert r.status_code == 200
    assert "/kho/nap" not in r.text, "màn hình mời bấm một nút sẽ trả 404"


@pytest.mark.asyncio
async def test_co_quyen_nap_thi_trang_kho_HIEN_form(app_client: httpx.AsyncClient):
    """Cặp đôi của bài trên: không có bài này thì bài trên xanh cả khi form không bao giờ hiện."""
    await _vao(app_client)

    r = await app_client.get("/kho")
    assert "/kho/nap" in r.text
    # Ô chọn mệnh giá phải dựng từ CHÍNH cấu hình, kèm loại để lọc được.
    assert 'data-loai="tanthu"' in r.text


@pytest.mark.asyncio
async def test_nap_thieu_csrf_thi_404(app_client: httpx.AsyncClient):
    await _vao(app_client)

    r = await app_client.post(
        "/kho/nap", data={"loai": "tanthu", "menh_gia": "10000", "ma": "AAA111"}, headers=_goc()
    )
    assert r.status_code == 404
    assert await scalar("SELECT count(*) FROM codes") == 0


@pytest.mark.asyncio
async def test_nap_thieu_Origin_thi_404_du_token_dung(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)

    r = await app_client.post(
        "/kho/nap", data={"loai": "tanthu", "menh_gia": "10000", "ma": "AAA111", "_csrf": csrf}
    )
    assert r.status_code == 404
    assert await scalar("SELECT count(*) FROM codes") == 0


# ── Giữ lại cái vừa gõ ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lo_bi_tu_choi_thi_form_giu_nguyen_danh_sach_ma(app_client: httpx.AsyncClient):
    """Bắt dán lại vài trăm mã là lúc người ta dán nhầm."""
    csrf = await _vao(app_client)

    r = await _nap(app_client, csrf, menh_gia="7777", ma="MA-GIU-LAI-1 MA-GIU-LAI-2")
    assert "MA-GIU-LAI-1" in r.text
    assert "MA-GIU-LAI-2" in r.text


# ── Bot và web dùng CHUNG một đoạn mã ───────────────────────────────


@pytest.mark.asyncio
async def test_bot_va_web_goi_cung_mot_ham():
    """Không phải kiểm hành vi mà kiểm CẤU TRÚC: handler Telegram không được giữ bản sao.

    Hai bản sao của bốn hàng rào là hai bản sẽ lệch nhau — thường vào lúc một bên được sửa
    và người sửa không biết bên kia tồn tại.
    """
    import inspect

    from televip.apps.adminweb.routes import kho as web_kho
    from televip.apps.worker.handlers.admin import codes as bot_codes

    nguon_bot = inspect.getsource(bot_codes.handle_add_giffcode)
    nguon_web = inspect.getsource(web_kho.nap_kho)

    assert "kiem_lo_nap" in nguon_bot and "kiem_lo_nap" in nguon_web
    assert "stock_service.nap(" in nguon_bot and "stock.nap(" in nguon_web

    # Và không bên nào tự ĐỌC lại cấu hình mệnh giá để tự kiểm. Tìm lời gọi
    # `settings_service.get_*`, không tìm tên khoá: cả hai bên có quyền NHẮC tên khoá trong
    # câu báo lỗi, và cấm nhắc tên thì bài kiểm này chỉ ép người ta viết câu mơ hồ hơn.
    for ten, nguon in (("bot", nguon_bot), ("web", nguon_web)):
        assert "settings_service.get" not in nguon, f"{ten} tự đọc lại cấu hình mệnh giá"

    # Và `stock.nap()` chỉ nhận `LoNapKho` — kiểu mà chỉ `kiem_lo_nap()` dựng được. Đây là
    # thứ chặn bằng KIỂU, nên không đường vào nào ghi thẳng vào kho được.
    from televip.services import stock

    tham_so = inspect.signature(stock.nap).parameters
    assert tham_so["lo"].annotation == "LoNapKho"
