"""Test xác thực chữ ký Mini App.

Mỗi test dưới đây là một cách kẻ gian có thể thử vào. Nếu bất kỳ test nào chuyển sang
xanh khi lẽ ra phải đỏ (hoặc ngược lại), lỗ hổng của bot cũ đã quay lại.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest

from televip.apps.web.initdata import (
    build_init_data,
    parse_init_data,
    verify_signature,
)
from televip.core.errors import InitDataError, InitDataExpired, InitDataReplay

BOT_TOKEN = "123456789:AAtest-token-chi-dung-trong-test-khong-that"
OTHER_TOKEN = "987654321:AAmot-token-khac-hoan-toan-cho-test"

USER = {
    "id": 6989373720,
    "first_name": "Varder",
    "last_name": "4",
    "username": "Varder4",
    "language_code": "vi",
}


def _fresh(**overrides) -> str:
    user = {**USER, **overrides.pop("user", {})}
    return build_init_data(BOT_TOKEN, user, overrides.pop("auth_date", int(time.time())))


# ── Chữ ký ──────────────────────────────────────────────────────────


def test_chu_ky_dung_thi_qua():
    fields = verify_signature(_fresh(), BOT_TOKEN)
    assert json.loads(fields["user"])["id"] == USER["id"]


def test_chu_ky_sai_thi_tu_choi():
    data = _fresh()
    # Đổi đúng một ký tự trong hash
    tampered = data[:-1] + ("0" if data[-1] != "0" else "1")
    with pytest.raises(InitDataError):
        verify_signature(tampered, BOT_TOKEN)


def test_doi_du_lieu_ma_giu_chu_ky_cu_thi_tu_choi():
    """Kịch bản thật: bắt được initData của mình rồi sửa user_id thành người khác."""
    fields = dict(p.split("=", 1) for p in _fresh().split("&"))  # giữ nguyên dạng đã encode
    original_hash = fields["hash"]
    fields["user"] = urlencode({"u": json.dumps({**USER, "id": 999})})[2:]
    fields["hash"] = original_hash
    with pytest.raises(InitDataError):
        verify_signature(urlencode(fields), BOT_TOKEN)


def test_token_khac_thi_tu_choi():
    """Chữ ký ký bằng token của bot khác không được chấp nhận."""
    with pytest.raises(InitDataError):
        verify_signature(_fresh(), OTHER_TOKEN)


def test_thieu_hash_thi_tu_choi():
    with pytest.raises(InitDataError, match="hash"):
        verify_signature(urlencode({"auth_date": "123", "user": "{}"}), BOT_TOKEN)


def test_rong_thi_tu_choi():
    with pytest.raises(InitDataError):
        verify_signature("", BOT_TOKEN)


def test_username_co_ky_tu_dac_biet_van_qua():
    """Trường `user` là JSON đã percent-encode.

    Đây chính là chỗ hàm của bot cũ sai: nó không url-decode nên `data_check_string`
    dựng ra khác thứ Telegram đã ký, và bật lên là chặn nhầm mọi người dùng thật.
    """
    data = build_init_data(
        BOT_TOKEN,
        {**USER, "first_name": "Nguyễn Văn A & B", "last_name": "Đặng/Trần"},
        int(time.time()),
    )
    fields = verify_signature(data, BOT_TOKEN)
    assert "Nguyễn Văn A & B" in json.loads(fields["user"])["first_name"]


# ── Hạn dùng ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qua_han_thi_tu_choi():
    old = _fresh(auth_date=int(time.time()) - 3600)
    with pytest.raises(InitDataExpired):
        await parse_init_data(old, BOT_TOKEN, ttl_seconds=300, check_replay=False)


@pytest.mark.asyncio
async def test_auth_date_o_tuong_lai_thi_tu_choi():
    """Tự đặt auth_date tương lai để chữ ký không bao giờ hết hạn."""
    future = _fresh(auth_date=int(time.time()) + 7200)
    with pytest.raises(InitDataError, match="tương lai"):
        await parse_init_data(future, BOT_TOKEN, ttl_seconds=300, check_replay=False)


@pytest.mark.asyncio
async def test_lech_dong_ho_nho_van_chap_nhan():
    """Đồng hồ điện thoại nhanh vài giây là bình thường, không được chặn."""
    slightly_ahead = _fresh(auth_date=int(time.time()) + 20)
    parsed = await parse_init_data(slightly_ahead, BOT_TOKEN, ttl_seconds=300, check_replay=False)
    assert parsed.user_id == USER["id"]


# ── Chống phát lại ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dung_lai_lan_hai_thi_tu_choi(redis_clean):
    data = _fresh()
    first = await parse_init_data(data, BOT_TOKEN, ttl_seconds=300)
    assert first.user_id == USER["id"]

    with pytest.raises(InitDataReplay):
        await parse_init_data(data, BOT_TOKEN, ttl_seconds=300)


# ── Nội dung đã phân giải ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_doc_dung_thong_tin_nguoi_dung():
    parsed = await parse_init_data(_fresh(), BOT_TOKEN, ttl_seconds=300, check_replay=False)
    assert parsed.user_id == 6989373720
    assert parsed.username == "Varder4"
    assert parsed.full_name == "Varder 4"
    assert parsed.language_code == "vi"
    assert parsed.is_premium is False
