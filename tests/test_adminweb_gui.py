"""Đường TẠO và GỬI đợt bắn tin từ panel web.

Đây là đường tốn kém nhất của cả hệ thống: một đợt gửi tới 19.151 người và không rút lại
được. Bốn mệnh đề được đo ở đây:

- **Web ghi ý định, bot thực thi.** Panel không bao giờ gọi vòng bơm — có một bài đọc mã
  khẳng định, và một bài đầu-tới-cuối chứng minh mắt xích web → bot không đứt im lặng.
- **Bấm gửi hai lần chỉ bắt đầu một lần**, và điều đó do một câu `UPDATE` có điều kiện
  bảo đảm, không do một cái nút bị vô hiệu bằng JavaScript.
- **Bốn điều kiện của `start()` chặn bốn kịch bản khác nhau**: sai loại đợt, không phải
  người tạo, số đích lệch, nháp quá hạn.
- **Hàng rào nằm trong service, không trong route.** Mọi bài dưới đây gửi POST thẳng như
  một người bỏ qua giao diện.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import make_user
from tests.test_adminweb import _dang_nhap, _dung_admin, run_sql, scalar
from tests.test_adminweb_ghi import _goc

_QUYEN = ("/broadcast", "/broadcast_status", "/broadcast_cancel")

OWNER = 990_001
NGUOI_KHAC = 994_001


async def _vao(client: httpx.AsyncClient, *, nguong: int = 1) -> str:
    from televip.services import admin as admin_service
    from televip.services import settings_service

    await _dung_admin()
    for lenh in _QUYEN:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(OWNER)
    await run_sql(
        """
        INSERT INTO settings (key, value, value_type, label_vi, min_value, max_value)
             VALUES ('broadcast.min_audience', CAST(:v AS jsonb), 'int',
                     'Ngưỡng số người nhận tối thiểu', 1, 100000)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        {"v": str(nguong)},
    )
    settings_service.invalidate()
    await _dang_nhap(client)
    return await scalar(
        "SELECT csrf_token FROM admin_sessions WHERE revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


async def _nguoi_nhan(n: int) -> None:
    """`n` người đủ điều kiện nhận tin: đã /start, chưa chặn, chưa tắt thông báo."""
    from televip.db.engine import session as db_session

    async with db_session() as s:
        for i in range(n):
            await make_user(s, 995_000 + i)
    await run_sql(
        "UPDATE users SET started_bot_at = now(), last_active = now(), "
        "blocked_at = NULL, notify_optout = false"
    )


async def _tao_nhap(client: httpx.AsyncClient, csrf: str, *, body: str = "xin chao") -> int:
    r = await client.post(
        "/bantin/moi", data={"noi_dung": body, "tep": "all", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303, r.text[:400]
    return int(r.headers["location"].split("/")[2])


# ── Đường đi đầy đủ ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tu_web_bam_gui_thi_tin_that_su_bay(app_client: httpx.AsyncClient):
    """Bài đầu-tới-cuối, không giả lập gì — mắt xích web → bot.

    Web KHÔNG bơm. Sau khi bấm Gửi, không một dòng `outbox_messages` nào tồn tại cho tới
    khi job của tiến trình bot chạy. Đây là bài duy nhất chứng minh mắt xích đó không đứt
    **im lặng** — kiểu hỏng mà docstring của route đã cảnh báo.
    """
    from televip.services import broadcast as bc

    csrf = await _vao(app_client)
    await _nguoi_nhan(5)
    job = await _tao_nhap(app_client, csrf)

    tong = await scalar("SELECT total FROM broadcast_jobs WHERE job_id = :j", {"j": job})
    assert tong >= 5, "tệp đích phải được dựng ngay lúc tạo nháp"

    r = await app_client.post(
        f"/bantin/{job}/gui", data={"so_dich": str(tong), "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "running"
    )

    # Web KHÔNG bơm: chưa có dòng hàng đợi nào.
    assert await scalar("SELECT count(*) FROM outbox_messages") == 0, (
        "web không được tự bơm — nó không giữ kết nối Telegram nào"
    )

    # Đúng thứ job của tiến trình bot làm.
    await bc.resume_running_jobs()
    import asyncio

    for _ in range(40):
        if await scalar("SELECT count(*) FROM outbox_messages") >= tong:
            break
        await asyncio.sleep(0.1)

    assert await scalar("SELECT count(*) FROM outbox_messages") == tong, (
        "sau khi job bơm chạy, mỗi đích phải có đúng một dòng hàng đợi"
    )


# ── Bốn điều kiện của start() ───────────────────────────────────────


@pytest.mark.asyncio
async def test_bam_gui_hai_lan_chi_bat_dau_mot_lan(app_client: httpx.AsyncClient):
    """Hai tab, hoặc F5 trên POST. Chặn ở tầng database, không ở giao diện."""
    csrf = await _vao(app_client)
    await _nguoi_nhan(3)
    job = await _tao_nhap(app_client, csrf)
    tong = await scalar("SELECT total FROM broadcast_jobs WHERE job_id = :j", {"j": job})

    for _ in range(2):
        r = await app_client.post(
            f"/bantin/{job}/gui", data={"so_dich": str(tong), "_csrf": csrf}, headers=_goc()
        )
        assert r.status_code == 303

    # Hai dòng sổ — cả hai lượt bấm đều được ghi — nhưng chỉ MỘT lượt bắt đầu được.
    assert (
        await scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'adminweb.broadcast_start' "
            "AND entity_id = :j",
            {"j": str(job)},
        )
        == 2
    )
    assert (
        await scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'adminweb.broadcast_start' "
            "AND entity_id = :j AND (after->>'bat_dau_duoc')::bool",
            {"j": str(job)},
        )
        == 1
    ), "lượt bấm thứ hai không được bắt đầu lại đợt"


@pytest.mark.asyncio
async def test_so_dich_lech_thi_tu_choi_gui(app_client: httpx.AsyncClient):
    """Màn xem thử mở từ lâu nói "3.000 người" trong khi tệp thật đã khác.

    Bấm với một con số không còn đúng là bấm với thông tin sai.
    """
    csrf = await _vao(app_client)
    await _nguoi_nhan(3)
    job = await _tao_nhap(app_client, csrf)
    tong = await scalar("SELECT total FROM broadcast_jobs WHERE job_id = :j", {"j": job})

    await app_client.post(
        f"/bantin/{job}/gui", data={"so_dich": "9999", "_csrf": csrf}, headers=_goc()
    )
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "draft"

    await app_client.post(
        f"/bantin/{job}/gui", data={"so_dich": str(tong), "_csrf": csrf}, headers=_goc()
    )
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "running"
    )


@pytest.mark.asyncio
async def test_khong_gui_duoc_dot_KHAC_LOAI(app_client: httpx.AsyncClient):
    """Leo thang quyền: dùng đường `/broadcast` để khởi động một đợt `send_event`.

    Trên Telegram điều này an toàn vì `callback_data` chỉ đến từ nút do bot dựng. Trên web
    `job_id` là một trường POST giả được — nên `kind` phải là điều kiện của câu `UPDATE`.
    """
    csrf = await _vao(app_client)
    await _nguoi_nhan(3)
    from sqlalchemy import text as sql

    from televip.db.engine import session as db_session

    # Tự commit: `scalar()` của tệp kia KHÔNG commit, nên một `INSERT` qua nó bị cuộn lại
    # và bài kiểm sẽ xanh vì không có gì để bắt đầu — đúng loại bài kiểm chết.
    async with db_session() as s:
        job = int(
            (
                await s.execute(
                    sql("""
                    INSERT INTO broadcast_jobs (kind, audience, payload, state, total,
                                                created_by)
                         VALUES ('send_event', 'all', '{"text":"x"}'::jsonb, 'draft', 3, :a)
                      RETURNING job_id
                    """),
                    {"a": OWNER},
                )
            ).scalar_one()
        )
        await s.commit()
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "draft"

    r = await app_client.post(
        f"/bantin/{job}/gui", data={"so_dich": "3", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code in (303, 404)
    con_lai = await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job})
    assert con_lai != "running", "đợt send_event không được khởi động qua đường /broadcast"


@pytest.mark.asyncio
async def test_khong_gui_duoc_nhap_cua_NGUOI_KHAC(app_client: httpx.AsyncClient):
    from televip.db.engine import session as db_session

    csrf = await _vao(app_client)
    await _nguoi_nhan(3)
    async with db_session() as s:
        await make_user(s, NGUOI_KHAC)
        await s.commit()

    job = await _tao_nhap(app_client, csrf)
    await run_sql(
        "UPDATE broadcast_jobs SET created_by = :u WHERE job_id = :j",
        {"u": NGUOI_KHAC, "j": job},
    )
    tong = await scalar("SELECT total FROM broadcast_jobs WHERE job_id = :j", {"j": job})

    await app_client.post(
        f"/bantin/{job}/gui", data={"so_dich": str(tong), "_csrf": csrf}, headers=_goc()
    )
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "draft"

    # Và màn xem thử nói rõ vì sao không có nút.
    t = (await app_client.get(f"/bantin/{job}/xemthu")).text
    assert "không phải của bạn" in t


@pytest.mark.asyncio
async def test_nhap_qua_han_khong_bam_gui_duoc(app_client: httpx.AsyncClient):
    """Tệp đích đóng băng lúc tạo nháp — một nháp để quên gửi cho tệp không còn đúng."""
    csrf = await _vao(app_client)
    await _nguoi_nhan(3)
    job = await _tao_nhap(app_client, csrf)
    tong = await scalar("SELECT total FROM broadcast_jobs WHERE job_id = :j", {"j": job})
    await run_sql(
        "UPDATE broadcast_jobs SET created_at = now() - interval '2 hours' WHERE job_id = :j",
        {"j": job},
    )

    await app_client.post(
        f"/bantin/{job}/gui", data={"so_dich": str(tong), "_csrf": csrf}, headers=_goc()
    )
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "draft"

    t = (await app_client.get(f"/bantin/{job}/xemthu")).text
    assert "quá hạn" in t


# ── Hàng rào lúc tạo nháp ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_noi_dung_qua_dai_bi_tu_choi(app_client: httpx.AsyncClient):
    """Vượt trần là `BadRequest` — lỗi vĩnh viễn: cả đợt chạy hết mà KHÔNG AI nhận được gì."""
    from televip.services import broadcast as bc

    csrf = await _vao(app_client)
    await _nguoi_nhan(3)

    r = await app_client.post(
        "/bantin/moi",
        data={"noi_dung": "x" * (bc.MAX_CONTENT_CHARS + 1), "tep": "all", "_csrf": csrf},
        headers=_goc(),
    )
    assert r.status_code == 200, "phải trả lại ô soạn, không phải 500"
    assert "vượt trần" in r.text
    assert await scalar("SELECT count(*) FROM broadcast_jobs") == 0


@pytest.mark.asyncio
async def test_duoi_nguong_thi_nhap_bi_HUY_ngay(app_client: httpx.AsyncClient):
    """Một đợt bị từ chối mà vẫn nằm ở `draft` là một nút GỬI treo lơ lửng."""
    csrf = await _vao(app_client, nguong=50)
    await _nguoi_nhan(3)

    r = await app_client.post(
        "/bantin/moi", data={"noi_dung": "xin chao", "tep": "all", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 200
    assert "dưới ngưỡng 50" in r.text
    assert await scalar("SELECT count(*) FROM broadcast_jobs WHERE state = 'draft'") == 0
    assert await scalar("SELECT count(*) FROM broadcast_jobs WHERE state = 'cancelled'") == 1


@pytest.mark.asyncio
async def test_tao_nhap_moi_huy_nhap_cu_cua_chinh_minh(app_client: httpx.AsyncClient):
    """Hai bản nháp cùng sống nghĩa là một nội dung đã bị thay vẫn bấm gửi được."""
    csrf = await _vao(app_client)
    await _nguoi_nhan(3)

    job1 = await _tao_nhap(app_client, csrf, body="ban cu")
    job2 = await _tao_nhap(app_client, csrf, body="ban moi")

    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job1})
        == "cancelled"
    )
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job2}) == "draft"
    )


