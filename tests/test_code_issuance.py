"""Test cấp code — bộ test quan trọng nhất của dự án.

Bốn tình huống dưới đây tái hiện đúng bốn cách hệ cũ làm mất tiền. Mỗi test là một câu
hỏi có thể trả lời bằng số, không phải bằng niềm tin:

- R1 — cùng một người bấm nút 50 lần đồng thời: nhận đúng MỘT mã?
- R2 — 200 người tranh 200 mã cùng lúc: có ai nhận trùng mã của người khác không?
- R3 — kho cạn giữa chừng: hệ thống báo hết, hay phát ra mã không tồn tại?
- R4 — gửi tin thất bại: mã bị đốt, hay quay về kho?
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from televip.core.errors import OutOfStock
from televip.services import code_issuance as ci
from tests.conftest import add_codes, make_user


async def _reserve_in_own_transaction(
    factory: async_sessionmaker, *, user_id: int, grant_key: str, **kw
):
    """Mỗi lần gọi dùng một kết nối riêng — đúng như hai worker thật chạy song song."""
    async with factory() as s:
        async with s.begin():
            return await ci.reserve(s, user_id=user_id, grant_key=grant_key, **kw)


# ── R1 — Idempotency: bấm nút nhiều lần ─────────────────────────────


@pytest.mark.asyncio
async def test_r1_bam_50_lan_dong_thoi_chi_nhan_mot_ma(seeded, session_factory):
    """Đây chính là lỗi đã cho 226 người ôm 544 code thừa ở hệ cũ."""
    db = seeded
    uid = await make_user(db, 1001)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=50)

    results = await asyncio.gather(
        *[
            _reserve_in_own_transaction(
                session_factory,
                user_id=uid,
                grant_key=f"tanthu:{uid}",
                grant_type="tanthu",
                code_type="tanthu",
                value_vnd=10_000,
            )
            for _ in range(50)
        ],
        return_exceptions=True,
    )

    ok = [r for r in results if isinstance(r, ci.Grant)]
    assert ok, f"không lần nào thành công: {results[:3]}"

    # Tất cả phải trỏ về CÙNG một grant và CÙNG một mã.
    assert len({r.grant_id for r in ok}) == 1, "sinh ra nhiều grant cho cùng grant_key"
    assert len({r.code_id for r in ok}) == 1, "cùng một người nhận nhiều mã khác nhau"

    async with session_factory() as s:
        n_grants = (
            await s.execute(text("SELECT count(*) FROM code_grants WHERE user_id = :u"), {"u": uid})
        ).scalar_one()
        n_reserved = (
            await s.execute(text("SELECT count(*) FROM codes WHERE status = 'reserved'"))
        ).scalar_one()

    assert n_grants == 1, f"phải có đúng 1 grant, thực tế {n_grants}"
    assert n_reserved == 1, f"phải giữ chỗ đúng 1 mã, thực tế {n_reserved}"


# ── R2 — Không phát trùng mã giữa những người khác nhau ─────────────


@pytest.mark.asyncio
async def test_r2_200_nguoi_tranh_200_ma_khong_ai_trung(seeded, session_factory):
    """Câu hỏi sống còn: hai người có bao giờ nhận cùng một mã không?"""
    db = seeded
    n = 200
    for i in range(n):
        await make_user(db, 2000 + i)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=n)

    results = await asyncio.gather(
        *[
            _reserve_in_own_transaction(
                session_factory,
                user_id=2000 + i,
                grant_key=f"tanthu:{2000 + i}",
                grant_type="tanthu",
                code_type="tanthu",
                value_vnd=10_000,
            )
            for i in range(n)
        ],
        return_exceptions=True,
    )

    grants = [r for r in results if isinstance(r, ci.Grant)]
    errors = [r for r in results if isinstance(r, Exception)]

    assert not errors, f"có {len(errors)} lỗi ngoài dự kiến: {errors[:2]}"
    assert len(grants) == n

    code_ids = [g.code_id for g in grants]
    assert len(set(code_ids)) == n, (
        f"PHÁT TRÙNG MÃ: {n} người nhưng chỉ có {len(set(code_ids))} mã khác nhau"
    )

    code_values = [g.code_value for g in grants]
    assert len(set(code_values)) == n, "hai người nhận cùng một chuỗi mã"


# ── R3 — Kho cạn ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_r3_kho_can_thi_bao_het_khong_phat_ma_ma(seeded, session_factory):
    """Có 10 mã, 30 người tranh. Đúng 10 người được, 20 người nhận lỗi rõ ràng."""
    db = seeded
    for i in range(30):
        await make_user(db, 3000 + i)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=10)

    results = await asyncio.gather(
        *[
            _reserve_in_own_transaction(
                session_factory,
                user_id=3000 + i,
                grant_key=f"tanthu:{3000 + i}",
                grant_type="tanthu",
                code_type="tanthu",
                value_vnd=10_000,
            )
            for i in range(30)
        ],
        return_exceptions=True,
    )

    grants = [r for r in results if isinstance(r, ci.Grant)]
    out_of_stock = [r for r in results if isinstance(r, OutOfStock)]

    assert len(grants) == 10, f"phải cấp đúng 10, thực tế {len(grants)}"
    assert len(out_of_stock) == 20, f"phải có 20 lỗi hết kho, thực tế {len(out_of_stock)}"
    assert len({g.code_id for g in grants}) == 10

    async with session_factory() as s:
        # Người bị từ chối KHÔNG được để lại grant rác — transaction phải cuộn sạch,
        # nếu không họ sẽ vĩnh viễn không nhận được code khi admin nạp thêm mã.
        n_grants = (await s.execute(text("SELECT count(*) FROM code_grants"))).scalar_one()
    assert n_grants == 10, f"còn {n_grants - 10} grant rác của người bị từ chối"


# ── R4 — Gửi thất bại thì mã quay về kho, không bị đốt ──────────────


@pytest.mark.asyncio
async def test_r4_gui_that_bai_thi_ma_quay_ve_kho(seeded, session_factory):
    """Lỗi này ở hệ cũ đốt hàng nghìn mã mỗi đợt lớn: commit trước, gửi sau, gửi lỗi là mất."""
    db = seeded
    uid = await make_user(db, 4001)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=1)

    grant = await _reserve_in_own_transaction(
        session_factory,
        user_id=uid,
        grant_key=f"tanthu:{uid}",
        grant_type="tanthu",
        code_type="tanthu",
        value_vnd=10_000,
    )

    async with session_factory() as s:
        status = (
            await s.execute(
                text("SELECT status FROM codes WHERE code_id = :c"), {"c": grant.code_id}
            )
        ).scalar_one()
    assert status == "reserved", "sau reserve mã phải ở trạng thái giữ chỗ, chưa phải đã phát"

    # Giả lập gửi tin thất bại: không gọi mark_delivered, và ép hết hạn giữ chỗ.
    async with session_factory() as s:
        async with s.begin():
            await s.execute(text("UPDATE codes SET reserved_until = now() - interval '1 minute'"))

    async with session_factory() as s:
        async with s.begin():
            reclaimed = await ci.reap_reservations(s)
    assert reclaimed == 1

    async with session_factory() as s:
        status = (
            await s.execute(
                text("SELECT status FROM codes WHERE code_id = :c"), {"c": grant.code_id}
            )
        ).scalar_one()
        n_ledger = (await s.execute(text("SELECT count(*) FROM code_ledger"))).scalar_one()

    assert status == "available", "mã phải quay về kho, KHÔNG được đốt"
    assert n_ledger == 0, "chưa gửi tới tay ai thì tuyệt đối không được ghi sổ cái tiền"


# ── Pha 2: giao thành công thì ghi sổ đầy đủ ────────────────────────


@pytest.mark.asyncio
async def test_giao_thanh_cong_ghi_du_so_cai_va_bo_dem(seeded, session_factory):
    db = seeded
    uid = await make_user(db, 5001)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=1)

    grant = await _reserve_in_own_transaction(
        session_factory,
        user_id=uid,
        grant_key=f"tanthu:{uid}",
        grant_type="tanthu",
        code_type="tanthu",
        value_vnd=10_000,
    )

    async with session_factory() as s:
        async with s.begin():
            await ci.mark_delivered(s, grant_id=grant.grant_id)

    async with session_factory() as s:
        row = (
            await s.execute(
                text("""
                SELECT c.status, g.state, u.total_codes_received, u.total_value_received,
                       (SELECT count(*) FROM code_ledger) AS n_ledger,
                       (SELECT sum(value_vnd * direction) FROM code_ledger) AS ledger_sum
                  FROM codes c
                  JOIN code_grants g ON g.code_id = c.code_id
                  JOIN users u ON u.user_id = g.user_id
                 WHERE g.grant_id = :g
                """),
                {"g": grant.grant_id},
            )
        ).one()

    assert row.status == "issued"
    assert row.state == "delivered"
    assert row.total_codes_received == 1
    assert row.total_value_received == 10_000
    assert row.n_ledger == 1
    assert row.ledger_sum == 10_000, "sổ cái phải khớp đúng số tiền đã phát"


@pytest.mark.asyncio
async def test_mark_delivered_goi_hai_lan_khong_cong_doi(seeded, session_factory):
    """Worker có thể xử lý lại cùng một việc sau khi restart — không được cộng tiền hai lần."""
    db = seeded
    uid = await make_user(db, 6001)
    await add_codes(db, code_type="tanthu", value_vnd=10_000, count=1)

    grant = await _reserve_in_own_transaction(
        session_factory,
        user_id=uid,
        grant_key=f"tanthu:{uid}",
        grant_type="tanthu",
        code_type="tanthu",
        value_vnd=10_000,
    )

    for _ in range(3):
        async with session_factory() as s:
            async with s.begin():
                await ci.mark_delivered(s, grant_id=grant.grant_id)

    async with session_factory() as s:
        row = (
            await s.execute(
                text("""
                SELECT u.total_codes_received, u.total_value_received,
                       (SELECT count(*) FROM code_ledger) AS n_ledger
                  FROM users u WHERE u.user_id = :u
                """),
                {"u": uid},
            )
        ).one()

    assert row.total_codes_received == 1, "gọi 3 lần mà cộng nhiều hơn 1 là lỗi in tiền"
    assert row.total_value_received == 10_000
    assert row.n_ledger == 1, "sổ cái không được có bút toán trùng"
