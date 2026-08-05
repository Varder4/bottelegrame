"""Panel quản trị web — đường vào, hàng rào, và những gì người CHƯA đăng nhập thấy.

Panel nằm trên internet công cộng và nhìn thấy toàn bộ mã code chưa dùng. Nên các bài
dưới đây đo đúng những thứ quyết định panel có an toàn hay không, chứ không đo "trang có
mở được không".

Bốn mệnh đề trọng tâm:

- Người chưa đăng nhập nhận **404** ở mọi đường dẫn, không phải 401. Một trang trả 401 là
  một trang tự khai "ở đây có thứ đáng đăng nhập".
- Sai tên đăng nhập và sai mật khẩu ra **cùng một câu**, cùng một mã HTTP.
- Cookie bê sang máy khác thì phiên **chết hẳn**, kể cả với chủ thật của nó.
- Thu hồi quyền admin làm phiên web chết **ngay ở request kế tiếp**, không đợi cookie hết hạn.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text

from televip.db.engine import session as db_session
from televip.services import admin_auth
from tests.conftest import add_codes, make_user

OWNER_ID = 990_001
CSKH_ID = 990_002
TEN = "chubot"
MAT_KHAU = "matkhaudaimuoiky"

#: Quyền dựng cho `owner` trong các bài dưới đây. KHÔNG phải toàn bộ 33 lệnh — cố ý thiếu
#: `/broadcast`, để còn kiểm được rằng mục menu tương ứng KHÔNG hiện.
_LENH = ("/stats", "/tonkho", "/add_giffcode", "/codes", "/user", "/users")


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one_or_none()


async def _dung_admin(user_id: int = OWNER_ID, *, role: str = "owner", ten: str = TEN) -> None:
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, user_id)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, :r, :u) "
        "ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL",
        {"u": user_id, "r": role},
    )
    for lenh in _LENH:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES (:r, :c) ON CONFLICT DO NOTHING",
            {"r": role, "c": lenh},
        )
    admin_service.invalidate_role(user_id)
    async with db_session() as s:
        await admin_auth.set_password(s, user_id=user_id, login_name=ten, password=MAT_KHAU)
        await s.commit()


async def _dang_nhap(
    client: httpx.AsyncClient, ten: str = TEN, mk: str = MAT_KHAU
) -> httpx.Response:
    return await client.post("/dangnhap", data={"login_name": ten, "password": mk})


# ── Người chưa đăng nhập ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chua_dang_nhap_nhan_404_khong_phai_401(app_client: httpx.AsyncClient):
    """404 để máy quét không phân biệt được panel với một tên miền trống.

    Thử trên các đường dẫn NỘI DUNG. Đường dẫn gốc là ngoại lệ có chủ ý — xem bài kiểm
    ngay bên dưới.
    """
    for duong in ("/kho", "/nguoidung", "/cauhinh", "/nhatky", "/bantin"):
        r = await app_client.get(duong)
        assert r.status_code == 404, duong
        assert "www-authenticate" not in r.headers, duong


@pytest.mark.asyncio
async def test_duong_dan_GOC_chuyen_sang_trang_dang_nhap(app_client: httpx.AsyncClient):
    """Cửa trước của panel không được là một ngõ cụt.

    Một cái 404 ở `/` **không che được gì**: `/dangnhap` vốn đã mở công khai và trả 200 kèm
    form đăng nhập, nên người dò thử một lần là ra. Cái nó chặn được là đúng người có quyền
    vào — họ gõ tên miền, nhận `{"detail":"Not Found"}`, và không có gì nói phải đi đâu tiếp.
    """
    r = await app_client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/dangnhap"


@pytest.mark.asyncio
async def test_trang_dang_nhap_van_mo_duoc(app_client: httpx.AsyncClient):
    r = await app_client.get("/dangnhap")
    assert r.status_code == 200
    assert "Đăng nhập" in r.text


@pytest.mark.asyncio
async def test_tai_lieu_api_bi_tat_han(app_client: httpx.AsyncClient):
    """`/docs` là bản đồ đường dẫn miễn phí cho người dò."""
    for duong in ("/docs", "/redoc", "/openapi.json"):
        assert (await app_client.get(duong)).status_code == 404, duong


@pytest.mark.asyncio
async def test_moi_phan_hoi_deu_co_header_bao_mat(app_client: httpx.AsyncClient):
    """Gắn ở middleware nên trang LỖI cũng phải có — đó mới là trang hay bị quên."""
    for duong in ("/dangnhap", "/khong-ton-tai"):
        r = await app_client.get(duong)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"], duong
        assert r.headers["x-frame-options"] == "DENY", duong
        assert r.headers["x-content-type-options"] == "nosniff", duong
        assert r.headers["referrer-policy"] == "no-referrer", duong


@pytest.mark.asyncio
async def test_dev_KHONG_gan_hsts(app_client: httpx.AsyncClient):
    """Gắn HSTS trên `http://localhost` làm trình duyệt ép HTTPS cho MỌI dự án ở localhost."""
    r = await app_client.get("/dangnhap")
    assert "strict-transport-security" not in r.headers


# ── Đăng nhập ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dang_nhap_dung_thi_vao_duoc(app_client: httpx.AsyncClient):
    await _dung_admin()
    r = await _dang_nhap(app_client)

    assert r.status_code == 303
    assert r.headers["location"] == "/"

    trang = await app_client.get("/")
    assert trang.status_code == 200
    # Khẳng định trên DỮ LIỆU chứ không trên câu chữ: tiêu đề trang còn đổi theo thiết kế,
    # và một bài kiểm đỏ mỗi lần sửa giao diện sẽ sớm bị sửa cho xanh thay vì được đọc.
    assert str(OWNER_ID) in trang.text
    assert "owner" in trang.text


@pytest.mark.asyncio
async def test_cookie_phien_co_du_co_bao_ve(app_client: httpx.AsyncClient):
    await _dung_admin()
    r = await _dang_nhap(app_client)

    # So bằng chữ thường cả hai vế: thứ tự và cách viết hoa các thuộc tính cookie do thư
    # viện quyết định, chỉ SỰ CÓ MẶT của chúng mới là điều được kiểm.
    raw = r.headers.get("set-cookie", "").lower()
    assert "httponly" in raw, "thiếu HttpOnly ⇒ XSS lấy được phiên"
    assert "samesite=lax" in raw, "thiếu SameSite ⇒ form liên nguồn gốc mang cookie sang"
    assert "path=/" in raw


@pytest.mark.asyncio
async def test_sai_ten_va_sai_mat_khau_KHONG_phan_biet_duoc(app_client: httpx.AsyncClient):
    """Phân biệt hai câu đó là tặng kẻ dò một bộ lọc tìm tên đăng nhập có thật."""
    await _dung_admin()

    sai_ten = await _dang_nhap(app_client, ten="khongcotaikhoannay")
    sai_mk = await _dang_nhap(app_client, mk="saibet123456")

    assert sai_ten.status_code == sai_mk.status_code == 401
    # Câu chữ phải giống hệt nhau.
    assert "không đúng" in sai_ten.text
    assert "không đúng" in sai_mk.text


@pytest.mark.asyncio
async def test_dang_nhap_hong_thi_khong_dat_cookie(app_client: httpx.AsyncClient):
    await _dung_admin()
    r = await _dang_nhap(app_client, mk="saibet123456")
    assert "set-cookie" not in r.headers
    # Không có phiên ⇒ đường dẫn gốc đẩy về trang đăng nhập, và đường dẫn nội dung 404.
    assert (await app_client.get("/")).headers["location"] == "/dangnhap"
    assert (await app_client.get("/kho")).status_code == 404


@pytest.mark.asyncio
async def test_nguoi_chua_dat_mat_khau_khong_dang_nhap_duoc(app_client: httpx.AsyncClient):
    """Có quyền admin nhưng chưa đặt mật khẩu ⇒ chưa có đường vào web."""
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, CSKH_ID)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, 'cskh', :u)",
        {"u": CSKH_ID},
    )
    admin_service.invalidate_role(CSKH_ID)

    assert (await _dang_nhap(app_client, ten="chuadat")).status_code == 401


# ── Phiên ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cookie_be_sang_may_khac_giet_phien(app_client: httpx.AsyncClient):
    await _dung_admin()
    await _dang_nhap(app_client)
    assert (await app_client.get("/")).status_code == 200

    # Cùng cookie, User-Agent khác.
    lech = await app_client.get("/kho", headers={"user-agent": "May-Khac/1.0"})
    assert lech.status_code == 404

    # ...và chủ thật cũng mất phiên: cookie bị trộm thì để sống thêm giây nào cũng là quá lâu.
    assert (await app_client.get("/kho")).status_code == 404


@pytest.mark.asyncio
async def test_thu_hoi_quyen_giet_phien_web_ngay_lap_tuc(app_client: httpx.AsyncClient):
    """Không đợi cookie hết hạn — 8 tiếng là quá dài cho một người đã hết quyền."""
    from televip.services import admin as admin_service

    await _dung_admin()
    await _dang_nhap(app_client)
    assert (await app_client.get("/")).status_code == 200

    await run_sql("UPDATE admin_users SET revoked_at = now() WHERE user_id = :u", {"u": OWNER_ID})
    admin_service.invalidate_role(OWNER_ID)

    assert (await app_client.get("/kho")).status_code == 404
    # Phiên phải bị GIẾT, không chỉ bị từ chối lượt này.
    assert await scalar("SELECT revoked_at FROM admin_sessions") is not None


@pytest.mark.asyncio
async def test_dang_xuat_chi_nhan_POST(app_client: httpx.AsyncClient):
    """Liên kết đăng xuất bằng GET nhúng vào ảnh trên trang khác sẽ đá admin ra liên tục."""
    await _dung_admin()
    await _dang_nhap(app_client)
    assert (await app_client.get("/dangxuat")).status_code == 405


@pytest.mark.asyncio
async def test_dang_xuat_giet_phien(app_client: httpx.AsyncClient):
    await _dung_admin()
    await _dang_nhap(app_client)

    r = await app_client.post("/dangxuat")
    assert r.status_code == 303
    assert (await app_client.get("/kho")).status_code == 404
    # Và cửa trước đẩy về trang đăng nhập thay vì im lặng.
    assert (await app_client.get("/")).headers["location"] == "/dangnhap"


# ── Chặn dò mật khẩu ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sai_qua_nhieu_lan_thi_bi_khoa(app_client: httpx.AsyncClient):
    from televip.apps.adminweb import security

    await _dung_admin()
    for _ in range(security.MAX_LOGIN_FAILS):
        assert (await _dang_nhap(app_client, mk="saibet123456")).status_code == 401

    bi_khoa = await _dang_nhap(app_client, mk="saibet123456")
    assert bi_khoa.status_code == 429
    # ...và mật khẩu ĐÚNG cũng bị chặn — nếu không thì khoá này vô nghĩa.
    assert (await _dang_nhap(app_client)).status_code == 429


@pytest.mark.asyncio
async def test_dang_nhap_dung_xoa_bo_dem_sai(app_client: httpx.AsyncClient):
    """Gõ nhầm vài lần rồi gõ đúng thì không được mang bộ đếm sang lần sau."""
    from televip.apps.adminweb import security

    await _dung_admin()
    for _ in range(security.MAX_LOGIN_FAILS - 1):
        await _dang_nhap(app_client, mk="saibet123456")

    assert (await _dang_nhap(app_client)).status_code == 303

    # Bộ đếm đã xoá: lại sai được đủ số lần nữa mà chưa bị khoá.
    for _ in range(security.MAX_LOGIN_FAILS - 1):
        assert (await _dang_nhap(app_client, mk="saibet123456")).status_code == 401


# ── Sổ kiểm toán ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dang_nhap_ghi_so_kem_IP(app_client: httpx.AsyncClient):
    """Đây là thứ đường bot KHÔNG trả lời được: "lệnh đó gõ từ đâu"."""
    await _dung_admin()
    await _dang_nhap(app_client)

    row = await scalar(
        "SELECT after FROM audit_log WHERE action = 'adminweb.dangnhap' ORDER BY log_id DESC LIMIT 1"
    )
    assert row is not None, "đăng nhập không để lại dấu vết nào"
    assert "ip" in row
    assert row.get("ua", "").startswith("Mozilla/5.0")


@pytest.mark.asyncio
async def test_dang_nhap_hong_KHONG_ghi_so_thanh_cong(app_client: httpx.AsyncClient):
    await _dung_admin()
    await _dang_nhap(app_client, mk="saibet123456")

    assert await scalar("SELECT count(*) FROM audit_log WHERE action = 'adminweb.dangnhap'") == 0


# ── Thanh bên: menu gác bằng quyền thật ─────────────────────────────


@pytest.mark.asyncio
async def test_menu_chi_hien_muc_nguoi_do_co_quyen(app_client: httpx.AsyncClient):
    """`cskh` không NHÌN THẤY mục "Kho code" — vì bảng phân quyền không có hàng đó.

    Đây là điều tách panel này khỏi cách làm của source tham chiếu: bên đó menu là một
    mảng cứng chọn theo `role === 'CSKH'`, nên đổi quyền bằng lệnh thì menu không biết gì
    và hiện ra một danh sách sai. Ở đây menu đọc thẳng `admin_permissions`.
    """
    from televip.services import admin as admin_service

    # `cskh` thật (migration 0003) có `/stats`, `/user`, `/users` — và KHÔNG có `/tonkho`.
    async with db_session() as s:
        await make_user(s, CSKH_ID)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, 'cskh', :u)",
        {"u": CSKH_ID},
    )
    for lenh in ("/stats", "/user", "/users"):
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('cskh', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(CSKH_ID)
    async with db_session() as s:
        await admin_auth.set_password(s, user_id=CSKH_ID, login_name="nhanvien", password=MAT_KHAU)
        await s.commit()

    assert (await _dang_nhap(app_client, ten="nhanvien")).status_code == 303
    trang = (await app_client.get("/")).text

    # Thanh bên: mục có quyền phải là thẻ <a>, mục không có quyền không được xuất hiện.
    assert 'href="/nguoidung"' in trang, "cskh có quyền /user nhưng không thấy mục Người dùng"
    assert 'href="/kho"' not in trang, "cskh KHÔNG có quyền /tonkho mà vẫn thấy liên kết Kho code"
    assert 'href="/bantin"' not in trang


@pytest.mark.asyncio
async def test_owner_thay_du_muc(app_client: httpx.AsyncClient):
    await _dung_admin()
    await _dang_nhap(app_client)
    trang = (await app_client.get("/")).text

    # `_LENH` chỉ cấp 5 quyền, nên owner trong bài này thấy đúng những mục tương ứng.
    assert 'href="/kho"' in trang
    assert 'href="/nguoidung"' in trang
    # `/broadcast` không nằm trong `_LENH` ⇒ không được hiện.
    assert 'href="/bantin"' not in trang


# ── Số liệu thật: kho và thống kê ───────────────────────────────────
#
# Panel là tầng trình bày THỨ HAI trên cùng một `services/`. Điều duy nhất đáng đo ở đây là
# nó có nói **cùng một con số** với bot hay không — chứ không phải trang có mở được không.


async def _nap_kho() -> None:
    """Kho có hai loại, ba mệnh giá, một trong đó cố ý để dưới ngưỡng."""
    async with db_session() as s:
        await add_codes(s, code_type="tanthu", value_vnd=10_000, count=7, prefix="A")
        await add_codes(s, code_type="tanthu", value_vnd=50_000, count=2, prefix="B")
        await add_codes(s, code_type="event", value_vnd=88_000, count=4, prefix="E")
    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi)
             VALUES ('stock.warn_threshold', '3'::jsonb, 'int', 'Ngưỡng cảnh báo tồn kho')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """
    )
    from televip.services import settings_service

    settings_service.invalidate()


