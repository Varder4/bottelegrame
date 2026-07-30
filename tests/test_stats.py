"""Số liệu tổng quan, số liệu cá nhân và bảng xếp hạng — chạy trên PostgreSQL thật.

Mệnh đề trọng tâm: **bảng xếp hạng "hôm nay" phải là bảng của HÔM NAY.**

Bot cũ có một nút tên là `👑 BXH hôm nay` mà cả ba truy vấn đằng sau đều không có một điều
kiện thời gian nào (`db_manager.py:676-684`) — bảng toàn thời gian đội lốt bảng ngày. Ở
đây ranh giới ngày là **nửa đêm giờ Việt Nam**, quy về một khoảng UTC bằng `vn_day_bounds`,
nên hai bản ghi cách nhau hai giây qua mốc đó phải rơi vào hai ngày khác nhau — kể cả khi
máy chủ chạy UTC.

⚠️ Cùng luật với `test_tanthu.py`: KHÔNG dùng fixture `db`/`seeded` của `conftest`. Dịch vụ
đọc ghi qua engine **toàn cục**, nên dựng dữ liệu bằng một engine thứ hai biến thứ tự commit
giữa hai bên thành một cuộc đua.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from sqlalchemy import text

from televip.core.clock import UTC, VN_TZ, vn_day_bounds
from televip.db.engine import session as db_session
from televip.db.engine import transaction
from televip.domain import texts
from televip.services import stats
from tests.conftest import TEST_DATABASE_URL, _truncate_all

#: Một ngày nghiệp vụ **cố định trong quá khứ**, không phải "hôm nay". Hai lý do: ranh
#: giới ngày là thứ đang được kiểm nên nó phải là một hằng đọc được, và dữ liệu của bài
#: kiểm không được lẫn với bản ghi mà bài kiểm khác vừa tạo bằng `now()`.
DAY = date(2026, 7, 20)
DAY_START, DAY_END = vn_day_bounds(DAY)
#: Hai mốc cách nhau đúng hai giây, nằm hai bên nửa đêm giờ VN.
LAST_SECOND_YESTERDAY = DAY_START - timedelta(seconds=1)
FIRST_SECOND_TODAY = DAY_START + timedelta(seconds=1)

_GRANT_TYPES_SQL = """
INSERT INTO grant_types (code, label_vi, once_per_life) VALUES
    ('tanthu', 'Code tan thu', true),
    ('referral_milestone', 'Moc moi ban be', false),
    ('event_box', 'Dap hop', false),
    ('points_redeem', 'Doi diem', false),
    ('share_event', 'Event chia se', true),
    ('admin_manual', 'Admin trao tay', false)
