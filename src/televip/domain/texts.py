"""Toàn bộ chuỗi tiếng Việt hiển thị cho người dùng — một nơi duy nhất.

Nội dung chép từ `docs/ke-hoach-v2/13-dac-ta-bot-moi.md` §13.2. Handler chỉ gọi hàm ở
đây, **không tự nối chuỗi**. Lý do rất cụ thể: bot cũ có ba biến thể của cùng một nút
`check_groups` nằm ở ba file khác nhau (`✨ Tôi Đã Tham Gia - Nhận CODE  ✨` với hai dấu
cách, `… Nhận CODE 10K ✨`, `… Nhận CODE ✨`) vì mỗi nơi tự gõ lại. Khi câu chữ chỉ tồn
tại ở một chỗ thì hiện tượng đó không xảy ra được nữa.

Nguyên tắc của module:

- **Thuần khiết.** Hàm nhận tham số và trả chuỗi. Không đọc DB, không đọc `settings`,
  không đọc đồng hồ — mọi con số nghiệp vụ do tầng gọi truyền vào. Nhờ vậy test câu chữ
  không cần database, và đổi seed cấu hình không làm hỏng test.
- **Không viết cứng con số nghiệp vụ.** Mốc mời bạn, mệnh giá, bảng tỉ lệ event đều là
  tham số. `/help` của bot cũ liệt kê thang mốc 5/15/30/…/500 vốn không phải luật đang
  chạy — vì nó được gõ tay vào một chuỗi.

**Định dạng tiền:** dấu chấm phân cách nghìn (`10.000đ`), theo quy ước Việt Nam và theo
cách chính §13.6 viết mọi con số. Các khối mẫu trong §13.2 ghi `{value:,}` — đó là
format-spec Python bê nguyên từ bot cũ, không phải quy ước hiển thị. Muốn đổi lại chỉ
cần sửa `THOUSANDS_SEP`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import datetime

from televip.core.clock import VN_TZ

# ── Định dạng ───────────────────────────────────────────────────────

#: Dấu phân cách hàng nghìn. Việt Nam dùng dấu chấm.
THOUSANDS_SEP = "."

#: Ba độ dài đường kẻ mà §13.2 dùng, giữ đúng số ký tự của đặc tả.
SEP = "━" * 18
SEP_WIDE = "━" * 24
SEP_XWIDE = "━" * 30
SEP_SHARE = "━" * 15
SEP_DASH = "➖" * 9

#: Hạng 1-3 dùng huy chương; từ hạng 4 trở đi dùng số thứ tự.
_MEDALS = ("🥇", "🥈", "🥉")

#: Ký hiệu số có khung, dùng cho danh sách nhóm/kênh ở bước 2.
_KEYCAPS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

NO_DATA = "Chưa có dữ liệu"
NO_RANK = "Chưa xếp hạng"
ANONYMOUS = "Ẩn danh"


def format_vnd(value_vnd: int) -> str:
    """`10000` → `10.000`. Không kèm đơn vị — nơi gọi tự thêm `đ` hoặc ` VNĐ`."""
    return f"{value_vnd:,}".replace(",", THOUSANDS_SEP)


def value_label(value_vnd: int) -> str:
    """`10000` → `10K`. Nhãn ngắn dùng trong tên loại code."""
    return f"{value_vnd // 1000}K"


def display_name(username: str | None, full_name: str | None) -> str:
    """Tên hiển thị trên bảng xếp hạng: `@username` → `full_name` → `Ẩn danh`."""
    if username:
        return f"@{username}"
    if full_name:
        return full_name
    return ANONYMOUS


def _vn_time(moment: datetime, *, with_seconds: bool) -> str:
    """Mốc thời gian cho người đọc, luôn quy về giờ Việt Nam.

    Ép `astimezone` ngay tại chỗ hiển thị: tầng dưới lưu và truyền UTC (`clock.py`), còn
    người dùng chỉ hiểu giờ VN. Không nhận datetime naive vì không đoán được nó đang ở
    múi nào — đúng cái bẫy đã làm streak điểm danh của bot cũ lệch 7 tiếng.
    """
    if moment.tzinfo is None:
        raise ValueError("texts cần datetime có timezone — không nhận naive datetime")
    local = moment.astimezone(VN_TZ)
    pattern = "%H:%M:%S %d/%m/%Y" if with_seconds else "%H:%M %d/%m/%Y"
    return local.strftime(pattern)


def vn_time(moment: datetime, *, with_seconds: bool) -> str:
    """Bản công khai của `_vn_time`.

    Cần thiết vì hai màn hình thống kê / bảng xếp hạng đi qua `text_service`: ở đó dấu
    thời gian là **một biến chuỗi** (`{updated_at}`) do tầng gọi dựng sẵn, chứ không phải
    một `datetime` được hàm dưới đây định dạng. Nếu tầng gọi tự `strftime` thì quy ước
    hiển thị giờ có hai nguồn, và bản admin sửa được sẽ trôi khác bản mặc định.
    """
    return _vn_time(moment, with_seconds=with_seconds)


# ── Alert của answerCallbackQuery ───────────────────────────────────
# §13.3.3: mọi callback phải được answer trong 2 giây, kể cả nhánh lỗi.

ALERT_ALREADY_VERIFIED = "⚠️ Bạn đã xác thực rồi!"
ALERT_NEED_VERIFY = "⚠️ Bạn cần xác thực trước!"
ALERT_CHECKING = "Đang kiểm tra..."
ALERT_LOADING_SHARE_POST = "📤 Đang tải bài viết để chia sẻ..."
ALERT_ALREADY_JOINED_EVENT = "⚠️ Bạn đã tham gia event này rồi!"
ALERT_BOX_EMPTY = "🤮 Chúc bạn may mắn lần sau!"
ALERT_REDEEM_OK = "✅ Đổi code thành công!"
ALERT_OUT_OF_STOCK = "❌ CODE ĐÃ HẾT!"
ALERT_NOT_ENOUGH_POINTS = "⚠️ Không đủ điểm!"


# ── 13.2.1 · /start ─────────────────────────────────────────────────


def start_welcome() -> str:
    """Caption tin 1 của `/start` (ảnh `banner_start`)."""
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        "Mời bạn nhận quà không giới hạn....!✅\n"
        "Mời 5 người nhận 1 code👇"
    )


def start_gift_teaser() -> str:
    """Caption tin 2 của `/start` (ảnh `banner_gift`), đi kèm nút `🎁 Mở Quà Ngay 🎁`."""
    return "🎁TELEVIP - TẶNG CODE 24/7🎲\n\n🌟Tân Thủ Ngẫu nhiên Nhận 10K, 20K, 50K, 88K🌈"


# ── 13.2.2 · Xác minh ───────────────────────────────────────────────


def not_verified() -> str:
    """Cổng chặn xác minh — dùng chung cho MỌI nhánh chưa xác minh.

    Bot cũ có bản sao riêng của câu này cho nhánh đập hộp; ở đây chỉ một hàm.
    """
    return (
        "⚠️ BẠN CHƯA XÁC THỰC TÀI KHOẢN!\n"
        "\n"
        "🔐 Để sử dụng các chức năng của bot, bạn cần xác thực tài khoản trước.\n"
        "\n"
        "✅ Chỉ mất 5 giây\n"
        "🔒 An toàn & bảo mật\n"
        "🎁 Xác thực xong nhận ngay CODE!\n"
        "\n"
        "👇 Bấm nút bên dưới để xác thực:"
    )


def already_verified() -> str:
    """Màn hình thay thế khi người đã xác minh vẫn bấm `🎁 Mở Quà Ngay 🎁`."""
    return (
        "✅ BẠN ĐÃ XÁC THỰC TÀI KHOẢN!\n"
        "\n"
        "🎉 Bạn có thể sử dụng đầy đủ các chức năng của bot:\n"
        "\n"
        "🎁 Code Tân Thủ\n"
        "💎 Mời bạn nhận quà\n"
        "📢 EVENT\n"
        "✅ Điểm Danh\n"
        "🎁 Đổi CODE\n"
        "👑 BXH\n"
        "📈 Check Chia sẻ\n"
        "📊 Thống Kê TK\n"
        "\n"
        "💡 Chọn chức năng từ menu bên dưới!"
    )


def verify_success() -> str:
    """Câu trả về Mini App sau khi `POST /api/verify` thành công."""
    return "✅ Xác minh thành công!"


def verify_session_expired() -> str:
    """`auth_date` quá hạn hoặc initData bị phát lại."""
    return "Phiên xác minh đã hết hạn, mở lại Mini App"


def verify_rejected(support_link: str) -> str:
    """Điểm rủi ro vượt ngưỡng chặn.

    Cố ý chung chung: §13.2.2 cấm để lộ IP hay @username của tài khoản khác trong thông
    báo từ chối — kẻ dò tìm sẽ dùng chính thông báo đó để lập bản đồ tài khoản.
    """
    return (
        "⚠️ Không thể xác minh tài khoản của bạn lúc này.\n"
        "\n"
        f"💡 Vui lòng liên hệ CSKH để được hỗ trợ: {support_link}"
    )


# ── 13.2.3 · Code tân thủ ───────────────────────────────────────────


def tanthu_already_claimed(game_link: str) -> str:
    """Bước 0: đã có grant `tanthu`."""
    return (
        "⚠️ Bạn đã nhận Code Tân Thủ rồi!\n"
        "\n"
        "Mỗi tài khoản chỉ được nhận 1 lần.\n"
        "\n"
        "🎮 Chơi game ngay:\n"
        f"{game_link}"
    )


def tanthu_step1() -> str:
    """Bước 1 — chưa xác minh."""
    return (
        "🎁 CODE TÂN THỦ - 2 BƯỚC\n"
        "\n"
        f"{SEP}\n"
        "📋 BƯỚC 1: XÁC MINH (CHƯA LÀM)\n"
        f"{SEP}\n"
        "\n"
        "⚠️ Bạn cần hoàn thành 2 bước để nhận CODE:\n"
        "\n"
        "1️⃣ Xác minh tài khoản (5 giây)\n"
        "2️⃣ Tham gia nhóm\n"
        "\n"
        f"{SEP}\n"
        "\n"
        "👇 Bấm nút bên dưới để bắt đầu BƯỚC 1:"
    )


def numbered_links(links: Sequence[str]) -> str:
    """Danh sách link đánh số 1️⃣2️⃣3️⃣.

    Là **dữ liệu** chứ không phải câu chữ: nó đi vào biến `{invite_list}` của mẫu
    `tanthu.step2`, nên admin sửa mẫu trong `/suanoidung` vẫn không đổi được cách đánh số.
    Để ở đây vì `_keycap` là chi tiết riêng của module này.
    """
    return "\n".join(f"{_keycap(i)} {link}" for i, link in enumerate(links, start=1))


def tanthu_step2(invite_links: Sequence[str], fanpage_link: str) -> str:
    """Bước 2 — đã xác minh, liệt kê nhóm/kênh bắt buộc.

    `invite_links` lấy từ `required_chats` (is_active, theo `sort_order`); số nhóm là
    `len(invite_links)`, không viết cứng, vì admin thêm bớt nhóm bằng lệnh chứ không sửa
    code.
    """
    numbered = numbered_links(invite_links)
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        "✅ BƯỚC 1: ĐÃ XÁC MINH!\n"
        "\n"
        f"{SEP}\n"
        "📋 BƯỚC 2: THAM GIA NHÓM\n"
        f"{SEP}\n"
        "\n"
        "🎁 Để nhận CODE TÂN THỦ, bạn cần:\n"
        "\n"
        f"👉 Tham gia đủ {len(invite_links)} nhóm/kênh sau:\n"
        "\n"
        f"{numbered}\n"
        "\n"
        "👉 Like và nhắn tin cho fanpage:\n"
        f"{fanpage_link}\n"
        "\n"
        f"{SEP}\n"
        "\n"
        "✅ Sau khi tham gia đủ, bấm nút bên dưới để nhận CODE! 👇"
    )


def _keycap(index: int) -> str:
    return _KEYCAPS[index - 1] if index <= len(_KEYCAPS) else f"{index}."


def not_verified_retry_tanthu() -> str:
    """Callback `check_groups` khi người dùng chưa xác minh."""
    return (
        "⚠️ BẠN CHƯA XÁC MINH!\n"
        "\n"
        "Bạn cần xác minh tài khoản trước khi nhận CODE.\n"
        "\n"
        '👉 Bấm lại nút "🎁Code Tân Thủ" để bắt đầu.'
    )


def missing_groups(joined: int, total: int) -> str:
    """Chưa tham gia đủ nhóm.

    Bỏ câu "đợi 15 giây" của bản cũ: trạng thái lấy từ sự kiện `chat_member` nên cập nhật
    gần như tức thì, lời khuyên đó thành lời khuyên sai.
    """
    return (
        "❌ Bạn chưa tham gia đủ nhóm/kênh!\n"
        "\n"
        f"📊 Tiến độ: {joined}/{total}\n"
        "\n"
        "Hãy tham gia đủ các nhóm/kênh phía trên nhé!\n"
        "Sau đó bấm lại để nhận thưởng nhé! 🎁"
    )


def code_delivered(code_value: str, value_vnd: int) -> str:
    """Phát code tân thủ thành công."""
    return (
        "🎉 CHÚC MỪNG! 🎉\n"
        "\n"
        "✅ Bạn đã hoàn thành CẢ 2 BƯỚC!\n"
        "\n"
        f"{SEP}\n"
        f"💰 CODE TÂN THỦ: {code_value}\n"
        f"💵 Giá trị: {format_vnd(value_vnd)}đ\n"
        f"{SEP}\n"
        "\n"
        "Chúc bạn chơi may mắn! 🍀"
    )


def out_of_stock(support_link: str) -> str:
    """Hết kho code. Không có grant nào được tạo — không có gì để trừ."""
    return f"❌ CODE ĐÃ HẾT VUI LÒNG LIÊN HỆ CSKH ĐỂ ĐƯỢC HỖ TRỢ\n\n🔗 {support_link}"


# ── 13.2.4 · Mời bạn bè ─────────────────────────────────────────────


def countdown(remaining_seconds: int) -> str:
    """Đếm ngược của chiến dịch — công thức bắt buộc của §13.2.4.

    Tính trực tiếp trên số giây còn lại thay vì đọc `timedelta.days`/`.seconds` riêng lẻ:
    với timedelta âm thì `.days` âm còn `.seconds` **vẫn dương**, nên bot cũ hiện
    "Còn 0 ngày 13 giờ" vĩnh viễn cho một chiến dịch đã hết hạn từ tám tháng trước.
    """
    if remaining_seconds <= 0:
        return "⏰ Chiến dịch đã kết thúc"
    return f"Còn {remaining_seconds // 86400} ngày {(remaining_seconds % 86400) // 3600} giờ"


def campaign_block(
    *,
    interval: int,
    reward_value_vnd: int,
    remaining_seconds: int,
    max_claims: int,
    claimed: int,
    ref_link: str,
) -> str:
    """Khối chiến dịch — bản DUY NHẤT, dùng chung cho 13.2.4 và 13.2.14.

    §13.2.14 nói thẳng: "render bằng cùng một hàm, cùng một khoá settings, cùng công thức
    countdown — không được có bản sao thứ hai của đoạn văn đó".

    `claimed` phải là `count(*) FROM referral_rewards`, KHÔNG phải `qualified // interval`
    — hai con số lệch nhau ngay khi có một referral bị từ chối vì rủi ro.
    """
    return (
        "💎 PHẦN THƯỞNG:\n"
        "\n"
        f"Mời {interval} người = 1 code {value_label(reward_value_vnd)}\n"
        "\n"
        f"⏰ Thời gian: {countdown(remaining_seconds)}\n"
        "\n"
        f"🎯 Tối đa: {max_claims} lần/người (tối đa {interval * max_claims} người)\n"
        f"📊 Của bạn: Đã nhận {claimed}/{max_claims} lần\n"
        "\n"
        "🚀 CÁCH THAM GIA:\n"
        "\n"
        '1️⃣ Bấm nút "💎 Tham gia ngay" bên dưới\n'
        "\n"
        "2️⃣ Nhận bài viết có link riêng của bạn\n"
        "\n"
        "3️⃣ Chia sẻ cho bạn bè\n"
        "\n"
        "4️⃣ Tự động nhận code khi đủ người!\n"
        "\n"
        "⚠️ LƯU Ý:\n"
        "\n"
        "• Mỗi người CHỈ TÍNH 1 LẦN\n"
        "\n"
        "• Click link CỦA BẠN trước = Được tính ✅\n"
        "\n"
        f"• Mỗi {interval} người → Nhận code NGAY LẬP TỨC!\n"
        "\n"
        f"{SEP_WIDE}\n"
        "\n"
        "🔗 Link riêng của bạn:\n"
        f"{ref_link}"
    )


def referral_invite(
    *,
    interval: int,
    reward_value_vnd: int,
    remaining_seconds: int,
    max_claims: int,
    claimed: int,
    ref_link: str,
) -> str:
    """Màn hình nút `💎 Mời bạn nhận quà`."""
    block = campaign_block(
        interval=interval,
        reward_value_vnd=reward_value_vnd,
        remaining_seconds=remaining_seconds,
        max_claims=max_claims,
        claimed=claimed,
        ref_link=ref_link,
    )
    return f"🎁 CHIẾN DỊCH CHIA SẺ ĐANG CHẠY!\n\n{SEP_WIDE}\n\n{block}"


def referral_link(bot_username: str, user_id: int) -> str:
    """`ref_link` của một người.

    Tiền tố `ref_` thay cho `cref_575_` của bot cũ — con số 575 không mang nghĩa nghiệp vụ
    nào trong toàn bộ source cũ.
    """
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def share_post_intro() -> str:
    """Tin hướng dẫn đi trước bài viết chia sẻ (callback `join_referral`)."""
    return (
        "✅ BÀI VIẾT CHIA SẺ ĐÃ SẴN SÀNG!\n"
        "\n"
        "📤 Hãy CHUYỂN TIẾP (Forward) bài viết bên dưới cho bạn bè\n"
        "🔗 Hoặc chia sẻ vào nhóm Telegram\n"
        "\n"
        "💡 Càng nhiều người click link của bạn → Càng nhanh nhận code!"
    )


def share_post() -> str:
    """Caption bài viết chia sẻ (ảnh `banner_share`)."""
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        "🎉 TeleVip - TX / CL TẶNG BẠN CODE TÂN THỦ! 🎁\n"
        f"{SEP_SHARE}\n"
        "💰 Nhận ngay CODE miễn phí\n"
        "🎮 Chơi liền – Rút liền – Không cần nạp!\n"
        "\n"
        "💡 CÁCH NHẬN CODE:\n"
        "1️⃣ Bấm nút bên dưới\n"
        "2️⃣ Làm xác minh (5 giây)\n"
        "3️⃣ Nhận code NGAY!"
    )


def referral_milestone(
    *,
    tier_no: int,
    max_claims: int,
    qualified: int,
    code_value: str,
    value_vnd: int,
    interval: int,
) -> str:
    """Tin báo mốc mời bạn cho người giới thiệu."""
    return (
        f"🎉 CHÚC MỪNG! LẦN {tier_no}/{max_claims}!\n"
        "\n"
        f"💎 Bạn đã mời thành công {qualified} người!\n"
        "\n"
        f"🎁 Phần thưởng lần {tier_no}:\n"
        f"{SEP}\n"
        f"💰 Code: {code_value}\n"
        f"💵 Giá trị: {format_vnd(value_vnd)}đ\n"
        f"{SEP}\n"
        "\n"
        "✅ Code đã được gửi tự động!\n"
        f"🎯 Tiếp tục mời thêm {interval} người nữa để nhận code tiếp theo!"
    )


def referral_milestone_out_of_stock(
    *, qualified: int, tier_no: int, max_claims: int, support_link: str
) -> str:
    """Đạt mốc nhưng kho `moibanbe` rỗng.

    Tier **không** được đánh dấu đã phát, để lần chạy sau phát bù — nên câu chữ chỉ báo
    "tạm hết", không báo "đã nhận".
    """
    return (
        f"🎉 Bạn đã mời được {qualified} người! Mốc lần {tier_no}/{max_claims} đã đạt.\n"
        "\n"
        f"⚠️ Code tạm hết, vui lòng liên hệ CSKH: {support_link}"
    )


# ── 13.2.5 · Thống kê tài khoản ─────────────────────────────────────


def account_stats(
    *,
    total_refs: int,
    total_value_vnd: int,
    available_codes: int,
    used_codes: int,
    total_users: int,
    user_id: int,
    qualified_refs: int,
    rank_today: str,
    rank_alltime: str,
    total_codes_received: int,
    total_value_received_vnd: int,
    updated_at: datetime,
) -> str:
    """Caption màn hình `📊Thống Kê TK` (ảnh `banner_stats`).

    `rank_today`/`rank_alltime` nhận **chuỗi đã dựng sẵn** (số hạng hoặc `NO_RANK`) vì
    "chưa xếp hạng" không phải một con số. Bot cũ in cứng "Chưa có" ở cả hai dòng và
    không bao giờ tính hạng thật.
    """
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        "📊 THỐNG KÊ TỔNG QUAN HỆ THỐNG\n"
        "\n"
        f"🤝 Tổng số lượt mời: {format_vnd(total_refs)}\n"
        f"💰 Tổng số tiền code đã phát: {format_vnd(total_value_vnd)} VNĐ\n"
        f"🎟️ Tổng số code khả dụng: {format_vnd(available_codes)}\n"
        f"🎁 Tổng số code đã phát: {format_vnd(used_codes)}\n"
        f"👥 Tổng số người dùng: {format_vnd(total_users)}\n"
        "\n"
        f"{SEP_XWIDE}\n"
        "\n"
        "📈 THỐNG KÊ CÁ NHÂN CỦA BẠN\n"
        "\n"
        f"🆔 ID của bạn: {user_id}\n"
        f"👬 Số người bạn đã mời thành công: {qualified_refs} người\n"
        "\n"
        "🏆 HẠNG CỦA BẠN:\n"
        f"📅 Hạng hôm nay: {rank_today}\n"
        f"📆 Hạng toàn thời gian: {rank_alltime}\n"
        "\n"
        f"🎁 Tổng mã đã nhận: {total_codes_received}\n"
        f"💰 Tổng giá trị nhận được: {format_vnd(total_value_received_vnd)} VNĐ\n"
        "\n"
        f"{SEP_XWIDE}\n"
        "\n"
        f"⚡ Cập nhật lúc: {_vn_time(updated_at, with_seconds=True)}"
    )


# ── 13.2.6 · Bảng xếp hạng ──────────────────────────────────────────


def leaderboard(
    *,
    today: bool,
    top_referrers: Sequence[tuple[str, int]],
    top_receivers: Sequence[tuple[str, int]],
    top_streaks: Sequence[tuple[str, int]],
    updated_at: datetime,
) -> str:
    """Ba bảng xếp hạng, một chế độ.

    Mỗi mục là `(tên đã dựng bằng display_name, con số)`. Mục rỗng in `NO_DATA` mà không
    làm hỏng hai mục còn lại.
    """
    mode = "HÔM NAY" if today else "TOÀN THỜI GIAN"
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        f"👑 BẢNG XẾP HẠNG — {mode}\n"
        "\n"
        f"{_top_block('🏆 TOP MỜI BẠN BÈ', referrer_lines(top_referrers))}\n"
        "\n"
        f"{_top_block('💰 TOP NHẬN CODE', receiver_lines(top_receivers))}\n"
        "\n"
        f"{_top_block('🔥 TOP ĐIỂM DANH', streak_lines(top_streaks))}\n"
        "\n"
        f"{SEP}\n"
        "\n"
        f"⚡ Cập nhật: {_vn_time(updated_at, with_seconds=False)}\n"
        "\n"
        "💡 Tiếp tục điểm danh, mời bạn bè để lên TOP!"
    )


def _top_block(title: str, lines: str) -> str:
    return f"{SEP}\n{title}\n{SEP}\n{lines}"


def top_lines(entries: Sequence[tuple[str, int]], render_value: Callable[[int], str]) -> str:
    """Phần **thân** một bảng xếp hạng: huy chương + tên + dòng giá trị. Rỗng → `NO_DATA`.

    Tách khỏi `_top_block` vì `leaderboard.screen` giao cho admin sửa phần văn xuôi bao
    quanh, còn ba khối này đi vào template dưới dạng biến đã dựng sẵn
    (`{block_referrers}`…). Tầng gọi cần đúng chuỗi này, không cần cái tiêu đề.
    """
    if not entries:
        return NO_DATA
    return "\n".join(
        f"{_MEDALS[i - 1] if i <= len(_MEDALS) else f'{i}.'} {name}\n   {render_value(value)}"
        for i, (name, value) in enumerate(entries, start=1)
    )


def referrer_lines(entries: Sequence[tuple[str, int]]) -> str:
    """Khối `🏆 TOP MỜI BẠN BÈ` — `(tên, số người đã mời)`."""
    return top_lines(entries, lambda n: f"💎 {n} người")


def receiver_lines(entries: Sequence[tuple[str, int]]) -> str:
    """Khối `💰 TOP NHẬN CODE` — `(tên, tổng giá trị VNĐ)`."""
    return top_lines(entries, lambda n: f"💵 {format_vnd(n)}đ")


def streak_lines(entries: Sequence[tuple[str, int]]) -> str:
    """Khối `🔥 TOP ĐIỂM DANH` — `(tên, số ngày liên tiếp)`."""
    return top_lines(entries, lambda n: f"🔥 {n} ngày liên tiếp")


# ── 13.2.7 · Điểm danh ──────────────────────────────────────────────


def checkin_success(*, points: int, balance: int, streak: int, tiers: Sequence[int]) -> str:
    """Điểm danh thành công. Danh sách mệnh giá sinh từ `redeem.tiers`, không viết cứng."""
    return (
        "✅ ĐIỂM DANH THÀNH CÔNG!\n"
        "\n"
        f"🎁 Nhận được: +{format_vnd(points)}đ\n"
        f"💰 Số dư hiện tại: {format_vnd(balance)}đ\n"
        f"🔥 Chuỗi liên tiếp: {streak} ngày\n"
        "\n"
        f"💡 Mỗi ngày điểm danh nhận {format_vnd(points)}đ\n"
        "💡 Tích đủ điểm có thể đổi CODE:\n"
        f"{checkin_tier_lines(tiers)}\n"
        "\n"
        '👉 Bấm "🎁 Đổi CODE" để đổi điểm lấy code nhé!'
    )


def checkin_tier_lines(tiers: Sequence[int]) -> str:
    """Bậc cuối cùng được đánh dấu `(TỐI ĐA)` — nó là trần thật, không phải chú thích đẹp.

    Công khai vì `checkin.success` nhận khối này qua biến `{tier_lines}`: nó sinh trong
    một vòng lặp nên không diễn đạt được bằng template, và tầng gọi phải dựng sẵn.
    """
    last = len(tiers) - 1
    return "\n".join(
        f"   • {format_vnd(v)}đ = Code {value_label(v)}" + (" (TỐI ĐA)" if i == last else "")
        for i, v in enumerate(tiers)
    )


def checkin_already(*, balance: int, streak: int) -> str:
    """Đã điểm danh hôm nay (va PK `(user_id, business_date)`)."""
    return (
        "⚠️ BẠN ĐÃ ĐIỂM DANH HÔM NAY RỒI!\n"
        "\n"
        f"💰 Số dư hiện tại: {format_vnd(balance)}đ\n"
        f"🔥 Chuỗi liên tiếp: {streak} ngày\n"
        "\n"
        "⏰ Quay lại vào ngày mai để tiếp tục điểm danh nhé!\n"
        "\n"
        "💡 Có thể đổi CODE ngay nếu đủ điểm:\n"
        '👉 Bấm "🎁 Đổi CODE"'
    )


# ── 13.2.8 · Đổi điểm lấy code ──────────────────────────────────────


def redeem_tier_lines(*, balance: int, tiers: Sequence[int], points_per_day: int) -> str:
    """Một dòng cho MỖI bậc, đủ điểm hay chưa đủ đều hiện.

    Công khai cùng lý do với `checkin_tier_lines`: `redeem.menu` nhận khối này qua biến
    `{tier_lines}`. Số ngày là ước tính thô "còn thiếu / điểm mỗi ngày", làm tròn LÊN —
    nói "~2 ngày" cho thứ cần 2 ngày rưỡi là hứa sớm hơn thực tế.
    """
    lines = []
    for value in tiers:
        if balance >= value:
            lines.append(f"✅ Code {value_label(value)} ({format_vnd(value)}đ) - ĐỦ ĐIỂM")
        else:
            missing = value - balance
            days = math.ceil(missing / points_per_day) if points_per_day > 0 else 0
            lines.append(
                f"⏳ Code {value_label(value)} ({format_vnd(value)}đ)"
                f" - Còn thiếu {format_vnd(missing)}đ (~{days} ngày)"
            )
    return "\n".join(lines)


def redeem_menu(*, balance: int, tiers: Sequence[int], points_per_day: int) -> str:
    """Màn hình `🎁 Đổi CODE` — liệt kê **tất cả** bậc, đủ hay chưa đủ đều hiện."""
    body = (
        "🎁 ĐỔI ĐIỂM LẤY CODE\n"
        "\n"
        f"💰 Số dư của bạn: {format_vnd(balance)}đ\n"
        "\n"
        "📋 Bạn có thể chọn đổi ngay hoặc tiếp tục tích điểm:\n"
        "\n"
        f"{redeem_tier_lines(balance=balance, tiers=tiers, points_per_day=points_per_day)}\n"
        "\n"
        "💡 LƯU Ý:\n"
        "• Bạn có thể chọn đổi bất kỳ loại nào đủ điểm\n"
        "• Hoặc tiếp tục tích để đổi loại lớn hơn\n"
        f"• Điểm danh mỗi ngày: +{format_vnd(points_per_day)}đ\n"
        "\n"
    )
    if any(balance >= v for v in tiers):
        return body + "👇 Chọn loại code bạn muốn đổi:"
    return body + (
        '⚠️ Hiện tại bạn chưa đủ điểm đổi code nào.\n👉 Bấm "✅ Điểm Danh" để tích thêm điểm!'
    )


def redeem_success(*, code_value: str, value_vnd: int, balance: int) -> str:
    return (
        "🎉 ĐỔI CODE THÀNH CÔNG!\n"
        "\n"
        f"{SEP}\n"
        f"💰 CODE: {code_value}\n"
        f"💵 Giá trị: {format_vnd(value_vnd)}đ\n"
        f"{SEP}\n"
        "\n"
        f"💳 Đã trừ: -{format_vnd(value_vnd)}đ\n"
        f"💰 Số dư còn: {format_vnd(balance)}đ\n"
        "\n"
        "🎮 Sử dụng code ngay:"
    )


def redeem_out_of_stock(*, value_vnd: int, balance: int, support_link: str) -> str:
    """Hết kho mệnh giá này. Toàn bộ transaction rollback nên điểm KHÔNG bị trừ."""
    return (
        "❌ CODE ĐÃ HẾT!\n"
        "\n"
        f"Hiện tại code {format_vnd(value_vnd)}đ đã hết.\n"
        "\n"
        f"💰 Số dư của bạn: {format_vnd(balance)}đ\n"
        "\n"
        f"💡 Liên hệ: {support_link}"
    )


def redeem_not_enough(*, balance: int, value_vnd: int) -> str:
    return (
        "⚠️ KHÔNG ĐỦ ĐIỂM!\n"
        "\n"
        f"💰 Số dư của bạn: {format_vnd(balance)}đ\n"
        f"💎 Code cần đổi: {format_vnd(value_vnd)}đ\n"
        "\n"
        "💡 Điểm danh mỗi ngày để tích thêm điểm nhé!\n"
        '👉 Bấm "✅ Điểm Danh"'
    )


# ── 13.2.9 · Event đập hộp ──────────────────────────────────────────


def event_prize_lines(prize_table: Sequence[tuple[int, int]]) -> str:
    """Khối dòng tỉ lệ của caption event — **một dòng cho mỗi mức của bảng quay số**.

    Tách riêng khỏi `event_box_caption` để `services/event_box.py` dựng được đúng khối
    này cho biến `{prize_lines}` của khoá `event.box_caption` mà **không** gõ lại công
    thức dòng ở chỗ thứ hai. Một công thức, hai nơi gọi: caption mặc định trong code và
    caption admin đã sửa trong bảng đều in ra cùng những con số.
    """
    lines = []
    for value_vnd, weight_bp in prize_table:
        percent = _percent(weight_bp)
        if value_vnd == 0:
            lines.append(f"🤮 Hộp rỗng {percent}% 👻")
        else:
            lines.append(f"🎲 {value_vnd // 1000}k = 🎯 {percent}%")
    return "\n".join(lines)


def event_box_caption(prize_table: Sequence[tuple[int, int]], game_link: str) -> str:
    """Caption tin event, **render từ chính bảng tỉ lệ dùng để quay**.

    `prize_table` là `[(value_vnd, weight_bp), …]`, `value_vnd = 0` nghĩa là hộp rỗng,
    tổng trọng số bằng 10.000 basis point.

    Đây là chỗ sửa lỗi đắt nhất của bot cũ: nó có hai bảng, `EVENT_PROBABILITIES_DISPLAY`
    để in ra và `EVENT_PROBABILITIES_REAL` để quay. Người dùng đọc "88k = 1%" trong khi
    xác suất trúng 20k/50k/88k đều bằng 0. Nhận bảng qua tham số làm cho hai con số
    **không thể lệch nhau về mặt kiến trúc**.
    """
    return (
        "🌟✨ Giật code giờ vàng✨🌟\n"
        "😎 Test thử nhân phẩm ngay nào 🎲🍀\n"
        "\n"
        "📌 Xác suất mở hộp:\n"
        f"{event_prize_lines(prize_table)}\n"
        "\n"
        '😎 Min rút 50k Thoải mái "ăn non" – nói KHÔNG với vòng cược rườm rà! 🎲\n'
        f"🌐 Link Game: {game_link}"
    )


def _percent(weight_bp: int) -> str:
    """Basis point → phần trăm, bỏ số 0 thừa (`500` → `5`, `150` → `1.5`)."""
    value = weight_bp / 100
    return f"{value:g}"


def event_box_win(code_value: str, value_vnd: int) -> str:
    return (
        "🎉 CHÚC MỪNG! 🎉\n"
        "\n"
        "Bạn đã mở được:\n"
        f"🎁 {code_value}\n"
        f"💰 Giá trị: {format_vnd(value_vnd)} VNĐ\n"
        "\n"
        "Chúc bạn chơi may mắn! 🍀"
    )


def event_box_empty(game_link: str) -> str:
    """Hộp rỗng.

    Bỏ câu "CODE đã hết" của bản cũ — nó nói dối: rỗng là kết quả quay số, không phải hết
    kho, và câu đó đẩy người dùng sang CSKH hỏi "bao giờ có code" một cách vô ích.
    """
    return (
        "🤮 Hộp rỗng! 👻\n"
        "⁉️ Lượt này chưa trúng, hãy chờ lượt tiếp theo và MỞ QUÀ thật nhanh bạn nhé.\n"
        "\n"
        "🎮 Chơi game ngay:\n"
        f"{game_link}"
    )


def event_box_budget_capped(game_link: str) -> str:
    """Đợt đã chạm trần `settings.event.budget_cap_vnd`.

    §13.5.1 điểm 3: chạm trần là **điều duy nhất** được phép làm lệch tỉ lệ đã quảng cáo,
    nên nó phải hiện ra thành một câu khác hẳn hộp rỗng. Trả lại đúng câu "hộp rỗng" ở
    đây là nói dối: người dùng đọc tỉ lệ 22% trúng trong khi xác suất thật lúc đó bằng 0.
    """
    return (
        "📢 Đợt này đã phát hết phần quà! 🎁\n"
        "⁉️ Ngân sách của đợt đã dùng hết nên lượt mở này không còn cơ hội trúng.\n"
        "Hãy chờ đợt tiếp theo và mở thật nhanh bạn nhé.\n"
        "\n"
        "🎮 Chơi game ngay:\n"
        f"{game_link}"
    )


# ── 13.2.10 · Chiến dịch chia sẻ (nút 📢 EVENT) ──────────────────────


def share_event_default(
    *, bot_link: str, game_link: str, group_link: str, required_groups: int
) -> str:
    """Nội dung seed khi `share_event_config` chưa được admin cấu hình."""
    return (
        "✨ NHẬN CODE TELEVIP SIÊU NHANH ✨\n"
        "\n"
        "💥 Chia sẻ càng mạnh – Nhận code càng khủng!\n"
        "\n"
        f"🔥 Chỉ cần mở bot 👉 {bot_link}\n"
        "Là có ngay cơ hội:\n"
        "\n"
        "💰 Săn CODE vàng, CODE lộc mỗi ngày\n"
        "🎁 Giao diện xịn, thao tác nhanh – chia sẻ là có quà!\n"
        "\n"
        "📢 TELEVIP game tài xỉu, chẵn lẻ telegram, nạp rút siêu tốc, min rút 50k không vòng cược rườm rà\n"
        "\n"
        f"{SEP_DASH}\n"
        "\n"
        f"🎰 Chơi game tại: {game_link}\n"
        f"📱 Group Cộng Đồng: {group_link}\n"
        "\n"
        "💡 CÁCH THAM GIA:\n"
        f"1️⃣ Chia sẻ bài viết này vào {required_groups} nhóm\n"
        '2️⃣ Bấm "✅ Đã Chia Sẻ"\n'
        "3️⃣ GỬI LINK BÀI VIẾT CHO CSKH để xác minh\n"
        "4️⃣ Nhận CODE TỪ CSKH"
    )


# ── 13.2.11 · Hỗ trợ ────────────────────────────────────────────────


def support(support_link: str) -> str:
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        "👉 Chào bạn 🌸, để được hỗ trợ bạn vui lòng bấm vào link sau để gặp hỗ trợ viên nhé 🤝✨\n"
        "🙏 Cám ơn bạn! 💖\n"
        f"🔗 {support_link}"
    )


# ── 13.2.12 · Chơi game ─────────────────────────────────────────────


def play_game() -> str:
    return "🎮 Chơi Game 🎮\n\nBấm nút bên dưới để chơi ngay!"


# ── 13.2.13 · /help ─────────────────────────────────────────────────


def help_text(*, interval: int, reward_value_vnd: int, max_claims: int) -> str:
    """Hướng dẫn. Mọi con số sinh từ `settings`, không viết cứng.

    `main.py:1344-1347` của bot cũ liệt kê thang mốc 5/15/30/…/500 — thang đó **không
    phải luật đang chạy**, nó chỉ là chuỗi gõ tay không ai cập nhật.
    """
    return (
        "🎁TELEVIP - TẶNG CODE 24/7🎲\n"
        "\n"
        "📖 HƯỚNG DẪN SỬ DỤNG BOT\n"
        "\n"
        "🎁 CÁCH NHẬN CODE:\n"
        '1. Bấm "🎁 Mở Quà Ngay 🎁"\n'
        "2. Xác minh tài khoản (5 giây)\n"
        "3. Tham gia đủ nhóm\n"
        "4. Nhận code ngay lập tức!\n"
        "\n"
        "💎 MỜI BẠN BÈ:\n"
        '• Bấm "💎 Mời bạn nhận quà"\n'
        "• Copy link và chia sẻ\n"
        f"• Mời {interval} người = code {value_label(reward_value_vnd)}\n"
        f"• Tối đa {max_claims} lần ({interval * max_claims} người)\n"
        "\n"
        "🎮 CHƠI GAME:\n"
        '• Bấm "🎮 Chơi Game 🎮"\n'
        "\n"
        "📊 XEM THỐNG KÊ:\n"
        '• Bấm "📊Thống Kê TK"\n'
        "\n"
        "❓ HỖ TRỢ:\n"
        '• Bấm "💁‍♀️ Hỗ Trợ – CSKH"\n'
        "\n"
        "🎁 TeleVip - Nơi Mọi Điều Có Thể!"
    )


# ── 13.2.14 · Check chia sẻ ─────────────────────────────────────────


def share_progress(
    *,
    qualified: int,
    claimed: int,
    total_vnd: int,
    interval: int,
    max_claims: int,
    reward_value_vnd: int,
    remaining_seconds: int,
    ref_link: str,
) -> str:
    """Màn hình `📈Check Chia sẻ` — chỉ đọc, không ghi gì vào DB.

    Ba con số phải đọc trong **một** transaction chỉ-đọc ở tầng gọi, nếu không màn hình
    tự mâu thuẫn với chính nó. `total_vnd` là tổng thật trong `referral_rewards`, không
    phải `claimed × reward_value_vnd` — hai cách chỉ bằng nhau chừng nào mệnh giá chưa
    bao giờ đổi, mà mệnh giá thì đổi được bằng `/setcauhinh`.
    """
    block = campaign_block(
        interval=interval,
        reward_value_vnd=reward_value_vnd,
        remaining_seconds=remaining_seconds,
        max_claims=max_claims,
        claimed=claimed,
        ref_link=ref_link,
    )
    return (
        "📈 TIẾN ĐỘ CHIA SẺ CỦA BẠN\n"
        "\n"
        f"{SEP_WIDE}\n"
        "\n"
        f"👥 Đã mời thành công: {qualified} người\n"
        f"🎁 Đã nhận: {claimed}/{max_claims} lần\n"
        f"💵 Tổng giá trị đã nhận: {format_vnd(total_vnd)}đ\n"
        "\n"
        f"{share_status_line(qualified=qualified, claimed=claimed, interval=interval, max_claims=max_claims)}\n"
        "\n"
        f"{SEP_WIDE}\n"
        "\n"
        f"{block}"
    )


def share_status_line(*, qualified: int, claimed: int, interval: int, max_claims: int) -> str:
    """Dòng trạng thái của §13.2.14 — đúng **ba** trạng thái, không có trạng thái thứ tư.

    Trạng thái 3 (`đủ điều kiện mà chưa có reward`) là một **tín hiệu bất thường**, không
    phải trạng thái bình thường: 13.2.4 phát bù mọi tier ngay trong transaction xác minh,
    nên rơi vào đây chỉ có thể do kho `moibanbe` rỗng hoặc grant còn `reserved`. Vì thế
    kèm luôn dòng "đang được xử lý" — bản cũ để trạng thái này đứng vĩnh viễn.
    """
    if claimed >= max_claims:
        return f"✅ ĐÃ ĐẠT TỐI ĐA ({max_claims}/{max_claims} lần)"
    next_tier = claimed + 1
    next_milestone = next_tier * interval
    people_needed = max(0, next_milestone - qualified)
    if people_needed > 0:
        return (
            f"⏳ Còn {people_needed} người nữa để nhận lần thứ {next_tier}\n"
            f"🎯 Mục tiêu kế tiếp: {next_milestone} người"
        )
    return (
        f"🎁 ĐỦ ĐIỀU KIỆN nhận lần thứ {next_tier}!\n"
        "✅ Code sẽ được gửi TỰ ĐỘNG, bạn không cần làm gì thêm.\n"
        "⏳ Code đang được xử lý, sẽ tới trong ít phút."
    )


# ── Lỗi chung ───────────────────────────────────────────────────────


def rate_limited(seconds: float) -> str:
    """Bấm quá nhanh.

    §13.2 không đặc tả câu chữ cho nhánh này (bot cũ im lặng nuốt), nên chuỗi ở đây là
    **tự định nghĩa**. Làm tròn lên để không bao giờ hứa "0 giây" rồi vẫn chặn.
    """
    wait = max(1, math.ceil(seconds))
    return f"⏳ Bạn bấm hơi nhanh, vui lòng chờ {wait} giây rồi thử lại nhé!"


def system_busy() -> str:
    """Hạ tầng lỗi (nhóm bắt buộc không `healthy`, DB timeout…).

    Không được để người thật gánh lỗi cấu hình: câu này nói "thử lại sau", không đổ lỗi
    cho người dùng và không tiết lộ chi tiết kỹ thuật.
    """
    return "⚠️ Hệ thống đang bận, vui lòng thử lại sau ít phút nhé!"


# ── Hướng dẫn vận hành (/huongdan) ──────────────────────────────────
#
# Ba phần dưới đây là câu chữ cho ADMIN, không phải cho người dùng, nên chúng cố ý không
# nằm chung khối với `help_text()`. Vẫn để ở đây — và vẫn có khoá câu chữ — vì đây đúng
# là loại nội dung thay đổi theo cách vận hành chứ không theo code: quy ước đặt tên lô
# code, giờ bắn tin, ai được gọi khi hết kho. Sửa những thứ đó không nên cần một lần deploy.


def guide_codes() -> str:
    """Phần 1 — kho code."""
    return (
        "📖 HƯỚNG DẪN VẬN HÀNH (1/3) — KHO CODE\n"
        "\n"
        f"{SEP}\n"
        "\n"
        "📦 NẠP CODE\n"
        "/add_giffcode <loại> <mệnh giá> MA1 MA2 MA3\n"
        "Ví dụ: /add_giffcode tanthu 10k TV001 TV002\n"
        "\n"
        "• Mã trùng bị BỎ QUA, không ghi đè — nạp lại cùng một file là an toàn.\n"
        "• Lô vượt ngưỡng duyệt hai người sẽ bị TỪ CHỐI; chia nhỏ lô rồi nạp lại.\n"
        "\n"
        "📊 XEM KHO\n"
        "/tonkho        tồn theo từng loại × từng mệnh giá + dự báo còn đủ mấy ngày\n"
        "/codes         20 mã chưa dùng gần nhất\n"
        "/codes used    20 mã đã phát gần nhất, kèm ai nhận và lúc nào\n"
        "\n"
        "🗑 THU HỒI\n"
        "/del_code MA123              thu hồi MỘT mã chưa phát\n"
        "/del_all_code <loại> [giá]   thu hồi TOÀN BỘ mã chưa phát của một loại\n"
        "\n"
        "⚠️ Mã ĐÃ PHÁT không thu hồi được bằng hai lệnh trên. Đó không phải giới hạn kỹ\n"
        "thuật mà là chủ ý: mã đã vào sổ cái thì gỡ nó là một bút toán ngược, và xoá thẳng\n"
        "hàng dữ liệu sẽ làm mất bằng chứng ai đã nhận gì.\n"
        "\n"
        "🔁 PHÁT BÙ\n"
        "/resend_tanthu [@user|id]\n"
        "Gửi lại ĐÚNG mã đã cấp cho người đủ điều kiện nhưng lúc đó kho rỗng. Lệnh này\n"
        "KHÔNG cấp mã mới — nếu nó cấp mã mới thì một người sẽ ôm nhiều mã."
    )


def guide_broadcast() -> str:
    """Phần 2 — bắn tin và event."""
    return (
        "📖 HƯỚNG DẪN VẬN HÀNH (2/3) — BẮN TIN & EVENT\n"
        "\n"
        f"{SEP}\n"
        "\n"
        "📣 BẮN TIN\n"
        "/broadcast <nội dung>          tệp 30 ngày (mặc định)\n"
        "/broadcast <nội dung> --all    TOÀN BỘ người dùng\n"
        "\n"
        "• Gõ lệnh KHÔNG gửi gì cả. Bạn sẽ thấy bản xem thử + số đích thật, rồi phải bấm\n"
        "  xác nhận. Đây là cái chặn giữa một lần gõ nhầm và vài chục nghìn tin nhắn.\n"
        "• Xuống dòng trong nội dung được giữ nguyên.\n"
        "\n"
        "/broadcast_status <job_id>   tiến độ\n"
        "/broadcast_pause <job_id>    tạm dừng\n"
        "/broadcast_resume <job_id>   chạy tiếp — tiếp TỪ CHỖ DỪNG, không gửi lại từ đầu\n"
        "\n"
        "🎁 EVENT ĐẬP HỘP\n"
        "/send_event [lời dẫn] [--all]\n"
        "\n"
        "• Khối tỉ lệ LUÔN sinh từ settings.event.prize_table — không gõ tay được bộ số\n"
        "  khác vào tin gửi đi.\n"
        "• Thiếu mệnh giá trong kho thì lệnh TỪ CHỐI CHẠY. Quảng cáo giải 88K khi kho 88K\n"
        "  rỗng là hứa suông với người dùng.\n"
        "• Cửa sổ trúng thưởng tính từ lúc TỪNG người nhận được tin, không từ lúc bấm nút.\n"
        "\n"
        "📤 EVENT CHIA SẺ\n"
        "/update_share_event   reply một ảnh kèm caption để đặt bài chia sẻ\n"
        "/show_share_event     xem đúng thứ người dùng đang thấy\n"
        "/done_event <@user>   trao code sau khi đã xác minh bài share"
    )


def guide_config() -> str:
    """Phần 3 — cấu hình, quyền, và việc phải làm khi có sự cố."""
    return (
        "📖 HƯỚNG DẪN VẬN HÀNH (3/3) — CẤU HÌNH & SỰ CỐ\n"
        "\n"
        f"{SEP}\n"
        "\n"
        "⚙️ THAM SỐ\n"
        "/cauhinh [tiền_tố]        xem tham số đang chạy\n"
        "/setcauhinh <khoá> <giá>  đổi NÓNG, không cần restart\n"
        "\n"
        "Mỗi lần đổi để lại dấu vết ai đổi, lúc nào, từ giá trị nào sang giá trị nào.\n"
        "Không sửa file trên máy chủ — sửa ở đó thì không ai trả lời được câu hỏi trên.\n"
        "\n"
        "✍️ CÂU CHỮ\n"
        "/noidung [tiền_tố]        danh sách tin nhắn sửa được\n"
        "/xemnoidung <khoá>        xem bản đang chạy + biến bắt buộc\n"
        "/suanoidung <khoá> <nội dung>\n"
        "/resetnoidung <khoá>      về bản mặc định\n"
        "\n"
        # `{{…}}` là ngoặc thoát của `str.format`: chuỗi này là MẪU đi qua `text_service`,
        # nên một cặp ngoặc đơn ở đây sẽ bị đọc thành biến bắt buộc tên `code_value`.
        "⚠️ Thiếu một biến bắt buộc thì lệnh sửa BỊ TỪ CHỐI và nội dung không đổi. Xoá\n"
        "{{code_value}} khỏi tin trả code nghĩa là người dùng nhận một tin không có mã.\n"
        "\n"
        "👮 QUYỀN\n"
        "/admin_add <id> <owner|admin|cskh|ketoan>\n"
        "/admin_del <id>\n"
        "/help_admin    các lệnh mà CHÍNH bạn được phép dùng\n"
        "\n"
        "🚨 KHI CÓ SỰ CỐ\n"
        "• Hết code: bot tự cảnh báo vào nhóm admin. Nạp bằng /add_giffcode rồi\n"
        "  /resend_tanthu để phát bù cho người đã xếp hàng.\n"
        "• Bắn nhầm: /broadcast_pause NGAY. Đợt dừng tại chỗ, không mất tiến độ.\n"
        "• Nghi gian lận: /user <id> xem hồ sơ, /ban <id> <lý do> để khoá.\n"
        "  KHÔNG xoá tài khoản — xoá là mất luôn bằng chứng điều tra."
    )