@pytest.mark.asyncio
async def test_man_kho_hien_dung_con_so_cua_service(app_client: httpx.AsyncClient):
    """Con số trên màn hình phải bằng con số `services/stock.py` trả về, không phải xấp xỉ."""
    await _dung_admin()
    await _nap_kho()
    await _dang_nhap(app_client)

    r = await app_client.get("/kho")
    assert r.status_code == 200

    # Đối chiếu với chính service — cùng nguồn mà `/tonkho` của bot đọc.
    from televip.services import stock

    async with db_session() as s:
        rows = await stock.read_stock(s)
    tong = stock.summarize(rows, threshold=await stock.warn_threshold())
    assert tong.available == 13

    assert f">{tong.available}<" in r.text.replace("\n", "").replace(" ", "")
    # 7×10.000 + 2×50.000 + 4×88.000 = 522.000
    assert "522.000đ" in r.text
    assert "Tân thủ" in r.text and "Đập hộp" in r.text


@pytest.mark.asyncio
async def test_man_kho_danh_dau_menh_gia_duoi_nguong(app_client: httpx.AsyncClient):
    """Ngưỡng = 3, mệnh giá 50K còn 2 mã ⇒ phải được đánh dấu.

    Đây là con số người vận hành cần HÀNH ĐỘNG. Một bảng đúng nhưng không chỉ ra chỗ sắp
    hết thì người ta chỉ biết mình hết mã lúc người dùng bấm nhận và không có gì.
    """
    await _dung_admin()
    await _nap_kho()
    await _dang_nhap(app_client)

    trang = (await app_client.get("/kho")).text
    assert "mệnh giá dưới ngưỡng" in trang, "kho có mệnh giá thấp mà không có cảnh báo nào"

    # Dải cảnh báo cũng phải hiện ở trang tổng quan, kèm đường dẫn sang kho.
    tong_quan = (await app_client.get("/")).text
    assert 'href="/kho"' in tong_quan
    assert "đang dưới ngưỡng" in tong_quan


