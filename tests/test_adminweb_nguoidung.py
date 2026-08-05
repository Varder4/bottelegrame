"""Màn hình người dùng của panel — hàng rào, không phải bố cục.

Màn này hiện dữ liệu do 19.151 người **tự đặt**: `users.full_name` là văn bản tự do, không
lọc, không giới hạn độ dài. Nên phần lớn các bài ở đây đo hàng rào.

Bốn ca dễ trượt khỏi lưới, và cả bốn đều có bài kiểm riêng bên dưới:

- Người **vừa xác minh vừa bị khoá**. Test thường dựng hai người riêng nên không bao giờ
  chạm ca này; nếu template kiểm `verified_at` trước thì màn hình khoe "Đã xác minh" cho
  một người đang bị khoá.
- Người **đã được gỡ khoá**. Hai câu SQL join `user_bans` khác nhau một cách CỐ Ý; ai đó
  "đồng bộ hoá" chúng cho nhất quán sẽ xoá mất nhánh "đã gỡ lúc X".
- Ô tra cứu nhận **ký tự vô hình** dán từ Telegram — `.strip()` không bỏ U+200B.
- Tên chứa **HTML** hoặc **ký tự đảo chiều RTL**. Autoescape che cái đầu, không che cái sau.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from televip.db.engine import session as db_session
from televip.services import admin_auth
from tests.conftest import make_user

# `app_client` là fixture, nằm ở `conftest.py` — pytest tự tìm, không nhập.
from tests.test_adminweb import CSKH_ID, MAT_KHAU, OWNER_ID, _dang_nhap, _dung_admin, run_sql

NGUOI_THUONG = 992_001
NGUOI_KHOA = 992_002

#: Chuỗi có U+200B (zero-width space) ở đầu — đúng thứ dán từ Telegram hay kéo theo.
TOKEN_BAN = "​@KhachVIP "


async def _dung_nguoi_thuong() -> None:
    async with db_session() as s:
        await make_user(s, NGUOI_THUONG)
        await s.commit()
    await run_sql(
        "UPDATE users SET username = 'KhachVIP', full_name = :fn, verified_at = now() "
        "WHERE user_id = :u",
        {"u": NGUOI_THUONG, "fn": "Nguyễn Văn A"},
    )


async def _dung_vai_tro(role: str, ten: str, lenh: tuple[str, ...]) -> None:
    """Một admin phụ với ĐÚNG những quyền được liệt kê."""
    from televip.services import admin as admin_service

    async with db_session() as s:
        await make_user(s, CSKH_ID)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, :r, :u) "
        "ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL",
        {"u": CSKH_ID, "r": role},
    )
    for c in lenh:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES (:r, :c) ON CONFLICT DO NOTHING",
            {"r": role, "c": c},
        )
    admin_service.invalidate_role(CSKH_ID)
    async with db_session() as s:
        await admin_auth.set_password(s, user_id=CSKH_ID, login_name=ten, password=MAT_KHAU)
        await s.commit()


# ── Đường đi cơ bản ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_danh_sach_va_ho_so_mo_duoc(app_client: httpx.AsyncClient):
    await _dung_admin()
    await _dung_nguoi_thuong()
    await _dang_nhap(app_client)

    ds = await app_client.get("/nguoidung")
    assert ds.status_code == 200
    assert "KhachVIP" in ds.text
    assert str(NGUOI_THUONG) in ds.text

    hs = await app_client.get(f"/nguoidung/{NGUOI_THUONG}")
    assert hs.status_code == 200
    assert "Đã xác minh" in hs.text


# ── Ô tra cứu ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tra_cuu_chiu_duoc_hoa_thuong_va_ky_tu_vo_hinh(app_client: httpx.AsyncClient):
    """Năm cách gõ, cùng một người.

    `TOKEN_BAN` là ca quan trọng nhất: `.strip()` của Python **không** bỏ U+200B. Bài kiểm
    gõ chuỗi sạch sẽ luôn xanh, trong khi trên máy thật ô tra cứu báo "không tìm thấy" một
    người đang tồn tại và không có gì trên màn hình gợi ý vì sao.
    """
    await _dung_admin()
    await _dung_nguoi_thuong()
    await _dang_nhap(app_client)

    for token in ("@KhachVIP", "@khachvip", "KHACHVIP", TOKEN_BAN, str(NGUOI_THUONG)):
        r = await app_client.get("/nguoidung", params={"tim": token})
        assert r.status_code == 303, f"{token!r} phải tra ra người"
        assert r.headers["location"] == f"/nguoidung/{NGUOI_THUONG}", repr(token)


@pytest.mark.asyncio
async def test_tra_khong_ra_thi_o_lai_danh_sach(app_client: httpx.AsyncClient):
    """Bốn chuỗi không tra ra ai — và không chuỗi nào được làm sập trang.

    `"²"` và `"--5"` là hai cái bẫy đã có thật trong mã cũ: `"²".isdigit()` là True
    nhưng `int("²")` ném, còn `"--5".lstrip("-")` cho `"5"` là số nhưng `int("--5")` ném.
    Cả hai đều thoát ra thành 500 chứ không thành "không tìm thấy".
    """
    await _dung_admin()
    await _dung_nguoi_thuong()
    await _dang_nhap(app_client)

    for token in ("@khongcothat", "²", "--5", "@@@"):
        r = await app_client.get("/nguoidung", params={"tim": token})
        assert r.status_code == 200, f"{token!r}: tra hụt không được đẩy người ta vào ngõ cụt"
        assert "Không tìm thấy" in r.text, repr(token)
        assert "KhachVIP" in r.text, f"{token!r}: danh sách vẫn phải nằm dưới dải báo"


# ── Dữ liệu người dùng tự đặt ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ten_chua_ma_HTML_bi_escape(app_client: httpx.AsyncClient):
    await _dung_admin()
    async with db_session() as s:
        await make_user(s, NGUOI_THUONG)
        await s.commit()
    await run_sql(
        "UPDATE users SET username = NULL, full_name = :fn WHERE user_id = :u",
        {"u": NGUOI_THUONG, "fn": "<script>alert(1)</script>"},
    )
    await _dang_nhap(app_client)

    for duong in ("/nguoidung", f"/nguoidung/{NGUOI_THUONG}"):
        t = (await app_client.get(duong)).text
        assert "<script>alert(1)</script>" not in t, duong
        assert "&lt;script&gt;" in t, f"{duong}: tên phải hiện ra dạng đã escape"


@pytest.mark.asyncio
async def test_ten_boc_trong_bdi(app_client: httpx.AsyncClient):
    """U+202E đảo ngược thị giác cả dòng. Autoescape KHÔNG chặn — nó không phải HTML."""
    await _dung_admin()
    await _dung_nguoi_thuong()
    await _dang_nhap(app_client)

    t = (await app_client.get("/nguoidung")).text
    assert "<bdi>" in t, "tên người dùng phải nằm trong <bdi> để cách ly hướng đọc"


# ── Hai cờ trạng thái ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vua_xac_minh_vua_bi_khoa_thi_hien_DANG_BI_KHOA(app_client: httpx.AsyncClient):
    """Bot ưu tiên khoá. Web phải giống hệt — nếu không, CSKH trả lời sai cho khách."""
    await _dung_admin()
    async with db_session() as s:
        await make_user(s, NGUOI_KHOA)
        await s.commit()
    await run_sql(
        "UPDATE users SET username = 'bikhoa', verified_at = now() WHERE user_id = :u",
        {"u": NGUOI_KHOA},
    )
    await run_sql(
        "INSERT INTO user_bans (user_id, reason, banned_by, banned_at) "
        "VALUES (:u, :ly_do, :a, now())",
        {"u": NGUOI_KHOA, "a": OWNER_ID, "ly_do": "gian lận mời bạn"},
    )
    await _dang_nhap(app_client)

    hang = [d for d in (await app_client.get("/nguoidung")).text.split("<tr") if "bikhoa" in d]
    assert hang, "không thấy hàng của người bị khoá"
    assert "Đang bị khoá" in hang[0]
    assert "Đã xác minh" not in hang[0], "khoá phải thắng xác minh, đúng thứ tự của bot"

    hs = (await app_client.get(f"/nguoidung/{NGUOI_KHOA}")).text
    assert "Đang bị khoá" in hs
    assert "gian lận mời bạn" in hs


@pytest.mark.asyncio
async def test_da_go_khoa_danh_sach_sach_nhung_ho_so_van_nho(app_client: httpx.AsyncClient):
    """Hai câu SQL join `user_bans` khác nhau, và khác một cách CỐ Ý.

    Danh sách lọc `unbanned_at IS NULL` ngay trong `ON`; hồ sơ KHÔNG lọc, để còn in được
    nhánh "đã gỡ lúc X". Đồng bộ hoá hai câu cho "nhất quán" là xoá mất nhánh đó — và nó
    sẽ im lặng trở thành "chưa từng bị khoá".
    """
    await _dung_admin()
    async with db_session() as s:
        await make_user(s, NGUOI_KHOA)
        await s.commit()
    await run_sql("UPDATE users SET username = 'dagokhoa' WHERE user_id = :u", {"u": NGUOI_KHOA})
    await run_sql(
        "INSERT INTO user_bans (user_id, reason, banned_by, banned_at, unbanned_at) "
        "VALUES (:u, :ly_do, :a, now() - interval '2 days', now() - interval '1 day')",
        {"u": NGUOI_KHOA, "a": OWNER_ID, "ly_do": "nhầm"},
    )
    await _dang_nhap(app_client)

    hang = [d for d in (await app_client.get("/nguoidung")).text.split("<tr") if "dagokhoa" in d]
    assert hang and "Đang bị khoá" not in hang[0], "đã gỡ khoá mà danh sách vẫn báo đang khoá"

    assert "đã gỡ lúc" in (await app_client.get(f"/nguoidung/{NGUOI_KHOA}")).text


# ── Chỗ trống ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ho_so_nguoi_moi_hien_CAU_chu_khong_hien_so_0(app_client: httpx.AsyncClient):
    """Người vừa `/start` có bốn con số 0 — trông y hệt một hồ sơ đã đo xong."""
    await _dung_admin()
    await _dung_nguoi_thuong()
    await _dang_nhap(app_client)

    t = (await app_client.get(f"/nguoidung/{NGUOI_THUONG}")).text
    assert "Chưa mời ai" in t
    assert "Chưa nhận mã nào" in t
    assert "Chưa có điểm" in t
    # Ngoại lệ CÓ LÝ DO: `risk_score` NOT NULL default 0, CHECK 0..100 ⇒ 0 là số đo được.
    assert "Điểm rủi ro" in t


@pytest.mark.asyncio
async def test_id_hong_va_id_khong_ton_tai_deu_ra_404(app_client: httpx.AsyncClient):
    """404 chứ không 422: một mã khác 404 là tự khai đường dẫn này có tồn tại.

    `"²"` là ca đắt nhất trong danh sách. `"²".isdigit()` trả True nhưng
    `int("²")` NÉM — nên một hàm đọc id dùng `isdigit` sẽ trả 500 chứ không 404, và
    500 vừa lộ ra đường dẫn có thật vừa đổ một vệt stack trace vào log.
    """
    await _dung_admin()
    await _dang_nhap(app_client)

    for duong in (
        "/nguoidung/abc",
        "/nguoidung/-5",
        "/nguoidung/0",
        "/nguoidung/²",
        "/nguoidung/999999999",
    ):
        assert (await app_client.get(duong)).status_code == 404, duong


# ── Phân quyền ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cskh_dung_duoc_ca_hai_man(app_client: httpx.AsyncClient):
    """`cskh` là vai trò dùng màn này nhiều nhất — nó phải chạy được với họ."""
    await _dung_nguoi_thuong()
    await _dung_vai_tro("cskh", "nhanvien", ("/user", "/users"))
    await _dang_nhap(app_client, ten="nhanvien")

    assert (await app_client.get("/nguoidung")).status_code == 200
    assert (await app_client.get(f"/nguoidung/{NGUOI_THUONG}")).status_code == 200
    assert 'href="/nguoidung"' in (await app_client.get("/")).text


@pytest.mark.asyncio
async def test_chi_co_user_ma_khong_co_users_thi_khong_thay_muc_menu(
    app_client: httpx.AsyncClient,
):
    """Menu và route phải khai CÙNG một quyền.

    Menu khai `/user` còn route danh sách gác `/users` thì người chỉ có `/user` sẽ thấy mục
    menu rồi bấm vào nhận 404 — trông như panel hỏng, và không ai đoán ra nguyên nhân.
    """
    await _dung_nguoi_thuong()
    await _dung_vai_tro("ketoan", "ketoan1", ("/user",))
    await _dang_nhap(app_client, ten="ketoan1")

    assert (await app_client.get("/nguoidung")).status_code == 404
    assert (await app_client.get(f"/nguoidung/{NGUOI_THUONG}")).status_code == 200
    assert 'href="/nguoidung"' not in (await app_client.get("/")).text


# ── Bài kiểm đọc MÃ, không chạy app ─────────────────────────────────


#: Khối chú thích của Jinja. Bỏ chúng TRƯỚC khi soi, nếu không thì một dòng ghi chú viết
#: "không dùng |safe ở đây" lại bị tính là vi phạm — và bài kiểm đo chữ thay vì đo cách dùng.
_CHU_THICH_JINJA = re.compile(r"\{#.*?#\}", re.DOTALL)


def test_khong_template_nao_dung_safe() -> None:
    """`|safe` trên dữ liệu người dùng là XSS, và nó là thứ ai đó sẽ thêm vào để "in đẹp"."""
    from televip.apps.adminweb.app import TEMPLATES_DIR

    pham = []
    for p in Path(TEMPLATES_DIR).glob("*.html"):
        ma = _CHU_THICH_JINJA.sub("", p.read_text(encoding="utf-8"))
        if "|safe" in ma or "Markup(" in ma:
            pham.append(p.name)
    assert pham == [], f"template dùng |safe hoặc Markup(): {pham}"


def test_route_web_khong_tu_viet_SQL() -> None:
    """Tầng trình bày gọi service, không tự truy vấn.

    `check_architecture.py` luật 3 chỉ soi `UPDATE`/`DELETE` trên bảng tiền — một câu
    `SELECT` viết thẳng trong route đi qua nó im lặng. Bài này bịt đúng chỗ đó cho panel.
    """
    from televip.apps.adminweb import routes

    # Đo CÁCH DÙNG, không đo chữ: một docstring nhắc tới "SELECT" là ghi chú, không phải
    # truy vấn. Hai dấu hiệu dưới đây thì không thể là gì khác ngoài SQL viết tại chỗ.
    pham = []
    for p in Path(next(iter(routes.__path__))).glob("*.py"):
        noi_dung = p.read_text(encoding="utf-8")
        if "db.execute(" in noi_dung or "from sqlalchemy import text" in noi_dung:
            pham.append(p.name)
    assert pham == [], f"route tự viết SQL thay vì gọi services/: {pham}"
