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
async def test_nguoi_bi_don_ma_bam_lai_thi_nhan_loi_ro_rang(seeded, session_factory):
    """Người có grant nhưng mã đã bị thu hồi phải nhận `AlreadyClaimed`, không phải lỗi lạ.

    `AlreadyClaimed` là thứ mọi handler đã có nhánh xử lý; một `IntegrityError` thì không.
    """
    db = seeded
    await make_user(db, 9200)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=5)

    await _reserve(session_factory, user_id=9200, key="tanthu:9200")

    async with session_factory() as s, s.begin():
        await s.execute(text("UPDATE codes SET reserved_until = now() - interval '1 minute'"))
    async with session_factory() as s, s.begin():
        await ci.reap_reservations(s)

    with pytest.raises(AlreadyClaimed):
        await _reserve(session_factory, user_id=9200, key="tanthu:9200")