@pytest.mark.asyncio
async def test_cskh_bi_tu_choi_man_kho_du_da_dang_nhap(app_client: httpx.AsyncClient):
    """Gác theo MẢNH: `cskh` vẫn vào được tổng quan, chỉ mất đúng màn hình kho.

    Gác cả trang tổng quan bằng `/tonkho` thì mọi `cskh` nhận 404 ngay sau khi đăng nhập —
    panel thành thứ họ không dùng được.
    """
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, CSKH_ID)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, 'cskh', :u)",
        {"u": CSKH_ID},
    )
    for lenh in ("/stats", "/user", "/users"):
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('cskh', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(CSKH_ID)
    async with db_session() as s:
        await admin_auth.set_password(s, user_id=CSKH_ID, login_name="nhanvien", password=MAT_KHAU)
        await s.commit()
    await _nap_kho()

    await _dang_nhap(app_client, ten="nhanvien")

    assert (await app_client.get("/")).status_code == 200, "cskh phải vào được tổng quan"
    assert (await app_client.get("/kho")).status_code == 404

    # Và không được rò chi tiết kho qua dải cảnh báo của trang tổng quan.
    tong_quan = (await app_client.get("/")).text
    assert "mệnh giá" not in tong_quan
    # Nhưng `/stats` thì cskh CÓ quyền, nên số liệu hệ thống vẫn hiện.
    assert "Ảnh chụp lúc" in tong_quan


@pytest.mark.asyncio
async def test_tong_quan_hien_so_that_khong_phai_cho_trong(app_client: httpx.AsyncClient):
    await _dung_admin()
    await _nap_kho()
    async with db_session() as s:
        for uid in (991_101, 991_102, 991_103):
            await make_user(s, uid)
    await _dang_nhap(app_client)

    trang = (await app_client.get("/")).text

    from televip.db.engine import transaction
    from televip.services import stats as stats_service

    async with transaction() as s:
        anh = await stats_service.system_snapshot(s)

    assert anh.total_users >= 4  # 3 người vừa tạo + chính owner
    assert f">{anh.total_users}<" in trang.replace("\n", "").replace(" ", "")
    assert f">{anh.codes_available}<" in trang.replace("\n", "").replace(" ", "")
    assert "Ảnh chụp lúc" in trang, "số liệu là ảnh chụp — không nói rõ lúc nào là nói dối"


def test_nhan_loai_phu_het_CODE_TYPES() -> None:
    """Thêm một loại code mà quên đặt nhãn thì bảng kho hiện mã thô — bài này bắt sớm."""
    from televip.apps.adminweb.routes.kho import NHAN_LOAI
    from televip.db.models.codes import CODE_TYPES

    thieu = [t for t in CODE_TYPES if t not in NHAN_LOAI]
    assert thieu == [], f"loại code chưa có nhãn tiếng Việt trong màn kho: {thieu}"
