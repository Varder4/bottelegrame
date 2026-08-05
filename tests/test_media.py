"""Đường ảnh: web ghi bytes, bot tải lên Telegram, mọi lần gửi sau dùng lại `file_id`.

Điều đắt nhất mà đường này ngăn: hệ cũ mở lại `images/2.jpg` (727.592 byte) cho **từng**
người nhận — **13,9 GB** băng thông cho một đợt 19.151 người. Với `file_id`, Telegram chỉ
copy tham chiếu và tổng băng thông là 0.

Bốn mệnh đề được đo:

- **Định dạng nhận theo BYTE ĐẦU TỆP**, không theo phần mở rộng và không theo `Content-Type`
  do trình duyệt khai — cả hai thứ sau đều do người gửi tự đặt.
- **Cùng một tấm ảnh tải lại không tốn thêm một lượt gọi Telegram nào** (vân tay `sha256`).
- **Bytes biến mất ngay khi có `file_id`** — chúng chỉ là phòng chờ.
- **Không tạo được nháp khi ảnh chưa sẵn sàng**, và hàng rào đó ở tầng dữ liệu.
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.db.engine import session as db_session
from televip.db.engine import transaction
from televip.services import media
from tests.conftest import TEST_DATABASE_URL, _truncate_all


@pytest_asyncio.fixture
async def wired():
    """Database test thật, bảng sạch. Không giả lập gì — `media_uploads` là chỗ bytes ở."""
    from types import SimpleNamespace

    from televip.db import engine as db_engine

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    async with db_session() as s:
        await _truncate_all(s)
        await s.commit()
    try:
        yield
    finally:
        # BẮT BUỘC dọn: mỗi bài kiểm async có event loop riêng, nên một pool sống sót qua
        # ranh giới bài kiểm sẽ giữ kết nối gắn với loop đã đóng — và bài kế tiếp chết bằng
        # `'NoneType' object has no attribute 'send'`, một câu không nói gì về nguyên nhân.
        await db_engine.dispose_engine()


# Byte đầu tệp thật của ba định dạng Telegram nhận.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 64

ADMIN = 996_001


async def _dem_asset() -> int:
    async with db_session() as s:
        return (await s.execute(text("SELECT count(*) FROM media_assets"))).scalar_one()


# ── Nhận dạng định dạng ─────────────────────────────────────────────


def test_nhan_dang_theo_byte_dau_tep() -> None:
    assert media._kieu_anh(JPEG) == "image/jpeg"
    assert media._kieu_anh(PNG) == "image/png"
    assert media._kieu_anh(WEBP) == "image/webp"


def test_tu_choi_tep_khong_phai_anh() -> None:
    """Một tệp `.jpg` chứa HTML vẫn là HTML.

    Telegram sẽ từ chối nó — nhưng sau khi ta đã dựng xong tệp đích 19.151 dòng. Bắt ở đây
    rẻ hơn nhiều.
    """
    for du_lieu in (b"<html>xin chao</html>", b"%PDF-1.4", b"GIF89a", b"MZ\x90\x00"):
        with pytest.raises(media.AnhKhongHopLe):
            media._kieu_anh(du_lieu)


def test_RIFF_khong_phai_WEBP_bi_tu_choi() -> None:
    """`RIFF` cũng là byte đầu của WAV và AVI — kiểm thêm bốn byte ở vị trí 8."""
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64
    with pytest.raises(media.AnhKhongHopLe):
        media._kieu_anh(wav)


# ── Phòng chờ ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anh_moi_vao_hang_cho_va_KHONG_goi_telegram(wired) -> None:
    async with transaction() as db:
        kq = await media.xin_tai_anh(db, du_lieu=JPEG, ten_tep="a.jpg", created_by=ADMIN)

    assert kq.state == "pending"
    assert kq.file_id is None
    assert not kq.san_sang, "ảnh mới KHÔNG được coi là sẵn sàng"
    assert await _dem_asset() == 0, "chưa gọi Telegram thì chưa có asset nào"

    async with db_session() as s:
        con = (
            await s.execute(
                text("SELECT length(du_lieu) FROM media_uploads WHERE upload_id = :u"),
                {"u": kq.upload_id},
            )
        ).scalar_one()
    assert con == len(JPEG), "bytes phải nằm lại chờ bot tải lên"


@pytest.mark.asyncio
async def test_anh_qua_co_va_anh_rong_bi_tu_choi(wired) -> None:
    async with transaction() as db:
        with pytest.raises(media.AnhKhongHopLe, match="rỗng"):
            await media.xin_tai_anh(db, du_lieu=b"", ten_tep="a.jpg", created_by=ADMIN)

    qua_co = JPEG + b"\x00" * media.MAX_ANH_BYTES
    async with transaction() as db:
        with pytest.raises(media.AnhKhongHopLe, match="vượt trần"):
            await media.xin_tai_anh(db, du_lieu=qua_co, ten_tep="a.jpg", created_by=ADMIN)


# ── Vòng đời đầy đủ ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vong_doi_day_du_va_bytes_BIEN_MAT(wired) -> None:
    """Bytes chỉ là phòng chờ. Giữ chúng sau khi có `file_id` là giữ vài MB cho một thứ
    không ai đọc nữa — và mỗi lần sao lưu database phải cõng theo."""
    async with transaction() as db:
        kq = await media.xin_tai_anh(db, du_lieu=PNG, ten_tep="b.png", created_by=ADMIN)

    async with transaction() as db:
        viec = await media.nhan_viec(db)
    assert [v.upload_id for v in viec] == [kq.upload_id]
    assert viec[0].du_lieu == PNG, "job phải nhận đúng bytes đã lưu"
    assert viec[0].attempts == 1

    async with transaction() as db:
        key = await media.danh_dau_xong(
            db,
            upload_id=kq.upload_id,
            sha256=kq.sha256,
            file_id="AgACAgUAAxk-file-id-gia",
            width=800,
            height=600,
        )
    assert key == f"sha256:{kq.sha256}"

    async with db_session() as s:
        tt = await media.trang_thai(s, kq.upload_id)
        con = (
            await s.execute(
                text("SELECT du_lieu FROM media_uploads WHERE upload_id = :u"),
                {"u": kq.upload_id},
            )
        ).scalar_one()
    assert tt is not None and tt.san_sang
    assert tt.file_id == "AgACAgUAAxk-file-id-gia"
    assert con is None, "bytes phải bị xoá ngay khi có file_id"


@pytest.mark.asyncio
async def test_cung_anh_tai_lai_KHONG_goi_telegram_lan_hai(wired) -> None:
    """Vân tay `sha256` — đây là lý do `media_assets` có cột đó từ migration đầu tiên."""
    async with transaction() as db:
        lan1 = await media.xin_tai_anh(db, du_lieu=WEBP, ten_tep="c.webp", created_by=ADMIN)
    async with transaction() as db:
        await media.danh_dau_xong(
            db,
            upload_id=lan1.upload_id,
            sha256=lan1.sha256,
            file_id="file-id-lan-1",
            width=100,
            height=100,
        )

    # Cùng bytes, tên tệp khác — vân tay là NỘI DUNG, không phải tên.
    async with transaction() as db:
        lan2 = await media.xin_tai_anh(db, du_lieu=WEBP, ten_tep="ten-khac.webp", created_by=ADMIN)

    assert lan2.san_sang, "ảnh đã có sẵn phải trả file_id NGAY"
    assert lan2.file_id == "file-id-lan-1"
    assert lan2.state == "done"

    async with transaction() as db:
        assert await media.nhan_viec(db) == [], "không được xếp thêm việc cho bot"
    assert await _dem_asset() == 1, "một tấm ảnh chỉ có một hàng trong danh mục"

    async with db_session() as s:
        con = (
            await s.execute(
                text("SELECT du_lieu FROM media_uploads WHERE upload_id = :u"),
                {"u": lan2.upload_id},
            )
        ).scalar_one()
    assert con is None, "lần hai không giữ bytes — đã có file_id rồi"


@pytest.mark.asyncio
async def test_van_tay_la_NOI_DUNG_khong_phai_ten_tep(wired) -> None:
    a = JPEG + b"noi dung A"
    b = JPEG + b"noi dung B"
    async with transaction() as db:
        ka = await media.xin_tai_anh(db, du_lieu=a, ten_tep="cung-ten.jpg", created_by=ADMIN)
        kb = await media.xin_tai_anh(db, du_lieu=b, ten_tep="cung-ten.jpg", created_by=ADMIN)
    assert ka.sha256 != kb.sha256
    assert ka.sha256 == hashlib.sha256(a).hexdigest()


# ── Hỏng ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loi_tam_thoi_thi_thu_lai_va_GIU_bytes(wired) -> None:
    """Lỗi mạng không được làm mất một tấm ảnh admin vừa chọn."""
    async with transaction() as db:
        kq = await media.xin_tai_anh(db, du_lieu=JPEG, ten_tep="d.jpg", created_by=ADMIN)
    async with transaction() as db:
        viec = await media.nhan_viec(db)
        await media.danh_dau_hong(
            db, upload_id=kq.upload_id, attempts=viec[0].attempts, loi="mang chet", vinh_vien=False
        )

    async with db_session() as s:
        tt = await media.trang_thai(s, kq.upload_id)
        con = (
            await s.execute(
                text("SELECT length(du_lieu) FROM media_uploads WHERE upload_id = :u"),
                {"u": kq.upload_id},
            )
        ).scalar_one()
    assert tt is not None and tt.state == "pending", "lỗi tạm thời KHÔNG được bỏ cuộc"
    assert con == len(JPEG), "bytes phải còn để thử lại"


@pytest.mark.asyncio
async def test_loi_vinh_vien_thi_bo_cuoc_ngay(wired) -> None:
    """Telegram từ chối tấm ảnh, hoặc bot không ở trong nhóm admin — thử lại 5 lần vô ích."""
    async with transaction() as db:
        kq = await media.xin_tai_anh(db, du_lieu=JPEG, ten_tep="e.jpg", created_by=ADMIN)
    async with transaction() as db:
        await media.nhan_viec(db)
        await media.danh_dau_hong(
            db, upload_id=kq.upload_id, attempts=1, loi="Telegram tu choi", vinh_vien=True
        )

    async with db_session() as s:
        tt = await media.trang_thai(s, kq.upload_id)
    assert tt is not None and tt.state == "failed"
    assert tt.last_error is not None and "tu choi" in tt.last_error


@pytest.mark.asyncio
async def test_het_luot_thu_thi_bo_cuoc(wired) -> None:
    async with transaction() as db:
        kq = await media.xin_tai_anh(db, du_lieu=JPEG, ten_tep="f.jpg", created_by=ADMIN)
    async with transaction() as db:
        await media.danh_dau_hong(
            db,
            upload_id=kq.upload_id,
            attempts=media.MAX_LUOT_THU,
            loi="het luot",
            vinh_vien=False,
        )

    async with db_session() as s:
        tt = await media.trang_thai(s, kq.upload_id)
    assert tt is not None and tt.state == "failed"


@pytest.mark.asyncio
async def test_lease_chan_giao_cung_mot_viec_lan_hai(wired) -> None:
    """Hai lượt nhặt việc NỐI TIẾP nhau: `lease_until` là thứ chặn, không phải khoá hàng."""
    async with transaction() as db:
        await media.xin_tai_anh(db, du_lieu=JPEG, ten_tep="g.jpg", created_by=ADMIN)

    async with transaction() as db:
        lan1 = await media.nhan_viec(db)
    async with transaction() as db:
        lan2 = await media.nhan_viec(db)

    assert len(lan1) == 1
    assert lan2 == [], "hàng đang có lease không được giao lần hai"


@pytest.mark.asyncio
async def test_giao_dich_dang_mo_KHONG_lam_treo_tien_trinh_kia(wired) -> None:
    """Đây mới là thứ `FOR UPDATE SKIP LOCKED` mua được, và bài trên KHÔNG đo được nó.

    Hai lượt nối tiếp thì `lease_until` đã đủ chặn — bỏ `SKIP LOCKED` đi bài đó vẫn xanh.
    Điều chỉ `SKIP LOCKED` làm được là: khi tiến trình A còn đang GIỮ khoá hàng trong một
    giao dịch chưa đóng, tiến trình B **không phải xếp hàng chờ**.

    Không có nó, hai tiến trình bot chạy chồng nhau lúc deploy sẽ nối đuôi: job của B treo
    tới khi A commit. Bài này đo bằng một hạn giờ — hết giờ nghĩa là đã treo.
    """
    import asyncio

    from televip.db.engine import transaction as tx

    async with transaction() as db:
        await media.xin_tai_anh(db, du_lieu=JPEG, ten_tep="h1.jpg", created_by=ADMIN)
        await media.xin_tai_anh(db, du_lieu=PNG, ten_tep="h2.jpg", created_by=ADMIN)

    ket_qua_b: list[int] = []

    async def tien_trinh_a() -> None:
        """Giữ khoá hàng suốt 2 giây rồi mới commit."""
        async with tx() as db:
            await media.nhan_viec(db, limit=1)
            await asyncio.sleep(2.0)

    async def tien_trinh_b() -> None:
        await asyncio.sleep(0.3)  # để A kịp lấy khoá
        async with tx() as db:
            # 1,2 giây: dư cho một truy vấn, và vẫn kết thúc TRƯỚC khi A nhả khoá ở giây 2.
            viec = await asyncio.wait_for(media.nhan_viec(db, limit=1), timeout=1.2)
            ket_qua_b.extend(v.upload_id for v in viec)

    await asyncio.gather(tien_trinh_a(), tien_trinh_b())

    assert len(ket_qua_b) == 1, "B phải nhặt được hàng CÒN LẠI, không phải chờ A"


# ── file_id_cua: đường DUY NHẤT lấy photo cho payload ────────────────


@pytest.mark.asyncio
async def test_file_id_cua_chi_tra_khi_anh_THAT_SU_san_sang(wired) -> None:
    """Đây là hàng rào ở tầng DỮ LIỆU, không phải một cái nút bị vô hiệu bằng JavaScript.

    `media_assets` chỉ có hàng khi `file_id` tồn tại thật, nên không có đường nào dựng được
    một payload mang `photo` rỗng.
    """
    async with db_session() as s:
        assert await media.file_id_cua(s, "sha256:khong-co-that") is None

    async with transaction() as db:
        kq = await media.xin_tai_anh(db, du_lieu=PNG, ten_tep="h.png", created_by=ADMIN)
    async with db_session() as s:
        assert await media.file_id_cua(s, f"sha256:{kq.sha256}") is None, (
            "ảnh còn trong phòng chờ thì chưa có file_id nào"
        )

    async with transaction() as db:
        key = await media.danh_dau_xong(
            db, upload_id=kq.upload_id, sha256=kq.sha256, file_id="xong", width=1, height=1
        )
    async with db_session() as s:
        assert await media.file_id_cua(s, key) == "xong"
