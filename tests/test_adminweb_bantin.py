"""Màn bắn tin của panel — và ranh giới của nó.

Miền rủi ro nhất: một đợt gửi tới 19.151 người và không rút lại được.

Ba mệnh đề được đo ở đây, và mệnh đề đầu tiên quan trọng hơn hai cái sau:

- **Panel KHÔNG có đường nào bắt đầu, tạm dừng hay chạy tiếp một đợt.** Không phải vì có
  một câu `if` chặn, mà vì không route nào tồn tại. Có một bài đọc thẳng bảng route để
  khẳng định — nó đỏ ngay khi ai đó thêm một route như vậy, kể cả trước khi có form.
- **Nút Huỷ đi trọn vẹn qua `broadcast_service.cancel()`**, và ghi sổ kể cả khi không huỷ
  được.
- **Quyền của màn xem là `/broadcast_status`, không phải `/broadcast`** — màn này không tạo
  được đợt nào, nên đòi quyền tạo đợt là đòi thừa.
"""

from __future__ import annotations

import re

import httpx
import pytest

from tests.test_adminweb import _dang_nhap, _dung_admin, run_sql, scalar
from tests.test_adminweb_ghi import _goc

_QUYEN = ("/broadcast_status", "/broadcast_cancel")


async def _vao(client: httpx.AsyncClient) -> str:
    from televip.services import admin as admin_service

    await _dung_admin()
    for lenh in _QUYEN:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(990_001)
    await _dang_nhap(client)
    return await scalar(
        "SELECT csrf_token FROM admin_sessions WHERE revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


async def _tao_dot(state: str = "running", *, total: int = 100) -> int:
    """Một đợt trong database. Tự commit — `scalar()` của tệp kia thì không."""
    from sqlalchemy import text

    from televip.db.engine import session as db_session

    async with db_session() as s:
        job = (
            await s.execute(
                text("""
                INSERT INTO broadcast_jobs (kind, audience, payload, state, total, created_by)
                     VALUES ('broadcast', 'all', '{"text": "xin chao"}'::jsonb, :s, :t, 990001)
                  RETURNING job_id
                """),
                {"s": state, "t": total},
            )
        ).scalar_one()
        await s.commit()
    return int(job)


# ── Ranh giới: panel không gửi được ─────────────────────────────────


def test_panel_KHONG_co_route_nao_bat_dau_hay_chay_tiep() -> None:
    """Đo bằng bảng route, không bằng một lần bấm.

    Một route lật `state='running'` mà không khởi động vòng bơm sẽ để lại: đợt ghi là "đang
    chạy", tệp đích đầy, KHÔNG một dòng outbox nào, KHÔNG một tin nào bay, KHÔNG một dòng
    log lỗi nào. Đợt đứng im tới lần restart bot kế tiếp — lúc đó cả đợt bỗng bắn đi.

    Bài này đỏ ngay khi ai đó thêm một route như vậy, kể cả trước khi có form gọi nó.
    """
    from televip.apps.adminweb.routes import bantin

    duong = {r.path for r in bantin.router.routes}
    cam = {
        "/bantin/{job_id}/gui",
        "/bantin/{job_id}/batdau",
        "/bantin/{job_id}/tamdung",
        "/bantin/{job_id}/chaytiep",
        "/bantin/tao",
    }
    assert not (duong & cam), f"panel không được có route gửi/tạm dừng/chạy tiếp: {duong & cam}"

    # Và đúng một đường GHI, không hơn.
    ghi = {r.path for r in bantin.router.routes if "POST" in getattr(r, "methods", set())}
    assert ghi == {"/bantin/{job_id}/huy"}, f"đường ghi ngoài dự kiến: {ghi}"


def test_tien_trinh_adminweb_khong_giu_doi_tuong_Bot() -> None:
    """Lý do gốc của mọi ranh giới ở trên: không có gì để gửi bằng."""
    from pathlib import Path

    from televip.apps.adminweb import app as adminweb_app

    thu_muc = Path(adminweb_app.__file__).parent
    # Tìm lệnh NHẬP, không tìm chữ: ghi chú của panel nhắc tới Telegram ở khắp nơi, và một
    # lời nhắc không phải một lời gọi Bot API.
    nhap = re.compile(r"^\s*(?:from|import)\s+telegram(?:\s|[.]|$)", re.MULTILINE)
    pham = [p.name for p in thu_muc.rglob("*.py") if nhap.search(p.read_text(encoding="utf-8"))]
    assert pham == [], f"tiến trình panel không được nhập thư viện telegram: {pham}"


# ── Đọc ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_danh_sach_va_chi_tiet_mo_duoc(app_client: httpx.AsyncClient):
    await _vao(app_client)
    job = await _tao_dot()

    ds = await app_client.get("/bantin")
    assert ds.status_code == 200
    assert f"#{job}" in ds.text
    assert "đang chạy" in ds.text

    ct = await app_client.get(f"/bantin/{job}")
    assert ct.status_code == 200
    assert "Chi tiết tệp đích" in ct.text


@pytest.mark.asyncio
async def test_dot_chua_bat_dau_KHONG_in_toc_do_bang_0(app_client: httpx.AsyncClient):
    """`0,0 tin/giây` cho một đợt chưa bơm dòng nào là bịa một con số đo được."""
    await _vao(app_client)
    job = await _tao_dot("draft")

    t = (await app_client.get(f"/bantin/{job}")).text
    assert "Chưa bắt đầu" in t
    assert "tin/giây" not in t


@pytest.mark.asyncio
async def test_job_id_hong_va_khong_ton_tai_deu_ra_404(app_client: httpx.AsyncClient):
    await _vao(app_client)
    for duong in ("/bantin/abc", "/bantin/-1", "/bantin/0", "/bantin/999999"):
        assert (await app_client.get(duong)).status_code == 404, duong


# ── Nút Huỷ ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_huy_dot_dang_chay(app_client: httpx.AsyncClient):
    csrf = await _vao(app_client)
    job = await _tao_dot("running")

    r = await app_client.post(f"/bantin/{job}/huy", data={"_csrf": csrf}, headers=_goc())
    assert r.status_code == 303
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job})
        == "cancelled"
    )

    assert (
        await scalar(
            "SELECT after->>'ip' FROM audit_log "
            "WHERE action = 'adminweb.broadcast_cancel' AND entity_id = :j",
            {"j": str(job)},
        )
        == "127.0.0.1"
    )


