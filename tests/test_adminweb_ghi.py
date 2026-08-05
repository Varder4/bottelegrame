"""Hai đường GHI của panel: câu chữ bot và cấu hình.

Đây là hai đường vào THỨ HAI cho những thao tác vốn chỉ có trong bot. Điều đáng đo không
phải "form có lưu được không" mà là bốn mệnh đề:

- **Không hàng rào nào bị chép.** Panel gọi thẳng `text_service.set_content()` và
  `settings_service.set_by_admin()`. Bài kiểm ở đây chứng minh hàng rào vẫn NỔ khi đi qua
  web — kể cả khi giao diện không dựng nút tương ứng.
- **CSRF chặn thật**, và chặn cả khi thiếu `Origin`.
- **Mỗi lần ghi để lại đúng một dòng `audit_log` có IP**, trong cùng giao dịch.
- **CRLF của form HTML bị chuẩn hoá.** Không chuẩn hoá thì mọi khoá thành "đã sửa" ngay
  lần lưu đầu tiên dù người ta không đổi một chữ nào.
"""

from __future__ import annotations

import httpx
import pytest

from televip.db.engine import session as db_session
from tests.test_adminweb import _dang_nhap, _dung_admin, run_sql, scalar

#: Khoá câu chữ dùng cho mọi bài dưới đây — chọn một khoá KHÔNG có biến bắt buộc để phần
#: lớn bài kiểm không phải mang theo một chuỗi mẫu dài.
_QUYEN = ("/noidung", "/xemnoidung", "/suanoidung", "/resetnoidung", "/cauhinh", "/setcauhinh")

#: Khoá cấu hình không nhạy cảm, kiểu số — dùng để thử đường ghi.
KHOA_SO = "leaderboard.top_n"


