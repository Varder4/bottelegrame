"""Đăng nhập web cho admin — băm mật khẩu và vòng đời phiên.

Đây là **cửa duy nhất** vào panel quản trị, và panel nhìn thấy toàn bộ mã code chưa dùng —
mỗi mã chưa dùng là một tờ tiền. Nên các bài dưới đây kiểm cả những thứ mà một bộ kiểm thử
đăng nhập thông thường bỏ qua.

Bốn mệnh đề trọng tâm:

- Sai mật khẩu và **không có tài khoản** phải giống nhau ở cả kết quả lẫn THỜI GIAN. Khác
  nhau ở thời gian là đủ để kẻ dò lọc ra danh sách tên đăng nhập có thật trước khi thử.
- Thu hồi quyền admin làm **mất luôn** đường đăng nhập web, không đợi cookie hết hạn.
- Cookie bê sang máy khác (User-Agent lệch) thì phiên **chết ngay**, không chỉ bị từ chối.
- `admin_users` vẫn là nguồn quyền duy nhất: đặt mật khẩu **không** tạo ra admin.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.db.engine import session as db_session
from televip.services import admin_auth as aa
from tests.conftest import TEST_DATABASE_URL, _truncate_all, make_user

ADMIN_ID = 980_001
KHACH_ID = 980_002
MAT_KHAU = "matkhaudaimuoiky"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    try:
        async with db_session() as s:
            await _truncate_all(s)
            await s.commit()
        yield
    finally:
        await db_engine.dispose_engine()


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one_or_none()


async def _dung_admin(user_id: int = ADMIN_ID, *, role: str = "owner") -> None:
    async with db_session() as s:
        await make_user(s, user_id)
        await s.commit()
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u, :r, :u) "
        "ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL",
        {"u": user_id, "r": role},
    )


async def _dat_mat_khau(user_id: int = ADMIN_ID, ten: str = "chubot") -> bool:
    async with db_session() as s:
        ok = await aa.set_password(s, user_id=user_id, login_name=ten, password=MAT_KHAU)
        await s.commit()
    return ok


# ── Băm mật khẩu ────────────────────────────────────────────────────


def test_bam_va_kiem_lai():
    bam = aa.hash_password(MAT_KHAU)
    assert aa.verify_password(MAT_KHAU, bam)
    assert not aa.verify_password("saibet123456", bam)


def test_moi_lan_bam_ra_chuoi_khac_nhau():
    """Không muối thì hai admin đặt cùng mật khẩu có cùng chuỗi băm — lộ một là lộ cả hai."""
    assert aa.hash_password(MAT_KHAU) != aa.hash_password(MAT_KHAU)


def test_tu_choi_mat_khau_ngan_ngay_luc_DAT():
    """Chặn lúc đặt, không phải lúc đăng nhập — lúc đăng nhập thì đã muộn."""
    with pytest.raises(aa.PasswordTooShort):
        aa.hash_password("x" * (aa.MIN_PASSWORD_LEN - 1))
    # Đúng ngưỡng thì qua: đây là mốc dưới, không phải trần.
    assert aa.hash_password("x" * aa.MIN_PASSWORD_LEN)


def test_chuoi_bam_hong_trong_db_khong_thanh_duong_vao():
    """Dữ liệu hỏng phải trả False, không được ném ra ngoài và cũng không được cho qua."""
    for rac in ("", "rac", "scrypt$khong$phai$so$a$b", "bcrypt$1$2$3$a$b"):
        assert aa.verify_password(MAT_KHAU, rac) is False


def test_tham_so_doc_tu_chinh_chuoi_bam():
    """Nâng `_SCRYPT_N` sau này không được làm hỏng mật khẩu đã đặt bằng tham số cũ."""
    bam_yeu = aa.hash_password(MAT_KHAU).replace(f"scrypt${aa._SCRYPT_N}$", "scrypt$4096$", 1)
    # Chuỗi này khai n=4096; nếu hàm dùng hằng số của module thay vì đọc chuỗi, nó sẽ tính
    # ra khoá khác và trượt.
    assert aa.verify_password(MAT_KHAU, bam_yeu) is False, (
        "băm lại bằng n khác phải ra khoá khác — bài này chỉ chắc rằng hàm KHÔNG bỏ qua n"
    )


def test_khong_co_tai_khoan_ton_tuong_duong_mot_lan_bam():
    """Chênh lệch thời gian tự tố cáo tên đăng nhập nào có thật.

    Ngưỡng lỏng (nửa thời gian) vì máy chạy test có thể đang bận; cái cần bắt là trường
    hợp trả về NGAY LẬP TỨC do thoát sớm, không phải chênh vài mili-giây.
    """
    bam = aa.hash_password(MAT_KHAU)

    t0 = time.perf_counter()
    aa.verify_password("saibet123456", bam)
    co_tk = time.perf_counter() - t0

    t1 = time.perf_counter()
    ket_qua = aa.verify_password("saibet123456", None)
    khong_tk = time.perf_counter() - t1

    assert ket_qua is False
    assert khong_tk > co_tk * 0.5, (
        f"không có tài khoản chỉ tốn {khong_tk * 1000:.0f}ms so với {co_tk * 1000:.0f}ms — "
        "thoát sớm, và chênh lệch đó đủ để dò ra tên đăng nhập có thật"
    )


# ── Đặt mật khẩu và xác thực ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dat_mat_khau_roi_dang_nhap_duoc(wired):
    await _dung_admin()
    assert await _dat_mat_khau()

    async with db_session() as s:
        tk = await aa.authenticate(s, login_name="chubot", password=MAT_KHAU)
    assert tk is not None
    assert tk.user_id == ADMIN_ID
    assert tk.role == "owner"


@pytest.mark.asyncio
async def test_ten_dang_nhap_khong_phan_biet_hoa_thuong(wired):
    await _dung_admin()
    await _dat_mat_khau(ten="ChuBot")

    async with db_session() as s:
        assert await aa.authenticate(s, login_name="CHUBOT", password=MAT_KHAU) is not None
        assert await aa.authenticate(s, login_name="  chubot  ", password=MAT_KHAU) is not None


@pytest.mark.asyncio
async def test_sai_mat_khau_thi_truot(wired):
    await _dung_admin()
    await _dat_mat_khau()

    async with db_session() as s:
        assert await aa.authenticate(s, login_name="chubot", password="saibet123456") is None


@pytest.mark.asyncio
async def test_dat_mat_khau_KHONG_tao_ra_admin(wired):
    """`admin_users` là nguồn quyền duy nhất. Đặt mật khẩu không phải cách cấp quyền."""
    async with db_session() as s:
        await make_user(s, KHACH_ID)
        await s.commit()

    async with db_session() as s:
        ok = await aa.set_password(s, user_id=KHACH_ID, login_name="kegian", password=MAT_KHAU)
        await s.commit()

    assert ok is False
    assert await scalar("SELECT count(*) FROM admin_users") == 0
    async with db_session() as s:
        assert await aa.authenticate(s, login_name="kegian", password=MAT_KHAU) is None


@pytest.mark.asyncio
async def test_thu_hoi_quyen_lam_MAT_LUON_duong_dang_nhap(wired):
    """`/admin_del` đặt `revoked_at`; đường đăng nhập web phải chết theo, không đợi cookie."""
    await _dung_admin()
    await _dat_mat_khau()
    await run_sql("UPDATE admin_users SET revoked_at = now() WHERE user_id = :u", {"u": ADMIN_ID})

    async with db_session() as s:
        assert await aa.authenticate(s, login_name="chubot", password=MAT_KHAU) is None


# ── Phiên ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tao_phien_roi_doc_lai_duoc(wired):
    await _dung_admin()

    async with db_session() as s:
        cookie, csrf = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip="1.2.3.4")
        await s.commit()

    async with db_session() as s:
        phien = await aa.load_session(s, cookie_value=cookie, user_agent=UA)
        await s.commit()
    assert phien is not None
    assert phien.user_id == ADMIN_ID
    assert phien.csrf_token == csrf


@pytest.mark.asyncio
async def test_database_KHONG_giu_gia_tri_cookie(wired):
    """Dump database lọt ra ngoài không được chứa phiên sống nào."""
    await _dung_admin()
    async with db_session() as s:
        cookie, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        await s.commit()

    luu = await scalar("SELECT session_id FROM admin_sessions")
    assert luu != cookie, "giá trị cookie nằm nguyên văn trong database"
    assert len(luu) == 64, "phải là băm SHA-256 dạng hex"


@pytest.mark.asyncio
async def test_cookie_be_sang_may_khac_thi_GIET_phien(wired):
    """Không chỉ từ chối lượt này — phiên phải chết hẳn."""
    await _dung_admin()
    async with db_session() as s:
        cookie, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        await s.commit()

    async with db_session() as s:
        assert await aa.load_session(s, cookie_value=cookie, user_agent="May-Khac/1.0") is None
        await s.commit()

    assert await scalar("SELECT revoked_at FROM admin_sessions") is not None

    # ...và chủ thật của cookie cũng không dùng lại được.
    async with db_session() as s:
        assert await aa.load_session(s, cookie_value=cookie, user_agent=UA) is None


@pytest.mark.asyncio
async def test_cookie_bia_thi_khong_vao_duoc(wired):
    await _dung_admin()
    async with db_session() as s:
        assert await aa.load_session(s, cookie_value="bia-dat-hoan-toan", user_agent=UA) is None


@pytest.mark.asyncio
async def test_phien_qua_han_tuyet_doi_thi_chet(wired):
    await _dung_admin()
    async with db_session() as s:
        cookie, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        await s.commit()
    await run_sql("UPDATE admin_sessions SET expires_at = now() - interval '1 second'")

    async with db_session() as s:
        assert await aa.load_session(s, cookie_value=cookie, user_agent=UA) is None


@pytest.mark.asyncio
async def test_phien_nhan_roi_qua_lau_thi_chet(wired):
    """Hạn nhàn rỗi tách biệt với hạn tuyệt đối: rời máy quá lâu là mất phiên."""
    await _dung_admin()
    async with db_session() as s:
        cookie, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        await s.commit()
    await run_sql("UPDATE admin_sessions SET last_seen_at = now() - interval '31 minutes'")

    async with db_session() as s:
        assert await aa.load_session(s, cookie_value=cookie, user_agent=UA) is None
    # Hạn tuyệt đối vẫn còn — chứng minh đúng hạn NHÀN RỖI đã giết nó.
    assert await scalar("SELECT expires_at > now() FROM admin_sessions") is True


@pytest.mark.asyncio
async def test_moi_luot_doc_deu_cham_moc_hoat_dong(wired):
    await _dung_admin()
    async with db_session() as s:
        cookie, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        await s.commit()
    await run_sql("UPDATE admin_sessions SET last_seen_at = now() - interval '10 minutes'")

    async with db_session() as s:
        assert await aa.load_session(s, cookie_value=cookie, user_agent=UA) is not None
        await s.commit()

    assert (
        await scalar("SELECT last_seen_at > now() - interval '10 seconds' FROM admin_sessions")
        is True
    )


@pytest.mark.asyncio
async def test_thu_hoi_moi_phien_cua_mot_nguoi(wired):
    await _dung_admin()
    cookies = []
    async with db_session() as s:
        for _ in range(3):
            c, _csrf = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
            cookies.append(c)
        await s.commit()

    async with db_session() as s:
        so = await aa.revoke_all_sessions(s, user_id=ADMIN_ID)
        await s.commit()
    assert so == 3

    async with db_session() as s:
        for c in cookies:
            assert await aa.load_session(s, cookie_value=c, user_agent=UA) is None


@pytest.mark.asyncio
async def test_dang_xuat_chi_giet_dung_phien_do(wired):
    await _dung_admin()
    async with db_session() as s:
        c1, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        c2, _ = await aa.create_session(s, user_id=ADMIN_ID, user_agent=UA, ip=None)
        await s.commit()

    async with db_session() as s:
        await aa.revoke_session(s, cookie_value=c1)
        await s.commit()

    async with db_session() as s:
        assert await aa.load_session(s, cookie_value=c1, user_agent=UA) is None
        assert await aa.load_session(s, cookie_value=c2, user_agent=UA) is not None


# ── CSRF ────────────────────────────────────────────────────────────


def test_csrf_thieu_token_la_TRUOT_khong_phai_bo_qua():
    assert aa.check_csrf(None, "abc") is False
    assert aa.check_csrf("", "abc") is False
    assert aa.check_csrf("sai", "abc") is False
    assert aa.check_csrf("abc", "abc") is True