@pytest.mark.asyncio
async def test_huy_dot_da_xong_van_ghi_so(app_client: httpx.AsyncClient):
    """Một lượt bấm không đổi gì vẫn là một lượt có người ĐỊNH đổi.

    Đó đúng là thứ cần trả lời khi soát lại về sau: ai đã cố huỷ đợt nào, lúc nào.
    """
    csrf = await _vao(app_client)
    job = await _tao_dot("done")

    r = await app_client.post(f"/bantin/{job}/huy", data={"_csrf": csrf}, headers=_goc())
    assert r.status_code == 303
    assert await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "done"

    assert (
        await scalar(
            "SELECT (after->>'huy_duoc')::bool FROM audit_log "
            "WHERE action = 'adminweb.broadcast_cancel' AND entity_id = :j",
            {"j": str(job)},
        )
        is False
    )


@pytest.mark.asyncio
async def test_huy_khong_co_csrf_thi_404(app_client: httpx.AsyncClient):
    await _vao(app_client)
    job = await _tao_dot("running")

    r = await app_client.post(f"/bantin/{job}/huy", headers=_goc())
    assert r.status_code == 404
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "running"
    )


@pytest.mark.asyncio
async def test_khong_co_quyen_huy_thi_khong_co_nut_va_POST_bi_chan(app_client: httpx.AsyncClient):
    from televip.services import admin as admin_service

    await _dung_admin()
    await run_sql(
        "INSERT INTO admin_permissions (role, command) VALUES ('owner', '/broadcast_status') "
        "ON CONFLICT DO NOTHING"
    )
    admin_service.invalidate_role(990_001)
    await _dang_nhap(app_client)
    csrf = await scalar(
        "SELECT csrf_token FROM admin_sessions WHERE revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )
    job = await _tao_dot("running")

    t = (await app_client.get(f"/bantin/{job}")).text
    assert "Huỷ đợt" not in t, "không có quyền huỷ mà vẫn dựng nút"

    r = await app_client.post(f"/bantin/{job}/huy", data={"_csrf": csrf}, headers=_goc())
    assert r.status_code == 404
    assert (
        await scalar("SELECT state FROM broadcast_jobs WHERE job_id = :j", {"j": job}) == "running"
    )


@pytest.mark.asyncio
async def test_quyen_xem_la_broadcast_status_khong_phai_broadcast(app_client: httpx.AsyncClient):
    """Màn này không tạo được đợt nào, nên đòi quyền tạo đợt là đòi thừa."""
    from televip.services import admin as admin_service

    await _dung_admin()
    await run_sql(
        "INSERT INTO admin_permissions (role, command) VALUES ('owner', '/broadcast') "
        "ON CONFLICT DO NOTHING"
    )
    admin_service.invalidate_role(990_001)
    await _dang_nhap(app_client)

    # Có `/broadcast` mà KHÔNG có `/broadcast_status` ⇒ vẫn bị chặn.
    assert (await app_client.get("/bantin")).status_code == 404
    assert 'href="/bantin"' not in (await app_client.get("/")).text