async def _gieo_cau_hinh() -> None:
    """Fixture của panel `TRUNCATE` sạch bảng `settings`, nên bài kiểm phải tự gieo.

    Hai khoá, cố ý khác nhau ở đúng một điểm: một khoá thường và một khoá `sensitive`. Đó
    là cặp tối thiểu để chứng minh hàng rào duyệt hai người có chặn thật.
    """
    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi, min_value, max_value, sensitive)
             VALUES ('leaderboard.top_n', '3'::jsonb, 'int', 'Số dòng mỗi khối BXH', 1, 20, false),
                    ('code.tanthu_value_vnd', '10000'::jsonb, 'money_vnd',
                     'Mệnh giá code tân thủ', 1000, 100000, true)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, sensitive = EXCLUDED.sensitive
        """
    )
    from televip.services import settings_service

    settings_service.invalidate()


async def _cap_quyen() -> None:
    from televip.services import admin as admin_service

    for lenh in _QUYEN:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(990_001)


async def _vao(client: httpx.AsyncClient) -> str:
    """Đăng nhập và trả về token CSRF của phiên."""
    await _dung_admin()
    await _cap_quyen()
    await _gieo_cau_hinh()
    await _dang_nhap(client)
    return await scalar(
        "SELECT csrf_token FROM admin_sessions WHERE revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


def _goc() -> dict[str, str]:
    """Header `Origin` khớp — thiếu nó là TRƯỢT, kể cả khi token đúng."""
    return {"origin": "http://testserver"}


async def _mot_khoa_cau_chu() -> str:
    from televip.services import text_service

    return next(k for k, spec in text_service.TEMPLATES.items() if not spec.content.count("{"))


# ── CSRF ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghi_khong_co_csrf_thi_404(app_client: httpx.AsyncClient):
    await _vao(app_client)

    r = await app_client.post(f"/cauhinh/{KHOA_SO}", data={"gia_tri": "9"}, headers=_goc())
    assert r.status_code == 404, "thiếu token CSRF mà vẫn ghi được"


@pytest.mark.asyncio
async def test_ghi_thieu_Origin_thi_404_du_token_dung(app_client: httpx.AsyncClient):
    """Hỏng theo hướng ĐÓNG: thiếu `Origin` là từ chối, không phải bỏ qua lớp kiểm."""
    csrf = await _vao(app_client)

    r = await app_client.post(f"/cauhinh/{KHOA_SO}", data={"gia_tri": "9", "_csrf": csrf})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_token_cua_phien_khac_KHONG_dung_duoc(app_client: httpx.AsyncClient):
    await _vao(app_client)

    r = await app_client.post(
        f"/cauhinh/{KHOA_SO}",
        data={"gia_tri": "9", "_csrf": "token-bia-ra-cho-du-dai-32-ky-tu"},
        headers=_goc(),
    )
    assert r.status_code == 404


# ── Cấu hình: đường ghi ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_doi_cau_hinh_ghi_ca_hai_cuon_so(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    cu = await scalar("SELECT value FROM settings WHERE key = :k", {"k": KHOA_SO})

    r = await app_client.post(
        f"/cauhinh/{KHOA_SO}", data={"gia_tri": "7", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303, r.text[:400]
    assert r.headers["location"] == f"/cauhinh/{KHOA_SO}"

    assert await scalar("SELECT value FROM settings WHERE key = :k", {"k": KHOA_SO}) == 7
    assert cu != 7

    # Sổ thứ nhất: settings_audit (cũ → mới).
    assert (
        await scalar(
            "SELECT count(*) FROM settings_audit WHERE key = :k AND new_value = '7'::jsonb",
            {"k": KHOA_SO},
        )
        == 1
    )
    # Sổ thứ hai: audit_log, KÈM IP. Thiếu IP thì không trả lời được "đổi từ đâu".
    dong = await scalar(
        "SELECT after->>'ip' FROM audit_log WHERE action = 'setcauhinh' AND entity_id = :k",
        {"k": KHOA_SO},
    )
    assert dong == "127.0.0.1", "audit_log của thao tác web phải có IP"


@pytest.mark.asyncio
async def test_gia_tri_sai_kieu_giu_nguyen_gia_tri_cu(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    cu = await scalar("SELECT value FROM settings WHERE key = :k", {"k": KHOA_SO})

    r = await app_client.post(
        f"/cauhinh/{KHOA_SO}", data={"gia_tri": "ba", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 200, "sai kiểu phải trả lại form, không phải 500"
    assert "không đúng kiểu" in r.text
    assert await scalar("SELECT value FROM settings WHERE key = :k", {"k": KHOA_SO}) == cu


@pytest.mark.asyncio
async def test_khoa_sensitive_bi_service_chan_du_gui_POST_tay(app_client: httpx.AsyncClient):
    """Giao diện không dựng form cho khoá này — nhưng hàng rào KHÔNG nằm ở giao diện.

    Bài này gửi thẳng POST như một người bỏ qua trang. Nếu chỉ có giao diện chặn thì mười
    khoá đắt nhất hệ thống đổi được bằng một dòng `curl`.
    """
    csrf = await _vao(app_client)
    khoa = await scalar("SELECT key FROM settings WHERE sensitive ORDER BY key LIMIT 1")
    assert khoa, "seed phải có khoá sensitive"
    cu = await scalar("SELECT value FROM settings WHERE key = :k", {"k": khoa})

    r = await app_client.post(
        f"/cauhinh/{khoa}", data={"gia_tri": "1", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 200
    assert "duyệt thứ hai" in r.text
    assert await scalar("SELECT value FROM settings WHERE key = :k", {"k": khoa}) == cu


@pytest.mark.asyncio
async def test_khoa_khong_ton_tai_ra_404(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    r = await app_client.post(
        "/cauhinh/khong.co.khoa.nay", data={"gia_tri": "1", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 404


# ── Câu chữ: đường ghi ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sua_cau_chu_ghi_so_va_doi_noi_dung(app_client: httpx.AsyncClient):
    from televip.services import text_service

    csrf = await _vao(app_client)
    khoa = await _mot_khoa_cau_chu()

    r = await app_client.post(
        f"/cauchu/{khoa}", data={"noi_dung": "Xin chào bản mới", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303, r.text[:400]

    async with db_session() as s:
        info = await text_service.get_info(khoa, db=s)
    assert info.content == "Xin chào bản mới"
    assert info.customized is True

    assert (
        await scalar(
            "SELECT after->>'ip' FROM audit_log "
            "WHERE action = 'adminweb.suanoidung' AND entity_id = :k",
            {"k": khoa},
        )
        == "127.0.0.1"
    )


@pytest.mark.asyncio
async def test_CRLF_cua_form_bi_chuan_hoa(app_client: httpx.AsyncClient):
    """Trình duyệt gửi `\\r\\n` theo đúng chuẩn HTML.

    Ghi thẳng vào database thì **mọi khoá đều thành "đã sửa"** ngay lần lưu đầu tiên dù
    người ta không đổi một chữ nào — vì bản mặc định trong mã dùng `\\n`. Và mỗi dòng dài
    thêm một byte trong mọi tin nhắn gửi đi sau đó.
    """
    from televip.services import text_service

    csrf = await _vao(app_client)
    khoa = await _mot_khoa_cau_chu()

    await app_client.post(
        f"/cauchu/{khoa}", data={"noi_dung": "dòng 1\r\ndòng 2\r\n", "_csrf": csrf}, headers=_goc()
    )

    async with db_session() as s:
        info = await text_service.get_info(khoa, db=s)
    assert "\r" not in info.content, "CRLF phải được chuẩn hoá trước khi vào database"
    assert info.content == "dòng 1\ndòng 2\n"


@pytest.mark.asyncio
async def test_thieu_bien_bat_buoc_bi_tu_choi_va_GIU_chu_da_go(app_client: httpx.AsyncClient):
    """Thiếu biến là từ chối, không phải cảnh báo — và không được làm mất chữ đã soạn."""
    from televip.services import text_service

    csrf = await _vao(app_client)
    khoa = next(k for k, s in text_service.TEMPLATES.items() if s.required_vars)
    async with db_session() as s:
        cu = (await text_service.get_info(khoa, db=s)).content

    r = await app_client.post(
        f"/cauchu/{khoa}", data={"noi_dung": "mất hết biến rồi", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 200, "phải trả lại form, không phải 500 và không phải 303"
    assert "Chưa lưu được" in r.text
    assert "mất hết biến rồi" in r.text, "chữ vừa soạn phải còn nguyên trong ô"

    async with db_session() as s:
        assert (await text_service.get_info(khoa, db=s)).content == cu


@pytest.mark.asyncio
async def test_ve_ban_mac_dinh(app_client: httpx.AsyncClient):
    from televip.services import text_service

    csrf = await _vao(app_client)
    khoa = await _mot_khoa_cau_chu()
    mac_dinh = text_service.default_content(khoa)

    await app_client.post(
        f"/cauchu/{khoa}", data={"noi_dung": "bản tạm", "_csrf": csrf}, headers=_goc()
    )
    r = await app_client.post(f"/cauchu/{khoa}/mac-dinh", data={"_csrf": csrf}, headers=_goc())
    assert r.status_code == 303

    async with db_session() as s:
        info = await text_service.get_info(khoa, db=s)
    assert info.content == mac_dinh
    assert info.customized is False
    # KHÔNG xoá dòng: đường quay lui phải còn nguyên.
    assert await scalar("SELECT count(*) FROM message_templates WHERE key = :k", {"k": khoa}) >= 1


@pytest.mark.asyncio
async def test_khoa_cau_chu_la_ra_404(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    for duong in ("/cauchu/khong.co.khoa", "/cauchu/../../etc/passwd"):
        assert (await app_client.get(duong)).status_code == 404, duong
    r = await app_client.post(
        "/cauchu/khong.co.khoa", data={"noi_dung": "x", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 404


# ── Phân quyền ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_xem_duoc_nhung_khong_sua_duoc_thi_khong_co_form(app_client: httpx.AsyncClient):
    """Vai trò chỉ có quyền XEM phải thấy nội dung mà không thấy nút Lưu."""
    from televip.services import admin as admin_service

    await _dung_admin()
    for lenh in ("/noidung", "/xemnoidung", "/cauhinh"):
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(990_001)
    await _gieo_cau_hinh()
    await _dang_nhap(app_client)
    khoa = await _mot_khoa_cau_chu()

    t = (await app_client.get(f"/cauchu/{khoa}")).text
    assert "không có quyền" in t
    assert 'name="noi_dung"' not in t, "không có quyền sửa mà vẫn dựng ô soạn"

    t = (await app_client.get(f"/cauhinh/{KHOA_SO}")).text
    assert 'name="gia_tri"' not in t


@pytest.mark.asyncio
async def test_man_hinh_in_dung_con_so_TTL_cua_service(app_client: httpx.AsyncClient):
    """Viết cứng 60 ở web là dựng một hằng số thứ hai — đổi TTL thì web nói dối."""
    from televip.services import settings_service, text_service

    await _vao(app_client)
    khoa = await _mot_khoa_cau_chu()

    t = (await app_client.get(f"/cauchu/{khoa}")).text
    assert f"{int(text_service.CACHE_TTL_SECONDS)} giây" in t

    t = (await app_client.get(f"/cauhinh/{KHOA_SO}")).text
    assert f"{int(settings_service.CACHE_TTL_SECONDS)} giây" in t