@pytest.mark.asyncio
async def test_tep_mac_dinh_la_tep_HEP(app_client: httpx.AsyncClient):
    """Ô chọn mà mục đầu là "TOÀN BỘ" biến hàng rào này thành cái ngược lại."""
    csrf = await _vao(app_client)
    t = (await app_client.get("/bantin/moi")).text

    # Ghi THẲNG giá trị mong đợi, không đối chiếu với `bc.DEFAULT_AUDIENCE`: so một hằng
    # với chính nó là một khẳng định không bao giờ đỏ được, kể cả khi ai đó đổi mặc định
    # thành "toàn bộ". Chính cái tên `active_30d` mới là thứ bài kiểm này khoá.
    dau = t.index("<select")
    tuy_chon_dau = t[dau : dau + 400].split("</option>")[0]
    assert "active_30d" in tuy_chon_dau, "mục ĐẦU của ô chọn phải là tệp hẹp"
    assert '"all"' not in tuy_chon_dau, "tệp TOÀN BỘ không được là mục đầu"

    # Không gửi trường `tep` ⇒ service phải nhận tệp hẹp.
    await _nguoi_nhan(3)
    r = await app_client.post(
        "/bantin/moi", data={"noi_dung": "xin chao", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303
    assert await scalar("SELECT audience FROM broadcast_jobs LIMIT 1") == "active_30d"


# ── Ranh giới ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gui_khong_co_csrf_thi_404(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    await _nguoi_nhan(3)
    job = await _tao_nhap(app_client, csrf)

    r = await app_client.post(f"/bantin/{job}/gui", data={"so_dich": "3"}, headers=_goc())
    assert r.status_code == 404
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "draft"


@pytest.mark.asyncio
async def test_chi_co_broadcast_status_thi_khong_soan_duoc(app_client: httpx.AsyncClient):
    """Màn xem tiến độ và màn soạn là hai quyền khác nhau."""
    from televip.services import admin as admin_service

    await _dung_admin()
    await run_sql(
        "INSERT INTO admin_permissions (role, command) VALUES ('owner', '/broadcast_status') "
        "ON CONFLICT DO NOTHING"
    )
    admin_service.invalidate_role(OWNER)
    await _dang_nhap(app_client)

    assert (await app_client.get("/bantin")).status_code == 200
    assert (await app_client.get("/bantin/moi")).status_code == 404
