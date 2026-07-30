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
