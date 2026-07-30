"""Bàn phím và câu chữ — chống trôi nhãn.

Nhãn nút là **khoá định tuyến** (§13.3.1), nên một dấu cách thừa hay một dấu gạch nối
gõ nhầm là một nút chết, không phải một lỗi hiển thị. Vì vậy test dưới đây viết chuỗi
literal thay vì tham chiếu hằng số: nếu ai đó "sửa cho đẹp" cả hằng số lẫn test thì
diff vẫn phơi ra chuỗi thật, không giấu được sau một cái tên.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from televip.domain import texts
from televip.telegram import keyboards as kb

# Nhãn viết tay từ bảng §13.3.1, theo đúng thứ tự 5 hàng × 2 cột.
SPEC_LAYOUT = (
    ("🎮 Chơi Game 🎮", "🎁Code Tân Thủ"),
    ("💎 Mời bạn nhận quà", "📢 EVENT"),
    ("✅ Điểm Danh", "🎁 Đổi CODE"),
    ("👑 BXH", "📈Check Chia sẻ"),
    ("📊Thống Kê TK", "💁‍♀️ Hỗ Trợ – CSKH"),
)


def _labels(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.keyboard]


# ── Bố cục ──────────────────────────────────────────────────────────


def test_main_keyboard_co_5_hang_10_nut():
    rows = _labels(kb.main_keyboard())
    assert len(rows) == 5
    assert all(len(r) == 2 for r in rows)
    assert sum(len(r) for r in rows) == 10


def test_main_keyboard_khop_tung_ky_tu_voi_dac_ta():
    assert _labels(kb.main_keyboard()) == [list(row) for row in SPEC_LAYOUT]


def test_main_keyboard_resize_va_khong_one_time():
    markup = kb.main_keyboard()
    assert markup.resize_keyboard is True
    assert not markup.one_time_keyboard


def test_ba_nhan_thieu_dau_cach_sau_emoji_duoc_giu_nguyen():
    # Đây là chỗ dễ bị "sửa cho đẹp" nhất — sửa là gãy định tuyến của người dùng cũ.
    assert kb.BTN_CODE_TAN_THU == "🎁Code Tân Thủ"
    assert kb.BTN_THONG_KE_TK == "📊Thống Kê TK"
    assert kb.BTN_CHECK_CHIA_SE == "📈Check Chia sẻ"
    for label in (kb.BTN_CODE_TAN_THU, kb.BTN_THONG_KE_TK, kb.BTN_CHECK_CHIA_SE):
        assert " " not in label[:2]


def test_nhan_ho_tro_dung_en_dash_va_emoji_zwj():
    assert kb.BTN_HO_TRO == "💁‍♀️ Hỗ Trợ – CSKH"
    assert "–" in kb.BTN_HO_TRO  # en dash, KHÔNG phải "-" (U+002D)
    assert "-" not in kb.BTN_HO_TRO
    assert "‍" in kb.BTN_HO_TRO  # zero-width joiner của emoji 💁‍♀️


# ── ROUTE_TABLE ─────────────────────────────────────────────────────


def test_route_table_phu_dung_bang_bo_nut_cua_ban_phim():
    on_keyboard = {label for row in _labels(kb.main_keyboard()) for label in row}
    assert set(kb.ROUTE_TABLE) == on_keyboard


def test_route_table_khong_co_handler_trung_nhau():
    assert len(set(kb.ROUTE_TABLE.values())) == len(kb.ROUTE_TABLE)


def test_route_of_khop_bang_nhau_tuyet_doi():
    # Bot cũ khớp bằng `"Chơi" in text or "Game" in text` nên nuốt cả tin nhắn thường.
    assert kb.route_of("🎮 Chơi Game 🎮") == "play_game"
    assert kb.route_of("tôi muốn Chơi Game") is None
    assert kb.route_of("🎮 Chơi Game 🎮 ") is None
    assert kb.route_of("🎁 Code Tân Thủ") is None  # thừa một dấu cách
    assert kb.route_of("") is None


# ── Nút inline (§13.3.3) ────────────────────────────────────────────


def test_callback_data_khop_dac_ta():
    assert kb.open_gift_keyboard().inline_keyboard[0][0].callback_data == "open_gift"
    assert kb.check_groups_keyboard().inline_keyboard[0][0].callback_data == "check_groups"
    assert kb.join_referral_keyboard().inline_keyboard[0][0].callback_data == "join_referral"
    assert kb.open_box_keyboard(42).inline_keyboard[0][0].callback_data == "dap_hop_42"
    assert kb.redeem_keyboard([10000]).inline_keyboard[0][0].callback_data == "redeem_10000"


def test_nhan_inline_khop_tung_ky_tu():
    assert kb.open_gift_keyboard().inline_keyboard[0][0].text == "🎁 Mở Quà Ngay 🎁"
    assert kb.verify_keyboard("https://x.test").inline_keyboard[0][0].text == "🤖 Xác minh ngay"
    assert (
        kb.verify_step1_keyboard("https://x.test").inline_keyboard[0][0].text
        == "🤖 Xác minh ngay (BƯỚC 1)"
    )
    assert (
        kb.check_groups_keyboard().inline_keyboard[0][0].text == "✨ Tôi Đã Tham Gia - Nhận CODE ✨"
    )
    assert kb.join_referral_keyboard().inline_keyboard[0][0].text == "💎 Tham gia ngay"
    assert (
        kb.share_post_keyboard("https://t.me/b?start=ref_1").inline_keyboard[0][0].text
        == "🎁 Tham Gia Nhận CODE Tân Thủ 🎁"
    )
    assert kb.open_box_keyboard(1).inline_keyboard[0][0].text == "🎁 Đập Hộp 🎁"
    assert kb.enter_code_keyboard("https://g.test").inline_keyboard[0][0].text == (
        "🎁 Nhập CODE Tại Đây 💸"
    )
    share = kb.share_event_keyboard("https://s.test", "https://c.test")
    assert share.inline_keyboard[0][0].text == "📤 Chia Sẻ Ngay"
    assert share.inline_keyboard[1][0].text == "✅ Đã Chia Sẻ - Liên Hệ CSKH"


def test_nut_check_groups_chi_co_mot_bien_the():
    # Bot cũ có ba chuỗi khác nhau cho cùng callback_data này.
    assert kb.BTN_CHECK_GROUPS.count("  ") == 0
    assert "10K" not in kb.BTN_CHECK_GROUPS


def test_redeem_keyboard_dung_mot_nut_moi_bac():
    markup = kb.redeem_keyboard([10000, 20000, 50000])
    assert [row[0].text for row in markup.inline_keyboard] == [
        "💎 Đổi Code 10K (10.000đ)",
        "💎 Đổi Code 20K (20.000đ)",
        "💎 Đổi Code 50K (50.000đ)",
    ]
    assert kb.redeem_keyboard([]).inline_keyboard == ()


def test_leaderboard_keyboard_vo_hieu_nut_dang_xem():
    today = kb.leaderboard_keyboard(today=True)
    assert [b.text for b in today.inline_keyboard[0]] == ["📅 Hôm nay", "👑 Toàn thời gian"]
    assert today.inline_keyboard[0][0].callback_data == kb.CB_NOOP
    assert today.inline_keyboard[0][1].callback_data == "lb_alltime"

    alltime = kb.leaderboard_keyboard(today=False)
    assert alltime.inline_keyboard[0][0].callback_data == "lb_today"
    assert alltime.inline_keyboard[0][1].callback_data == kb.CB_NOOP


def test_webapp_va_url_duoc_gan_dung_cho():
    assert kb.verify_keyboard("https://mini.test/v").inline_keyboard[0][0].web_app.url == (
        "https://mini.test/v"
    )
    assert kb.play_game_keyboard("https://t.me/game").inline_keyboard[0][0].url == (
        "https://t.me/game"
    )


# ── texts.py ────────────────────────────────────────────────────────


def test_dinh_dang_tien_kieu_viet_nam():
    assert texts.format_vnd(10000) == "10.000"
    assert texts.format_vnd(12000000) == "12.000.000"
    assert texts.format_vnd(0) == "0"
    assert texts.value_label(88000) == "88K"


def test_countdown_theo_cong_thuc_bat_buoc():
    assert texts.countdown(0) == "⏰ Chiến dịch đã kết thúc"
    # Bot cũ đọc timedelta.seconds riêng lẻ nên chiến dịch âm vẫn hiện "Còn 0 ngày 13 giờ".
    assert texts.countdown(-50000) == "⏰ Chiến dịch đã kết thúc"
    assert texts.countdown(2 * 86400 + 3 * 3600 + 59) == "Còn 2 ngày 3 giờ"


@pytest.mark.parametrize(
    ("qualified", "claimed", "expected_head"),
    [
        (0, 0, "⏳ Còn 5 người nữa để nhận lần thứ 1"),
        (7, 1, "⏳ Còn 3 người nữa để nhận lần thứ 2"),
        (10, 1, "🎁 ĐỦ ĐIỀU KIỆN nhận lần thứ 2!"),
        (60, 10, "✅ ĐÃ ĐẠT TỐI ĐA (10/10 lần)"),
    ],
)
def test_share_status_dung_ba_trang_thai(qualified, claimed, expected_head):
    line = texts.share_status_line(qualified=qualified, claimed=claimed, interval=5, max_claims=10)
    assert line.startswith(expected_head)


def test_share_status_trang_thai_3_keo_theo_dong_dang_xu_ly():
    line = texts.share_status_line(qualified=10, claimed=1, interval=5, max_claims=10)
    assert "⏳ Code đang được xử lý, sẽ tới trong ít phút." in line


def test_campaign_block_dung_chung_cho_hai_man_hinh():
    kwargs = dict(
        interval=5,
        reward_value_vnd=10000,
        remaining_seconds=86400,
        max_claims=10,
        claimed=2,
        ref_link="https://t.me/b?start=ref_9",
    )
    block = texts.campaign_block(**kwargs)
    assert block in texts.referral_invite(**kwargs)
    assert block in texts.share_progress(qualified=12, total_vnd=20000, **kwargs)
    assert "Mời 5 người = 1 code 10K" in block
    assert "🎯 Tối đa: 10 lần/người (tối đa 50 người)" in block


def test_texts_khop_dac_ta_tung_ky_tu():
    assert texts.start_welcome().splitlines()[0] == "🎁TELEVIP - TẶNG CODE 24/7🎲"
    assert texts.not_verified().startswith("⚠️ BẠN CHƯA XÁC THỰC TÀI KHOẢN!")
    assert texts.missing_groups(1, 3).splitlines()[2] == "📊 Tiến độ: 1/3"
    assert "💰 CODE TÂN THỦ: ABC-123" in texts.code_delivered("ABC-123", 10000)
    assert "💵 Giá trị: 10.000đ" in texts.code_delivered("ABC-123", 10000)
    assert texts.out_of_stock("https://t.me/cskh") == (
        "❌ CODE ĐÃ HẾT VUI LÒNG LIÊN HỆ CSKH ĐỂ ĐƯỢC HỖ TRỢ\n\n🔗 https://t.me/cskh"
    )


def test_duong_ke_giu_dung_so_ky_tu_cua_dac_ta():
    assert len(texts.SEP) == 18
    assert len(texts.SEP_WIDE) == 24
    assert len(texts.SEP_XWIDE) == 30


def test_tanthu_step2_danh_so_theo_so_nhom_that():
    msg = texts.tanthu_step2(["https://t.me/a", "https://t.me/b"], "https://fb.test/p")
    assert "👉 Tham gia đủ 2 nhóm/kênh sau:" in msg
    assert "1️⃣ https://t.me/a" in msg
    assert "2️⃣ https://t.me/b" in msg


def test_event_caption_sinh_tu_dung_bang_ti_le_dung_de_quay():
    caption = texts.event_box_caption([(0, 6000), (5000, 3500), (88000, 100)], "https://g.test")
    assert "🤮 Hộp rỗng 60% 👻" in caption
    assert "🎲 5k = 🎯 35%" in caption
    assert "🎲 88k = 🎯 1%" in caption


def test_redeem_menu_bac_du_va_chua_du():
    msg = texts.redeem_menu(balance=12000, tiers=[10000, 20000, 50000], points_per_day=2000)
    assert "✅ Code 10K (10.000đ) - ĐỦ ĐIỂM" in msg
    assert "⏳ Code 20K (20.000đ) - Còn thiếu 8.000đ (~4 ngày)" in msg
    assert msg.endswith("👇 Chọn loại code bạn muốn đổi:")

    ngheo = texts.redeem_menu(balance=0, tiers=[10000], points_per_day=2000)
    assert ngheo.endswith('👉 Bấm "✅ Điểm Danh" để tích thêm điểm!')
    assert "👇 Chọn" not in ngheo


def test_leaderboard_muc_rong_khong_lam_hong_muc_khac():
    msg = texts.leaderboard(
        today=True,
        top_referrers=[("@a", 5)],
        top_receivers=[],
        top_streaks=[("Bảo", 3)],
        updated_at=datetime(2026, 7, 30, 3, 0, tzinfo=UTC),
    )
    assert "👑 BẢNG XẾP HẠNG — HÔM NAY" in msg
    assert "🥇 @a\n   💎 5 người" in msg
    assert f"💰 TOP NHẬN CODE\n{texts.SEP}\n{texts.NO_DATA}" in msg
    assert "🥇 Bảo\n   🔥 3 ngày liên tiếp" in msg
    assert "⚡ Cập nhật: 10:00 30/07/2026" in msg  # UTC+7


def test_thoi_gian_hien_thi_luon_quy_ve_gio_viet_nam():
    with pytest.raises(ValueError, match="timezone"):
        texts.leaderboard(
            today=False,
            top_referrers=[],
            top_receivers=[],
            top_streaks=[],
            updated_at=datetime(2026, 7, 30, 3, 0),  # naive
        )


def test_display_name_uu_tien_username():
    assert texts.display_name("bob", "Bob Tran") == "@bob"
    assert texts.display_name(None, "Bob Tran") == "Bob Tran"
    assert texts.display_name(None, None) == texts.ANONYMOUS


def test_rate_limited_khong_bao_gio_hua_0_giay():
    assert "1 giây" in texts.rate_limited(0.2)
    assert "5 giây" in texts.rate_limited(4.1)
