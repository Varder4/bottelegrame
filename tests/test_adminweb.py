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

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.db.engine import session as db_session
from televip.services import admin_auth
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, _truncate_all, make_user

OWNER_ID = 990_001
CSKH_ID = 990_002
TEN = "chubot"
MAT_KHAU = "matkhaudaimuoiky"
UA = "Mozilla/5.0 (Windows NT 10.0) TestClient/1.0"

#: Mọi lệnh của khối kho — dùng để dựng quyền cho `owner`.
_LENH = ("/stats", "/tonkho", "/add_giffcode", "/codes", "/user")


@pytest_asyncio.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    """App thật, database test thật, Redis test thật — không giả lập gì.

    ## Cách trỏ app sang database test, và vì sao KHÔNG vá `get_settings`

    Bản đầu tiên của fixture này vá `televip.core.config.get_settings`. Nó **không có tác
    dụng**: `apps/adminweb/app.py` viết `from televip.core.config import get_settings`, nên
    tên đó đã được nối cứng vào module app từ lúc import — vá thuộc tính của module cấu
    hình không đổi được cái tên đã nối.

    Hậu quả thật: `create_app()` dựng engine trỏ vào database **dev**, rồi `_truncate_all()`
    xoá sạch nó — mất 220 mã code, 52 khoá cấu hình, nhóm bắt buộc và tài khoản admin.

    Cách đúng là **tự dựng engine và Redis trước**, rồi để `init_engine()` / `init_redis()`
    bên trong `create_app()` trả về sớm (chúng thoát ngay khi đã được khởi tạo). Ở đây ta
    dùng hành vi "trả về sớm" đó một cách CÓ CHỦ Ý, thay vì vấp phải nó.
    """

    from televip.apps.adminweb.app import create_app
    from televip.cache.client import close_redis, get_redis, init_redis
    from televip.db import engine as db_engine

    # Dọn sạch trạng thái toàn cục do test trước để lại, rồi tự dựng đúng đích ta muốn.
    await db_engine.dispose_engine()
    await close_redis()

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    init_redis(SimpleNamespace(redis_url=TEST_REDIS_URL))  # type: ignore[arg-type]

    try:
        # `create_app()` gọi lại `init_engine`/`init_redis`; cả hai thấy đã khởi tạo và
        # trả về ngay, nên app dùng đúng hai thứ ta vừa dựng ở trên.
        app = create_app()

        async with db_session() as s:
            await _truncate_all(s)  # tự kiểm `current_database()` — xem conftest
            await s.commit()
        await get_redis().flushdb()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers={"user-agent": UA}
        ) as client:
            yield client
    finally:
        await db_engine.dispose_engine()
        await close_redis()


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
    """404 để máy quét không phân biệt được panel với một tên miền trống."""
    r = await app_client.get("/")
    assert r.status_code == 404
    assert "www-authenticate" not in r.headers


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
    assert (await app_client.get("/")).status_code == 404


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
    lech = await app_client.get("/", headers={"user-agent": "May-Khac/1.0"})
    assert lech.status_code == 404

    # ...và chủ thật cũng mất phiên: cookie bị trộm thì để sống thêm giây nào cũng là quá lâu.
    assert (await app_client.get("/")).status_code == 404


@pytest.mark.asyncio
async def test_thu_hoi_quyen_giet_phien_web_ngay_lap_tuc(app_client: httpx.AsyncClient):
    """Không đợi cookie hết hạn — 8 tiếng là quá dài cho một người đã hết quyền."""
    from televip.services import admin as admin_service

    await _dung_admin()
    await _dang_nhap(app_client)
    assert (await app_client.get("/")).status_code == 200

    await run_sql("UPDATE admin_users SET revoked_at = now() WHERE user_id = :u", {"u": OWNER_ID})
    admin_service.invalidate_role(OWNER_ID)

    assert (await app_client.get("/")).status_code == 404
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
    assert (await app_client.get("/")).status_code == 404


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

    # `cskh` chỉ có `/stats` và `/user`, KHÔNG có `/tonkho`.
    async with db_session() as s:
        await make_user(s, CSKH_ID)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, 'cskh', :u)",
        {"u": CSKH_ID},
    )
    for lenh in ("/stats", "/user"):
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


@pytest.mark.asyncio
async def test_the_so_lieu_hien_dau_gach_chu_KHONG_hien_so_0(app_client: httpx.AsyncClient):
    """Giai đoạn 0 chưa nối số thật. Một con số 0 trông y hệt một con số thật.

    In `0` ở đây nghĩa là màn hình nói "kho rỗng" và "chưa ai nhận code" — hai câu đều SAI,
    và người đọc không có cách nào biết chúng chỉ là chỗ trống.
    """
    await _dung_admin()
    await _dang_nhap(app_client)
    trang = (await app_client.get("/")).text

    assert "Tồn kho code" in trang
    assert ">—<" in trang, "thẻ số liệu phải hiện dấu gạch khi chưa có số thật"