ON CONFLICT DO NOTHING
"""


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine
    from televip.services import settings_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    settings_service.invalidate()

    async with db_session() as s:
        await _truncate_all(s)
        await s.execute(text(_GRANT_TYPES_SQL))
        await s.commit()

    try:
        yield
    finally:
        await db_engine.dispose_engine()
        settings_service.invalidate()


# ── Dựng dữ liệu ────────────────────────────────────────────────────


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def add_user(user_id: int, username: str | None = None) -> int:
    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:uid, :un) ON CONFLICT DO NOTHING",
        {"uid": user_id, "un": username},
    )
    return user_id


async def add_referral(referrer: int, referee: int, *, created_at: datetime) -> None:
    await add_user(referee)
    await run_sql(
        """
        INSERT INTO referrals (referee_id, referrer_id, qualified_at, created_at)
             VALUES (:referee, :referrer, :at, :at)
        """,
        {"referee": referee, "referrer": referrer, "at": created_at},
    )


async def add_grant(user_id: int, value_vnd: int, *, created_at: datetime, state: str) -> None:
    await run_sql(
        """
        INSERT INTO code_grants (grant_key, user_id, grant_type, value_vnd, state,
                                 idempotency_key, created_at)
             VALUES (:key, :uid, 'admin_manual', :val, :state, :key, :at)
        """,
        {
            "key": f"test-{user_id}-{created_at.isoformat()}-{value_vnd}",
            "uid": user_id,
            "val": value_vnd,
            "state": state,
            "at": created_at,
        },
    )


async def add_checkin(user_id: int, day: date, streak: int) -> None:
    await run_sql(
        """
        INSERT INTO checkins (user_id, business_date, points_delta, streak_after)
             VALUES (:uid, :day, 2000, :streak)
        """,
        {"uid": user_id, "day": day, "streak": streak},
    )


async def set_user_stats(
    user_id: int,
    *,
    refs_total: int = 0,
    refs_qualified: int = 0,
    value_total: int = 0,
    checkin_streak: int = 0,
) -> None:
    await run_sql(
        """
        INSERT INTO user_stats (user_id, refs_total, refs_qualified, value_total, checkin_streak)
             VALUES (:uid, :rt, :rq, :vt, :cs)
        ON CONFLICT (user_id) DO UPDATE
                SET refs_total = EXCLUDED.refs_total,
                    refs_qualified = EXCLUDED.refs_qualified,
                    value_total = EXCLUDED.value_total,
                    checkin_streak = EXCLUDED.checkin_streak
        """,
        {
            "uid": user_id,
            "rt": refs_total,
            "rq": refs_qualified,
            "vt": value_total,
            "cs": checkin_streak,
        },
    )


async def write_system_stats(*, total_users: int, age_seconds: int) -> None:
    """Dòng `system_stats` với tuổi đặt sẵn — để thử nhánh "quá cũ thì tính lại"."""
    await run_sql(
        """
        INSERT INTO system_stats (id, total_users, codes_available, codes_issued,
                                  total_referrals, total_value_vnd, updated_at)
             VALUES (1, :tu, 0, 0, 0, 0, now() - make_interval(secs => :age))
        ON CONFLICT (id) DO UPDATE
                SET total_users = EXCLUDED.total_users, updated_at = EXCLUDED.updated_at
        """,
        {"tu": total_users, "age": age_seconds},
    )


# ── vn_day_bounds ───────────────────────────────────────────────────


def test_vn_day_bounds_cat_dung_moc_nua_dem_gio_vn() -> None:
    start, end = vn_day_bounds(DAY)

    assert start.astimezone(VN_TZ).isoformat() == "2026-07-20T00:00:00+07:00"
    assert end.astimezone(VN_TZ).isoformat() == "2026-07-21T00:00:00+07:00"
    # Trả về theo UTC để cắm thẳng vào `WHERE created_at >= $1 AND < $2`.
    assert start.utcoffset() == timedelta(0)
    assert start.astimezone(UTC).isoformat() == "2026-07-19T17:00:00+00:00"
    assert end - start == timedelta(days=1)


def test_vn_day_bounds_khoang_nua_mo_khong_chong_lan() -> None:
    """Cuối ngày hôm trước phải bằng đúng đầu ngày hôm sau — không hụt, không chồng."""
    _, end_yesterday = vn_day_bounds(DAY - timedelta(days=1))
    start_today, _ = vn_day_bounds(DAY)
    assert end_yesterday == start_today


# ── Bảng xếp hạng: hôm nay ──────────────────────────────────────────


async def test_bxh_hom_nay_khong_lan_du_lieu_hom_qua(wired) -> None:
    hom_qua = await add_user(7_001, "homqua")
    hom_nay = await add_user(7_002, "homnay")

    # Người của hôm qua mời NHIỀU hơn, và bản ghi cuối chỉ cách nửa đêm một giây.
    for i, referee in enumerate((7_101, 7_102, 7_103)):
        await add_referral(hom_qua, referee, created_at=LAST_SECOND_YESTERDAY - timedelta(hours=i))
    await add_referral(hom_nay, 7_201, created_at=FIRST_SECOND_TODAY)

    async with db_session() as db:
        board = await stats.leaderboard(db, today=True, limit=3, day=DAY)

    assert board.top_referrers == (("@homnay", 1),)


async def test_bxh_hom_nay_top_nhan_code_chi_tinh_grant_da_giao_trong_ngay(wired) -> None:
    uid = await add_user(7_301, "nguoinhan")
    await add_grant(uid, 50_000, created_at=LAST_SECOND_YESTERDAY, state="delivered")
    await add_grant(uid, 10_000, created_at=FIRST_SECOND_TODAY, state="delivered")
    # `reserved` là mã đang giữ chỗ, chưa ai nhận được — không được tính là "đã nhận".
    await add_grant(uid, 88_000, created_at=FIRST_SECOND_TODAY, state="reserved")

    async with db_session() as db:
        board = await stats.leaderboard(db, today=True, limit=3, day=DAY)

    assert board.top_receivers == (("@nguoinhan", 10_000),)


async def test_bxh_hom_nay_top_diem_danh_theo_ngay_nghiep_vu(wired) -> None:
    cham = await add_user(7_401, "cham")
    nghi = await add_user(7_402, "nghi")
    await add_checkin(cham, DAY, streak=12)
    await add_checkin(nghi, DAY - timedelta(days=1), streak=99)

    async with db_session() as db:
        board = await stats.leaderboard(db, today=True, limit=3, day=DAY)

    assert board.top_streaks == (("@cham", 12),)


async def test_bxh_mac_dinh_lay_ngay_nghiep_vu_hien_tai(wired, monkeypatch) -> None:
    """Không truyền `day` thì chế độ "hôm nay" phải hỏi `clock.business_date()`.

    Bài kiểm dời "hôm nay" về một ngày cố định thay vì chèn dữ liệu vào ngày thật: nếu
    ngày nghiệp vụ bị lấy từ chỗ khác (giờ máy chủ, `date.today()`), truy vấn sẽ nhìn vào
    một khoảng khác và bảng trả về rỗng.
    """
    monkeypatch.setattr(stats, "business_date", lambda: DAY)
    uid = await add_user(7_801_000, "macdinh")
    await add_referral(uid, 7_802_000, created_at=FIRST_SECOND_TODAY)

    async with db_session() as db:
        board = await stats.leaderboard(db, today=True, limit=3)

    assert board.top_referrers == (("@macdinh", 1),)


async def test_bxh_khoi_rong_khong_lam_hong_hai_khoi_con_lai(wired) -> None:
    uid = await add_user(7_501, "chionmot")
    await add_checkin(uid, DAY, streak=3)

    async with db_session() as db:
        board = await stats.leaderboard(db, today=True, limit=3, day=DAY)

    assert board.top_streaks == (("@chionmot", 3),)
    assert board.top_referrers == ()
    assert board.top_receivers == ()
    # Khối rỗng in "Chưa có dữ liệu", hai khối kia vẫn hiện (§13.2.6).
    assert texts.referrer_lines(board.top_referrers) == texts.NO_DATA
    assert "🥇 @chionmot" in texts.streak_lines(board.top_streaks)


# ── Bảng xếp hạng: toàn thời gian ───────────────────────────────────


async def test_bxh_toan_thoi_gian_doc_bang_tong_hop_va_ton_trong_limit(wired) -> None:
    # Con số cố ý lớn: database test dùng chung, và một dòng `user_stats` của bài kiểm
    # khác không được chen vào top 3 của bài này.
    for i in range(4):
        uid = await add_user(7_600 + i, f"nguoi{i}")
        await set_user_stats(
            uid,
            refs_total=9_000_000 - i,
            value_total=1_000_000 * (i + 1),
            checkin_streak=i,
        )

    async with db_session() as db:
        board = await stats.leaderboard(db, today=False, limit=3)

    assert board.today is False
    assert board.top_referrers == (
        ("@nguoi0", 9_000_000),
        ("@nguoi1", 8_999_999),
        ("@nguoi2", 8_999_998),
    )
    assert board.top_receivers[0] == ("@nguoi3", 4_000_000)
    # `checkin_streak = 0` không phải một hạng — người chưa điểm danh lần nào không lên bảng.
    assert all(value > 0 for _, value in board.top_streaks)


async def test_bxh_ten_hien_thi_theo_thu_tu_username_fullname_an_danh(wired) -> None:
    khong_ten = await add_user(7_701)
    await run_sql(
        "UPDATE users SET full_name = 'Nguyen Van A' WHERE user_id = :uid", {"uid": khong_ten}
    )
    an_danh = await add_user(7_702)
    await set_user_stats(khong_ten, refs_total=9_000_001)
    await set_user_stats(an_danh, refs_total=9_000_000)

    async with db_session() as db:
        board = await stats.leaderboard(db, today=False, limit=3)

    assert board.top_referrers[:2] == (
        ("Nguyen Van A", 9_000_001),
        (texts.ANONYMOUS, 9_000_000),
    )


# ── system_stats ────────────────────────────────────────────────────


async def test_system_snapshot_tu_tinh_khi_bang_chua_co_dong_nao(wired) -> None:
    await add_user(7_801)
    await add_grant(7_801, 10_000, created_at=FIRST_SECOND_TODAY, state="delivered")

    async with transaction() as db:
        snapshot = await stats.system_snapshot(db)

    assert snapshot.total_users == await scalar("SELECT count(*) FROM users")
    assert snapshot.codes_issued == await scalar(
        "SELECT count(*) FROM code_grants WHERE state = 'delivered'"
    )
    assert snapshot.total_value_vnd >= 10_000
    # Đã ghi lại, lượt bấm sau chỉ đọc một dòng.
    assert await scalar("SELECT count(*) FROM system_stats") == 1


async def test_system_snapshot_con_moi_thi_doc_nguyen_van(wired) -> None:
    await add_user(7_901)
    await write_system_stats(total_users=999, age_seconds=5)

    async with transaction() as db:
        snapshot = await stats.system_snapshot(db)

    # Con số vô lý nhưng còn mới: đọc bảng tổng hợp, KHÔNG chạy lại năm câu tổng hợp.
    assert snapshot.total_users == 999


async def test_system_snapshot_qua_cu_thi_tinh_lai(wired) -> None:
    await add_user(8_001)
    await write_system_stats(total_users=999, age_seconds=stats.DEFAULT_MAX_AGE_SECONDS + 60)

    async with transaction() as db:
        snapshot = await stats.system_snapshot(db)

    that = await scalar("SELECT count(*) FROM users")
    assert snapshot.total_users == that != 999
    assert await scalar("SELECT total_users FROM system_stats WHERE id = 1") == that


async def test_refresh_system_stats_ghi_de_dong_duy_nhat(wired) -> None:
    await add_user(8_101)
    async with transaction() as db:
        first = await stats.refresh_system_stats(db)
    await add_user(8_102)
    async with transaction() as db:
        second = await stats.refresh_system_stats(db)

    assert second.total_users == first.total_users + 1
    assert await scalar("SELECT count(*) FROM system_stats") == 1


# ── user_stats ──────────────────────────────────────────────────────


async def test_user_snapshot_nguoi_moi_toanh_tra_ve_so_0_khong_nem_loi(wired) -> None:
    async with db_session() as db:
        me = await stats.user_snapshot(db, 8_201, day=DAY)

    assert me.refs_qualified == 0
    assert me.codes_received == 0
    assert me.rank_today is None
    assert me.rank_alltime is None


async def test_user_snapshot_co_dong_users_nhung_chua_co_user_stats(wired) -> None:
    uid = await add_user(8_301, "moivao")

    async with db_session() as db:
        me = await stats.user_snapshot(db, uid, day=DAY)

    assert me.refs_total == 0
    assert me.rank_alltime is None


async def test_user_snapshot_hang_toan_thoi_gian_dem_nguoi_dung_tren_minh(wired) -> None:
    nhat = await add_user(8_401, "nhat")
    nhi = await add_user(8_402, "nhi")
    await set_user_stats(nhat, refs_total=9_000_001, refs_qualified=9_000_001)
    await set_user_stats(nhi, refs_total=9_000_000, refs_qualified=9_000_000)

    async with db_session() as db:
        assert (await stats.user_snapshot(db, nhat, day=DAY)).rank_alltime == 1
        assert (await stats.user_snapshot(db, nhi, day=DAY)).rank_alltime == 2


async def test_user_snapshot_hang_hom_nay_chi_dem_du_lieu_trong_ngay(wired) -> None:
    hom_qua = await add_user(8_501, "homqua")
    hom_nay = await add_user(8_502, "homnay")
    for referee in (8_601, 8_602, 8_603):
        await add_referral(hom_qua, referee, created_at=LAST_SECOND_YESTERDAY)
    await add_referral(hom_nay, 8_701, created_at=FIRST_SECOND_TODAY)

    async with db_session() as db:
        # Người mời 3 lượt của hôm qua không có hạng HÔM NAY.
        assert (await stats.user_snapshot(db, hom_qua, day=DAY)).rank_today is None
        assert (await stats.user_snapshot(db, hom_nay, day=DAY)).rank_today == 1


async def test_user_snapshot_lay_so_da_nhan_tu_bang_users(wired) -> None:
    uid = await add_user(8_801, "danhan")
    await run_sql(
        """
        UPDATE users SET total_codes_received = 3, total_value_received = 30000
         WHERE user_id = :uid
        """,
        {"uid": uid},
    )

    async with db_session() as db:
        me = await stats.user_snapshot(db, uid, day=DAY)

    assert me.codes_received == 3
    assert me.value_received == 30_000
