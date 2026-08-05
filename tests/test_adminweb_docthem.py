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


# ── Chiến dịch: ba đường ghi ────────────────────────────────────────


async def _csrf_moi() -> str:
    from tests.test_adminweb import scalar

    return await scalar(
        "SELECT csrf_token FROM admin_sessions WHERE revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


@pytest.mark.asyncio
async def test_mo_chien_dich_tu_web(app_client: httpx.AsyncClient):
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    csrf = await _csrf_moi()

    r = await app_client.post(
        "/chiendich/mo", data={"so_ngay": "30", "ten": "He ruc ro", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303
    assert await scalar("SELECT count(*) FROM campaigns WHERE is_active") == 1
    assert (
        await scalar("SELECT name FROM campaigns ORDER BY campaign_id DESC LIMIT 1") == "He ruc ro"
    )
    assert (
        await scalar("SELECT after->>'ip' FROM audit_log WHERE action = 'chiendich_start'")
        == "127.0.0.1"
    )


@pytest.mark.asyncio
async def test_mo_cai_moi_DUNG_moi_cai_dang_bat(app_client: httpx.AsyncClient):
    """Thứ tự bắt buộc: khoá → dừng mọi cái đang bật → mở cái mới.

    Hai dòng cùng `is_active` là dòng cũ thành "bật nhưng vô hình" — nó vẫn phát thưởng
    sau khi người vận hành đã đọc "đã dừng".
    """
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    csrf = await _csrf_moi()
    # Một chiến dịch cũ còn bật, chèn thẳng như một dòng sót lại.
    await run_sql(
        "INSERT INTO campaigns (code, name, interval_people, reward_value_vnd, max_claims, "
        "is_active, starts_at, ends_at) VALUES ('cu', 'Cu', 3, 10000, 5, true, "
        "now() - interval '10 days', now() + interval '10 days')"
    )

    await app_client.post(
        "/chiendich/mo", data={"so_ngay": "30", "ten": "Moi", "_csrf": csrf}, headers=_goc()
    )
    assert await scalar("SELECT count(*) FROM campaigns WHERE is_active") == 1, (
        "chỉ ĐÚNG MỘT chiến dịch được bật sau khi mở cái mới"
    )
    assert await scalar("SELECT name FROM campaigns WHERE is_active") == "Moi"


@pytest.mark.asyncio
async def test_gia_han_cong_don_tu_han_CU(app_client: httpx.AsyncClient):
    """Gia hạn 7 ngày cho chiến dịch còn 3 ngày phải ra 10 ngày.

    Tính từ `now()` là âm thầm CẮT NGẮN 3 ngày đang có.
    """
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    csrf = await _csrf_moi()
    await run_sql(
        "INSERT INTO campaigns (code, name, interval_people, reward_value_vnd, max_claims, "
        "is_active, starts_at, ends_at) VALUES ('c1', 'C1', 3, 10000, 5, true, "
        "now() - interval '1 day', now() + interval '3 days')"
    )

    r = await app_client.post(
        "/chiendich/giahan", data={"so_ngay": "7", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 303
    con_lai = await scalar(
        "SELECT round(EXTRACT(EPOCH FROM (ends_at - now())) / 86400) FROM campaigns "
        "WHERE code = 'c1'"
    )
    assert int(con_lai) == 10, f"phải ra 10 ngày, đang là {con_lai}"


@pytest.mark.asyncio
async def test_gia_han_khi_khong_co_cai_nao_dang_chay_thi_TU_CHOI(app_client: httpx.AsyncClient):
    """Gia hạn một cửa sổ đã đóng chính là MỞ LẠI van tiền — hai việc phải nhìn khác nhau."""
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    csrf = await _csrf_moi()

    # Một chiến dịch ĐÃ HẾT HẠN nhưng cờ `is_active` vẫn bật — trạng thái có thật, và là
    # cái bẫy: nếu truy vấn "đang chạy" quên điều kiện `ends_at > now()` thì lệnh gia hạn
    # sẽ kéo dài chính nó, tức MỞ LẠI van tiền mà không ai bấm nút mở.
    await run_sql(
        "INSERT INTO campaigns (code, name, interval_people, reward_value_vnd, max_claims, "
        "is_active, starts_at, ends_at) VALUES ('hethan', 'Het han', 3, 10000, 5, true, "
        "now() - interval '40 days', now() - interval '10 days')"
    )
    han_cu = await scalar("SELECT ends_at FROM campaigns WHERE code = 'hethan'")

    r = await app_client.post(
        "/chiendich/giahan", data={"so_ngay": "7", "_csrf": csrf}, headers=_goc()
    )
    assert r.status_code == 200, "phải trả lại trang kèm lý do, không phải 303"
    assert "mở lại van tiền" in r.text
    assert await scalar("SELECT ends_at FROM campaigns WHERE code = 'hethan'") == han_cu, (
        "chiến dịch đã hết hạn KHÔNG được gia hạn — đó là mở lại van tiền"
    )


@pytest.mark.asyncio
async def test_dung_tat_MOI_cai_dang_bat(app_client: httpx.AsyncClient):
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    csrf = await _csrf_moi()
    for ma in ("a", "b", "c"):
        await run_sql(
            "INSERT INTO campaigns (code, name, interval_people, reward_value_vnd, max_claims, "
            "is_active, starts_at, ends_at) VALUES (:m, :m, 3, 10000, 5, true, "
            "now() - interval '1 day', now() + interval '5 days')",
            {"m": ma},
        )

    r = await app_client.post("/chiendich/dung", data={"_csrf": csrf}, headers=_goc())
    assert r.status_code == 303
    assert await scalar("SELECT count(*) FROM campaigns WHERE is_active") == 0, (
        "phải dừng MỌI cái đang bật, không chỉ cái mới nhất"
    )


@pytest.mark.asyncio
async def test_so_ngay_la_bi_tu_choi(app_client: httpx.AsyncClient):
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    csrf = await _csrf_moi()

    for xau in ("0", "-5", "9999", "ba", "²", ""):
        r = await app_client.post(
            "/chiendich/mo", data={"so_ngay": xau, "_csrf": csrf}, headers=_goc()
        )
        assert r.status_code == 200, xau
    assert await scalar("SELECT count(*) FROM campaigns") == 0


@pytest.mark.asyncio
async def test_mo_chien_dich_khong_co_csrf_thi_404(app_client: httpx.AsyncClient):
    from tests.test_adminweb import scalar
    from tests.test_adminweb_ghi import _goc

    await _vao(app_client)
    r = await app_client.post("/chiendich/mo", data={"so_ngay": "30"}, headers=_goc())
    assert r.status_code == 404
    assert await scalar("SELECT count(*) FROM campaigns") == 0
