"""Job dọn kho không được tự huỷ kho.

Bộ soát dựng lại được kịch bản này trên database thật: kho 50 mã sạch, một lần gửi
thất bại rồi một lần dọn — và **5/5 người kế tiếp không ai nhận được mã**.

Nguyên nhân: `reap_reservations()` trả mã về `available` nhưng để `code_grants.code_id`
vẫn trỏ tới nó. Bảng có `uq_grants_code UNIQUE (code_id)`, còn `reserve()` chọn theo
`ORDER BY code_id`, nên mã vừa thu hồi (id nhỏ nhất) được chọn lại mãi rồi nổ
`UniqueViolation` — một lỗi không handler nào bắt.

Điểm đáng sợ của lỗi này: nó chỉ xuất hiện SAU khi có người lên lịch chạy job dọn, mà
job đó lại là thứ được thêm vào để bảo vệ kho.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text

from televip.core.errors import AlreadyClaimed
from televip.services import code_issuance as ci
from tests.conftest import add_codes, make_user


async def _reserve(factory, *, user_id: int, key: str):
    async with factory() as s, s.begin():
        return await ci.reserve(
            s,
            user_id=user_id,
            grant_key=key,
            grant_type="tanthu",
            code_type="tanthu",
            value_vnd=10_000,
        )


@pytest.mark.asyncio
async def test_sau_khi_don_kho_nguoi_ke_tiep_van_nhan_duoc_ma(seeded, session_factory):
    """Kịch bản đúng như bộ soát dựng lại."""
    db = seeded
    for i in range(6):
        await make_user(db, 9100 + i)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=50)

    # Người đầu tiên giữ chỗ một mã rồi "gửi thất bại" (không gọi mark_delivered).
    first = await _reserve(session_factory, user_id=9100, key="tanthu:9100")

    async with session_factory() as s, s.begin():
        await s.execute(text("UPDATE codes SET reserved_until = now() - interval '1 minute'"))

    async with session_factory() as s, s.begin():
        reclaimed = await ci.reap_reservations(s)
    assert reclaimed == 1

    # Mã đã về kho VÀ grant đã được gỡ liên kết — đây là nửa thứ hai từng bị thiếu.
    async with session_factory() as s:
        row = (
            await s.execute(
                text("""
                SELECT c.status, g.code_id
                  FROM codes c
                  LEFT JOIN code_grants g ON g.grant_id = :gid
                 WHERE c.code_id = :cid
                """),
                {"cid": first.code_id, "gid": first.grant_id},
            )
        ).one()
    assert row.status == "available"
    assert row.code_id is None, "grant vẫn giữ code_id — mã này sẽ nổ UniqueViolation ở lượt sau"

    # Năm người kế tiếp phải nhận được mã bình thường. Trước khi sửa, cả năm đều
    # ăn IntegrityError.
    for i in range(1, 6):
        grant = await _reserve(session_factory, user_id=9100 + i, key=f"tanthu:{9100 + i}")
        assert grant.code_value, f"người thứ {i} không nhận được mã"

    async with session_factory() as s:
        n = (
            await s.execute(text("SELECT count(*) FROM code_grants WHERE code_id IS NOT NULL"))
        ).scalar_one()
    assert n == 5


@pytest.mark.asyncio
async def test_nguoi_bi_don_ma_bam_lai_thi_NHAN_DUOC_MA(seeded, session_factory):
    """Người có grant nhưng mã đã bị thu hồi, bấm lại thì **nhận được mã**.

    ⚠️ Bài kiểm này từng khẳng định điều NGƯỢC LẠI: nó đòi `reserve()` ném `AlreadyClaimed`,
    với lý do "`AlreadyClaimed` là thứ mọi handler đã có nhánh xử lý, `IntegrityError` thì
    không". Lý do ấy đúng về mặt kiểu ngoại lệ và **sai về mặt hậu quả**: không có đường nào
    trong toàn hệ thống gắn lại mã cho một grant mồ côi, nên người dùng bị khoá VĨNH VIỄN
    khỏi phần thưởng của chính mình — kho đầy mã mà bấm bao nhiêu lần cũng chỉ nhận một câu
    "code đang hết", và `/resend_tanthu` không gỡ được vì nó chỉ đọc grant ĐÃ có mã.

    Đường tới đây là đường thường gặp chứ không phải hiếm: gửi thất bại (người dùng chưa mở
    chat riêng) ⇒ không `mark_delivered` ⇒ job dọn kho chạy mỗi phút trả mã về kho và NULL
    `code_grants.code_id`. Lỗi có ở cả bốn luồng phát: tân thủ, mốc mời bạn, đập hộp, đổi
    điểm.
    """
    db = seeded
    await make_user(db, 9200)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=5)

    dau = await _reserve(session_factory, user_id=9200, key="tanthu:9200")

    async with session_factory() as s, s.begin():
        await s.execute(text("UPDATE codes SET reserved_until = now() - interval '1 minute'"))
    async with session_factory() as s, s.begin():
        await ci.reap_reservations(s)

    lai = await _reserve(session_factory, user_id=9200, key="tanthu:9200")

    assert lai.code_value, "người dùng vẫn bị khoá khỏi phần thưởng của chính mình"
    assert lai.grant_id == dau.grant_id, "phải gắn lại vào ĐÚNG grant cũ, không tạo grant thứ hai"

    # Và đúng MỘT grant, đúng MỘT mã đang giữ chỗ — không có đường nào phát trùng.
    async with session_factory() as s:
        assert (
            await s.execute(text("SELECT count(*) FROM code_grants WHERE user_id = 9200"))
        ).scalar_one() == 1
        assert (
            await s.execute(text("SELECT count(*) FROM codes WHERE status = 'reserved'"))
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_grant_da_bi_thu_hoi_thi_khong_tu_gan_lai(seeded, session_factory):
    """`revoked` là dấu vết một lần can thiệp bằng tay — không được tự sửa.

    Đây là ranh giới của việc gắn lại ở bài trên: gắn lại một grant đã bị thu hồi chính là
    ghi đè quyết định của con người trên một đường tiêu tiền.
    """
    db = seeded
    await make_user(db, 9300)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=5)

    await _reserve(session_factory, user_id=9300, key="tanthu:9300")
    async with session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE code_grants SET code_id = NULL, state = 'revoked' WHERE user_id = 9300")
        )

    with pytest.raises(AlreadyClaimed):
        await _reserve(session_factory, user_id=9300, key="tanthu:9300")


# ── Hai lỗ hổng mà chính bản vá "gắn lại grant mồ côi" đã mở ra ─────
#
# Vòng soát thứ hai dựng lại được cả hai trên database thật. Cả hai chỉ tồn tại SAU khi
# `reserve()` biết gắn lại — trước đó nhánh này ném `AlreadyClaimed` nên không đi tới đâu.


async def _reserve_gia(factory, *, user_id: int, key: str, value_vnd: int):
    async with factory() as s, s.begin():
        return await ci.reserve(
            s,
            user_id=user_id,
            grant_key=key,
            grant_type="points_redeem",
            code_type="diemdanh",
            value_vnd=value_vnd,
        )


async def _mo_coi(factory) -> None:
    """Dựng đúng trạng thái mồ côi: gửi hỏng ⇒ không mark_delivered ⇒ job dọn kho chạy."""
    async with factory() as s, s.begin():
        await s.execute(text("UPDATE codes SET reserved_until = now() - interval '1 minute'"))
    async with factory() as s, s.begin():
        await ci.reap_reservations(s)


@pytest.mark.asyncio
async def test_gan_lai_KHONG_duoc_doi_menh_gia_da_vao_so(seeded, session_factory):
    """Mã trao tay và số tiền vào sổ phải là CÙNG MỘT con số.

    Đường tới đây không cần admin làm gì: khoá đổi điểm là `redeem:{user_id}:{ngày}`, nó
    **không mang bậc mệnh giá**. Bấm bậc 10K → gửi hỏng → job dọn → cùng ngày bấm bậc
    50K: `redeem()` thấy đã trừ điểm rồi nên không trừ thêm, còn `reserve()` thì được gọi
    với 50.000.

    Không ghim mệnh giá thì người dùng cầm mã 50.000đ trong khi sổ cái, bút toán và bộ đếm
    đều ghi 10.000đ — nhà phát trả 40.000đ mà không bao giờ ghi nhận. Chiều ngược lại thì
    người dùng bị lấy mất 40.000đ.
    """
    db = seeded
    await make_user(db, 9400)
    await add_codes(db, code_type="diemdanh", value_vnd=10_000, count=3, prefix="D10")
    await add_codes(db, code_type="diemdanh", value_vnd=50_000, count=3, prefix="D50")

    dau = await _reserve_gia(session_factory, user_id=9400, key="redeem:9400:x", value_vnd=10_000)
    assert dau.value_vnd == 10_000
    await _mo_coi(session_factory)

    # Lượt sau gọi với mệnh giá KHÁC — đúng như bậc thứ hai trong cùng ngày.
    lai = await _reserve_gia(session_factory, user_id=9400, key="redeem:9400:x", value_vnd=50_000)

    assert lai.value_vnd == 10_000, "mệnh giá trả về đã trôi khỏi con số đã vào sổ"
    async with session_factory() as s:
        row = (
            await s.execute(
                text("""
                SELECT g.value_vnd AS so_ghi, c.value_vnd AS ma_that, c.code_value
                  FROM code_grants g JOIN codes c USING (code_id)
                 WHERE g.user_id = 9400
                """)
            )
        ).one()
    assert row.so_ghi == row.ma_that, (
        f"sổ ghi {row.so_ghi:,}đ nhưng mã trao tay là {row.ma_that:,}đ ({row.code_value})"
    )
    assert row.code_value.startswith("D10"), "phát mã của mệnh giá chưa được trả tiền"


@pytest.mark.asyncio
async def test_gan_lai_bao_dung_rang_grant_da_ton_tai(seeded, session_factory):
    """`was_existing=False` cho một grant đã tồn tại làm chết hàng rào của `checkin.redeem`.

    Hàng rào đó (`if grant.was_existing and grant.value_vnd != value_vnd`) tồn tại đúng
    cho tình huống này, và trả `False` khiến nó im lặng ở lượt duy nhất nó có việc.
    """
    db = seeded
    await make_user(db, 9410)
    await add_codes(db, code_type="diemdanh", value_vnd=10_000, count=3, prefix="D10")

    await _reserve_gia(session_factory, user_id=9410, key="redeem:9410:x", value_vnd=10_000)
    await _mo_coi(session_factory)
    lai = await _reserve_gia(session_factory, user_id=9410, key="redeem:9410:x", value_vnd=10_000)

    assert lai.was_existing is True


@pytest.mark.asyncio
async def test_hai_luot_dong_thoi_tren_grant_mo_coi_chi_ra_MOT_ma(seeded, session_factory):
    """Hai người cầm chung một mã quà — đúng loại lỗi module này sinh ra để chặn.

    `ON CONFLICT DO NOTHING` chỉ chặn được người đến sau khi dòng kia CHƯA commit. Với một
    grant đã commit từ trước nó trả rỗng mà **không giữ khoá nào**, nên hai lời gọi song
    song cùng thấy `code_id IS NULL`, cùng giành một mã KHÁC NHAU qua `SKIP LOCKED`, và
    người sau ghi đè `code_id`. Mã của người trước mồ côi → job dọn trả về kho → phát cho
    NGƯỜI KHÁC.

    Đường tới đây có thật: `handle_check_groups` (nút thật sự gọi `reserve()`) không có
    cooldown, và `concurrent_updates(True)` biến một cú bấm đúp thành hai task song song.
    """
    db = seeded
    await make_user(db, 9420)
    await add_codes(db, code_type="diemdanh", value_vnd=10_000, count=10, prefix="D10")

    await _reserve_gia(session_factory, user_id=9420, key="redeem:9420:x", value_vnd=10_000)
    await _mo_coi(session_factory)

    # ── Đo BẤT BIẾN, không đo cuộc đua ────────────────────────────────────────────
    # Hai cách "tự nhiên" hơn đều KHÔNG phân biệt được, đã thử cả hai:
    #
    # 1. `asyncio.gather` hai lượt rồi mong chúng chồng nhau — xanh cả khi khoá bị gỡ.
    #    Đó là bài kiểm xác suất, và ở đây xác suất không đứng về phía nó.
    # 2. Một giao dịch khác giữ `FOR UPDATE` rồi đòi `reserve()` phải treo — cũng xanh khi
    #    khoá bị gỡ, vì `INSERT … ON CONFLICT` tự chờ trên dòng đang bị khoá. Nó đo cơ chế
    #    của Postgres chứ không đo dòng code của mình.
    #
    # Nên đo thẳng thứ cần đo: **`reserve()` có PHÁT RA câu khoá dòng grant hay không.**
    # Trắng hộp, nhưng dứt khoát — gỡ dòng khoá đi là bài này đỏ ngay.
    cau_lenh: list[str] = []

    def _ghi_lai(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        cau_lenh.append(" ".join(statement.split()).upper())

    # Lấy engine từ CHÍNH factory của bài kiểm — `db.engine` toàn cục không được khởi tạo
    # trong file này (fixture `engine` của conftest dựng engine riêng cho mỗi test).
    sync_engine = session_factory.kw["bind"].sync_engine
    sa_event.listen(sync_engine, "before_cursor_execute", _ghi_lai)
    try:
        lai = await _reserve_gia(
            session_factory, user_id=9420, key="redeem:9420:x", value_vnd=10_000
        )
    finally:
        sa_event.remove(sync_engine, "before_cursor_execute", _ghi_lai)

    assert lai.code_value
    khoa = [c for c in cau_lenh if "CODE_GRANTS" in c and "FOR UPDATE" in c]
    assert khoa, (
        "reserve() không khoá dòng grant trước khi đọc — hai lượt song song sẽ cùng thấy "
        "code_id IS NULL và cùng giành một mã khác nhau"
    )

    async with session_factory() as s:
        giu_cho = (
            await s.execute(text("SELECT count(*) FROM codes WHERE status = 'reserved'"))
        ).scalar_one()
        so_grant = (
            await s.execute(text("SELECT count(*) FROM code_grants WHERE user_id = 9420"))
        ).scalar_one()
    assert giu_cho == 1, "có mã bị giữ chỗ mà không grant nào trỏ tới — nó sẽ bị phát lại"
    assert so_grant == 1
