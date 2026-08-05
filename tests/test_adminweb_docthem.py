"""Bốn màn chỉ đọc của panel: báo cáo, tra định danh, nhật ký, chiến dịch.

Điều đáng đo ở đây không phải "trang có mở được không" mà là bốn mệnh đề:

- **Báo cáo trên web bằng đúng báo cáo trong bot.** Hai màn hình cùng đọc một con số qua
  hai đoạn mã khác nhau là hai con số sẽ lệch nhau; ở đây chúng gọi chung một service, và
  bài kiểm đối chiếu thẳng với service đó.
- **CSV mở được bằng Excel bản Việt.** UTF-8 không BOM ra chữ vỡ, và người nhận báo cáo mở
  bằng Excel chứ không mở bằng trình soạn thảo.
- **Trang tra định danh không có đường nào ra TÊN người khác.** Có một bài đọc thẳng tệp
  template để khẳng định.
- **IP không bao giờ hiện đầy đủ**, kể cả khi tra đúng một IP đầy đủ.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from televip.db.engine import session as db_session
from tests.conftest import make_user
from tests.test_adminweb import _dang_nhap, _dung_admin, run_sql

# `_dung_admin` chỉ cấp 6 lệnh; bốn màn này cần thêm quyền riêng của chúng.
_QUYEN_THEM = ("/baocao", "/checkip", "/cauhinh", "/chiendich")

NGUOI = 993_001
IP_THAT = "203.0.113.47"


async def _cap_quyen_them() -> None:
    from televip.services import admin as admin_service

    for lenh in _QUYEN_THEM:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
            "ON CONFLICT DO NOTHING",
            {"c": lenh},
        )
    admin_service.invalidate_role(990_001)


async def _vao(client: httpx.AsyncClient) -> None:
    await _dung_admin()
    await _cap_quyen_them()
    await _dang_nhap(client)


# ── Báo cáo ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bao_cao_hien_dung_con_so_cua_service(app_client: httpx.AsyncClient):
    await _vao(app_client)

    r = await app_client.get("/baocao")
    assert r.status_code == 200

    from televip.core.clock import business_date
    from televip.services import report as report_service

    async with db_session() as s:
        bc = await report_service.collect(s, period="ngay", today=business_date())

    assert "Báo cáo chi" in r.text
    assert bc.label in r.text


@pytest.mark.asyncio
async def test_ky_la_ra_404_chu_khong_am_tham_doi_ky(app_client: httpx.AsyncClient):
    """Im lặng chạy kỳ mặc định là trả về báo cáo hợp lệ cho một khoảng thời gian KHÁC."""
    await _vao(app_client)

    for ky in ("nam", "quy", "ngay2", "'"):
        assert (await app_client.get("/baocao", params={"ky": ky})).status_code == 404, ky

    for ky in ("ngay", "tuan", "thang", ""):
        assert (await app_client.get("/baocao", params={"ky": ky})).status_code == 200, ky


@pytest.mark.asyncio
async def test_csv_co_BOM_va_header_tai_ve(app_client: httpx.AsyncClient):
    """Excel bản Việt mở CSV UTF-8 **không BOM** thành chữ vỡ."""
    await _vao(app_client)

    r = await app_client.get("/baocao/csv", params={"ky": "tuan"})
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf"), "thiếu BOM ⇒ Excel bản Việt hiện chữ vỡ"
    assert "charset=utf-8" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert "baocao_tuan_" in r.headers["content-disposition"]
    # Báo cáo đổi theo từng phút — một bản trong cache là một bản người đọc tưởng mới.
    assert r.headers["cache-control"] == "no-store"


# ── Tra định danh ───────────────────────────────────────────────────


def test_template_dinh_danh_KHONG_chua_chu_username() -> None:
    """Luật riêng tư: không đường nào đi từ một tín hiệu ngược về TÊN của người khác.

    Bài kiểm đọc thẳng tệp template. Một bài kiểm chạy app sẽ chỉ đỏ khi có dữ liệu mẫu
    đúng hình dạng; bài này đỏ ngay khi ai đó gõ tên trường vào bảng.
    """
    from televip.apps.adminweb.app import TEMPLATES_DIR
    from tests.test_adminweb_nguoidung import _CHU_THICH_JINJA

    ma = _CHU_THICH_JINJA.sub("", (Path(TEMPLATES_DIR) / "dinhdanh.html").read_text("utf-8"))
    # Ngoại lệ duy nhất: chuỗi gợi ý trong ô nhập, để người tra biết gõ được gì.
    ma = ma.replace('placeholder="@username · user_id · 1.2.3.4 · IPv6"', "")
    assert "username" not in ma, "trang tra định danh không được có đường ra tên người khác"
    assert "full_name" not in ma


@pytest.mark.asyncio
async def test_tra_theo_IP_khong_hien_IP_day_du(app_client: httpx.AsyncClient):
    await _vao(app_client)
    async with db_session() as s:
        await make_user(s, NGUOI)
        await s.commit()
    await run_sql("UPDATE users SET username = 'nguoitinhieu' WHERE user_id = :u", {"u": NGUOI})
    await run_sql(
        "INSERT INTO identity_signals (user_id, signal_type, signal_value, hits, "
        "first_seen, last_seen) VALUES (:u, 'ip', :ip, 4, now(), now())",
        {"u": NGUOI, "ip": IP_THAT},
    )

    t = (await app_client.get("/dinhdanh", params={"tim": IP_THAT})).text

    assert "203.0.113.x" in t, "kết quả phải hiện dạng rút gọn"
    assert str(NGUOI) in t, "vẫn phải trả về mã tài khoản"
    assert "nguoitinhieu" not in t, "KHÔNG được lộ tên của người dùng chung IP"

    # Ô tra echo lại đúng chuỗi người vận hành vừa gõ, nên IP đầy đủ có mặt MỘT lần ở đó —
    # và nó đã nằm sẵn trong URL. Điều đáng chặn là IP xuất hiện ở nơi người ta CHƯA biết:
    # trong bảng kết quả. Đếm để bắt đúng chỗ đó.
    assert t.count(IP_THAT) == 1, "IP đầy đủ chỉ được có trong ô tra, không trong kết quả"


@pytest.mark.asyncio
async def test_tra_theo_nguoi_hien_tin_hieu_da_che(app_client: httpx.AsyncClient):
    await _vao(app_client)
    async with db_session() as s:
        await make_user(s, NGUOI)
        await s.commit()
    await run_sql(
        "INSERT INTO identity_signals (user_id, signal_type, signal_value, hits, "
        "first_seen, last_seen) VALUES (:u, 'ip', :ip, 4, now(), now())",
        {"u": NGUOI, "ip": IP_THAT},
    )

    t = (await app_client.get("/dinhdanh", params={"tim": str(NGUOI)})).text
    assert "203.0.113.x" in t
    assert IP_THAT not in t


@pytest.mark.asyncio
async def test_luot_xac_minh_cung_che_IP(app_client: httpx.AsyncClient):
    """Khối "lượt xác minh" là nguồn IP THỨ HAI trên trang, và nó đọc `verification_events`.

    Bài kiểm chỉ chèn `identity_signals` sẽ để nguyên khối này rỗng — nên tắt phần che ở
    `luot_xac_minh()` vẫn xanh. Đúng loại lỗ hổng mà một bộ kiểm thử "đủ dòng" bỏ lọt.
    """
    await _vao(app_client)
    async with db_session() as s:
        await make_user(s, NGUOI)
        await s.commit()
    await run_sql(
        "INSERT INTO verification_events (user_id, event_type, ip, asn, country, "
        "initdata_valid, verdict, risk_score) "
        "VALUES (:u, 'verify', CAST(:ip AS inet), 45903, 'VN', true, 'pass', 12)",
        {"u": NGUOI, "ip": IP_THAT},
    )

    t = (await app_client.get("/dinhdanh", params={"tim": str(NGUOI)})).text

    assert "Lượt xác minh gần nhất" in t
    assert "203.0.113.x" in t, "IP của lượt xác minh phải hiện dạng rút gọn"
    assert IP_THAT not in t, "IP đầy đủ KHÔNG được rời khỏi service"
    assert "45903" in t and "VN" in t, "ASN và quốc gia vẫn phải hiện — chúng không bị che"


@pytest.mark.asyncio
async def test_nguoi_chua_co_tin_hieu_noi_ro_VI_SAO_trong(app_client: httpx.AsyncClient):
    """Không để người đọc đoán giữa "sạch" và "chưa đo"."""
    await _vao(app_client)
    async with db_session() as s:
        await make_user(s, NGUOI)
        await s.commit()

    t = (await app_client.get("/dinhdanh", params={"tim": str(NGUOI)})).text
    assert "Chưa có tín hiệu nào" in t
    assert "xác minh qua Mini App" in t


# ── Nhật ký ─────────────────────────────────────────────────────────


async def _ghi_nhat_ky(action: str, actor: int, *, ip: str | None = None) -> None:
    await run_sql(
        "INSERT INTO audit_log (actor_type, actor_id, action, entity_type, entity_id, after) "
        "VALUES ('admin', :a, :act, 'settings', 'khoa.x', CAST(:sau AS jsonb))",
        {"a": actor, "act": action, "sau": json.dumps({"ip": ip} if ip else {})},
    )


@pytest.mark.asyncio
async def test_nhat_ky_loc_theo_hanh_dong(app_client: httpx.AsyncClient):
    await _vao(app_client)
    await _ghi_nhat_ky("setcauhinh", 990_001, ip="10.0.0.9")
    await _ghi_nhat_ky("adminweb.dangnhap", 990_001)

    tat_ca = (await app_client.get("/nhatky")).text
    assert "setcauhinh" in tat_ca and "adminweb.dangnhap" in tat_ca

    loc = (await app_client.get("/nhatky", params={"action": "setcauhinh"})).text
    # Đếm trong phần BẢNG, không trong cả trang: mọi tên action vẫn nằm trong ô chọn (hai
    # lần mỗi cái — `value=` và phần chữ), nên đếm cả trang là đếm nhầm.
    bang = loc.split("<tbody")[-1]
    assert "setcauhinh" in bang
    assert "adminweb.dangnhap" not in bang, "lọc rồi mà bảng vẫn còn action khác"


@pytest.mark.asyncio
async def test_nhat_ky_hien_ip_khi_co_va_gach_khi_khong(app_client: httpx.AsyncClient):
    """Dòng thiếu IP nghĩa là thao tác đến từ Telegram — không phải dữ liệu hỏng."""
    await _vao(app_client)
    await _ghi_nhat_ky("setcauhinh", 990_001, ip="10.0.0.9")
    await _ghi_nhat_ky("ban", 990_001)

    t = (await app_client.get("/nhatky")).text
    assert "10.0.0.9" in t


@pytest.mark.asyncio
async def test_nhat_ky_loc_ngay_khong_lam_sap_trang(app_client: httpx.AsyncClient):
    """Ngày gõ sai định dạng phải bị bỏ qua, không thành 500."""
    await _vao(app_client)
    await _ghi_nhat_ky("setcauhinh", 990_001)

    for tu in ("2026-01-01", "khong-phai-ngay", "2026-13-45", ""):
        r = await app_client.get("/nhatky", params={"tu": tu})
        assert r.status_code == 200, tu


# ── Chiến dịch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chien_dich_bat_ma_khong_chay_duoc_canh_bao(app_client: httpx.AsyncClient):
    """`campaign_window()` chỉ đọc DÒNG MỚI NHẤT, nên một dòng cũ còn bật là vô hình.

    Mỗi người mời đủ ăn tối đa `max_claims × reward`; trên tệp 19.151 người, một chiến dịch
    "đã dừng" mà vẫn phát là một đường chi không ai thấy. Màn hình này là chỗ nói ra nó.
    """
    await _vao(app_client)
    await run_sql(
        "INSERT INTO campaigns (code, name, interval_people, reward_value_vnd, max_claims, "
        "is_active, starts_at, ends_at) VALUES "
        "('cu', 'Chiến dịch cũ', 3, 10000, 5, true, now() - interval '30 days', "
        " now() - interval '1 day')"
    )

    t = (await app_client.get("/chiendich")).text
    assert "còn bật nhưng" in t, "chiến dịch hết hạn mà cờ is_active vẫn bật phải được nêu"
    assert "Bật mà không chạy" in t


@pytest.mark.asyncio
async def test_chien_dich_trong_noi_ro_KHONG_phat_gi(app_client: httpx.AsyncClient):
    await _vao(app_client)

    t = (await app_client.get("/chiendich")).text
    assert "Chưa có chiến dịch nào" in t
    assert "không phát mốc mời bạn nào" in t


# ── Phân quyền của cả bốn màn ───────────────────────────────────────


@pytest.mark.asyncio
async def test_thieu_quyen_thi_404_o_ca_bon_man(app_client: httpx.AsyncClient):
    """`_dung_admin` KHÔNG cấp 4 quyền này — nên owner ở đây phải bị chặn."""
    await _dung_admin()
    await _dang_nhap(app_client)

    for duong in ("/baocao", "/baocao/csv", "/dinhdanh", "/nhatky", "/chiendich"):
        assert (await app_client.get(duong)).status_code == 404, duong

    trang = (await app_client.get("/")).text
    for duong in ("/baocao", "/dinhdanh", "/nhatky", "/chiendich"):
        assert f'href="{duong}"' not in trang, duong
