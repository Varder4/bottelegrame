"""Câu chữ gửi cho người dùng, đọc từ database — hiện thực của nguyên tắc N2 cho VĂN BẢN.

`settings_service` làm điều này cho **con số**; module này làm đúng như vậy cho **chuỗi**.
Chủ bot sửa một câu bằng `/suanoidung`, có hiệu lực trong 60 giây, không deploy, không
restart tiến trình nào.

## Ba tầng của một câu chữ

1. `domain/texts.py` — bản **mặc định**, nằm trong code. Vẫn là nơi câu chữ được viết ra
   lần đầu và vẫn là nguồn sự thật của bố cục.
2. `TEMPLATES` ở file này — bản mặc định đó **dịch sang dạng template**. Nó KHÔNG được gõ
   lại bằng tay: mỗi template dựng lúc import bằng cách gọi chính hàm trong `texts.py` với
   giá trị mồi rồi thay giá trị đó bằng `{tên_biến}` (xem `_tmpl`). Gõ lại 50 đoạn văn
   tiếng Việt là tạo ra nguồn sự thật thứ hai, và hai nguồn thì sớm muộn cũng nói khác nhau.
3. Bảng `message_templates` — bản **admin đang chạy**. Thiếu dòng thì rơi về tầng 2.

Tầng 3 thiếu là chuyện bình thường, không phải sự cố: đó chính là thứ giữ cho bot **không
bao giờ câm**. Mất một dòng trong bảng, mất cả kết nối database, hay khoá chưa từng được
seed — người dùng vẫn nhận đúng câu mặc định trong code.

## Hàng rào biến

Hàng rào quan trọng nhất của module nằm ở `set_content`, không nằm ở `render`. Admin sửa
tin trả code mà xoá mất `{code_value}` thì tin vẫn gửi đi bình thường, chỉ là **không có
mã trong đó** — và không ai biết cho tới khi có người khiếu nại. Vì vậy nội dung mới bị
kiểm ba lớp trước khi ghi:

* thiếu biến bắt buộc  → từ chối, nêu đích danh biến nào thiếu;
* có biến lạ           → từ chối, vì `str.format` sẽ ném `KeyError` giữa đường nóng;
* thử render với bộ giá trị mẫu → từ chối nếu ném (ngoặc lệch, format-spec sai kiểu).

## Định dạng tiền

Một format-spec riêng: `{value_vnd:vnd}` gọi `texts.format_vnd` (`10000` → `10.000`). Nhờ
nó, tầng gọi truyền **số nguyên** đúng như khi gọi thẳng hàm trong `texts.py`, và quy ước
dấu chấm hàng nghìn không bị sao chép vào 20 chỗ khác nhau.

## Cái KHÔNG nằm trong bảng này

`format_vnd`, `value_label`, `display_name`, `referral_link` và các hàm `_`-riêng của
`texts.py` không phải câu chữ mà là **quy tắc dựng chuỗi** (định dạng số, sinh URL, đánh
số thứ tự, tô huy chương). Đưa chúng cho admin sửa nghĩa là cho phép sửa cách hệ thống
đọc một con số — hỏng ở đó không hiện ra thành một câu xấu mà thành một số tiền sai.

Từng dòng của danh sách (dòng giải thưởng event, dòng bậc đổi điểm, dòng bảng xếp hạng,
dòng nhóm phải vào) cũng vậy: chúng sinh trong vòng lặp và đi vào template dưới dạng một
biến khối đã dựng sẵn (`{prize_lines}`, `{tier_lines}`, `{invite_list}`…). Phần văn xuôi
bao quanh chúng thì sửa được bình thường.
"""

from __future__ import annotations

import asyncio
import string
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import VN_TZ
from televip.core.errors import TelevipError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.db.models.delivery import MessageTemplate, MessageTemplateAudit
from televip.domain import texts

log = get_logger(__name__)

#: Tuổi thọ bản sao trong RAM. Cùng con số với `settings_service` và cùng lý do: câu chữ
#: bị đọc ở gần như mọi lượt bấm nút, và trễ 60 giây khi đổi một câu là cái giá rẻ để
#: không phải dựng pub/sub giữa các worker.
CACHE_TTL_SECONDS: Final[float] = 60.0


class TemplateError(TelevipError):
    """Khoá câu chữ không tồn tại, hoặc nội dung mới không hợp lệ.

    Định nghĩa tại chỗ thay vì thêm vào `core/errors.py`: lỗi này chỉ có nghĩa với đường
    ghi của chính module này, và tầng handler bắt nó ngay tại nơi gọi.
    """


# ══════════════════════════════════════════════════════════════════════════════
# Format-spec riêng
# ══════════════════════════════════════════════════════════════════════════════


class _TemplateFormatter(string.Formatter):
    """`str.format` cộng đúng **một** spec riêng: `:vnd`.

    Không mở rộng thêm spec nào khác một cách tuỳ tiện — mỗi spec là một luật hiển thị mà
    admin phải nhớ, và luật nào không được dùng ở ít nhất một khoá thật thì chỉ là một
    cách nữa để gõ sai.
    """

    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec == "vnd":
            return texts.format_vnd(int(value))
        return super().format_field(value, format_spec)


_FORMATTER: Final = _TemplateFormatter()


def format_template(content: str, values: Mapping[str, Any]) -> str:
    """Thay biến trong `content`. Ném đúng những lỗi mà `str.format` ném."""
    return _FORMATTER.vformat(content, (), dict(values))


def template_vars(content: str) -> tuple[str, ...]:
    """Tên các biến xuất hiện trong nội dung, giữ thứ tự, không lặp.

    Ném:
        TemplateError: ngoặc `{`/`}` không cân — `str.format` sẽ ném ở đúng chỗ đó lúc chạy.
    """
    names: list[str] = []
    try:
        fields = list(_FORMATTER.parse(content))
    except ValueError as exc:
        raise TemplateError(f"nội dung có dấu ngoặc không cân: {exc}") from exc

    for _literal, field_name, _spec, _conv in fields:
        if field_name is None:
            continue
        # `{a.b}` / `{a[0]}` vẫn quy về biến `a` — đó là cái tầng gọi phải truyền vào.
        base = field_name.split(".")[0].split("[")[0].strip()
        if base and base not in names:
            names.append(base)
    return tuple(names)


# ══════════════════════════════════════════════════════════════════════════════
# Đăng ký khoá — bản mặc định dựng TỪ `domain/texts.py`
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """Một khoá câu chữ: nội dung mặc định + bộ giá trị mẫu.

    `sample` giữ hai vai cùng lúc, cố ý: nó vừa là **danh sách biến bắt buộc** (theo đúng
    thứ tự khai báo) vừa là bộ giá trị để xem thử và để thử render lúc kiểm nội dung mới.
    Tách làm hai chỗ thì sớm muộn có khoá khai biến mà không có giá trị mẫu, và lớp kiểm
    mạnh nhất — thử render thật — im lặng bỏ qua đúng khoá đó.
    """

    key: str
    label_vi: str
    content: str
    sample: Mapping[str, Any]

    @property
    def required_vars(self) -> tuple[str, ...]:
        return tuple(self.sample)


def _tmpl(rendered: str, *pairs: tuple[str, str]) -> str:
    """Chuỗi đã render + các cặp (giá trị mồi → `{biến}`) ⇒ template.

    Escape `{`/`}` **trước** khi cắm placeholder: một câu chữ có ngoặc nhọn thật sẽ làm
    `str.format` hiểu nhầm đó là tên biến.

    Ném:
        RuntimeError: giá trị mồi không có trong chuỗi đã render — nghĩa là `texts.py` vừa
            đổi và template này không còn dựng đúng nữa. Nổ lúc **import**, tức lúc chạy
            test, chứ không phải lúc có người bấm nút.
    """
    out = rendered.replace("{", "{{").replace("}", "}}")
    for literal, placeholder in pairs:
        if literal not in out:
            raise RuntimeError(
                f"không dựng được template: không tìm thấy {literal!r} trong chuỗi do "
                f"domain/texts.py sinh ra (đoạn đầu: {out[:80]!r})"
            )
        out = out.replace(literal, placeholder)
    return out


# ── Giá trị mồi ─────────────────────────────────────────────────────
# Cố ý xấu và độc nhất: chúng chỉ tồn tại để `_tmpl` tìm và thay, nên không được trùng với
# bất kỳ đoạn nào khác trong cùng câu chữ.

_SUPPORT = "«support_link»"
_GAME = "«game_link»"
_FANPAGE = "«fanpage_link»"
_BOT = "«bot_link»"
_GROUP = "«group_link»"
_REF = "«ref_link»"
_CODE = "«code_value»"
_INVITE = "«invite_link»"

#: `remaining_seconds` cho ra đúng "Còn 3 ngày 7 giờ".
_SECONDS_3D7H: Final = 3 * 86_400 + 7 * 3_600
_COUNTDOWN = texts.countdown(_SECONDS_3D7H)

_STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=VN_TZ)
_STAMP_SECONDS = "03:04:05 02/01/2026"
_STAMP_MINUTES = "03:04 02/01/2026"

_CAMPAIGN_ARGS: Final[dict[str, Any]] = {
    "interval": 111,
    "reward_value_vnd": 1_234_000,
    "remaining_seconds": _SECONDS_3D7H,
    "max_claims": 222,
    "claimed": 333,
    "ref_link": _REF,
}
_CAMPAIGN_BLOCK = texts.campaign_block(**_CAMPAIGN_ARGS)

_PROGRESS_ARGS: Final[dict[str, Any]] = {
    "qualified": 111,
    "claimed": 222,
    "total_vnd": 3_333_333,
    "interval": 444,
    "max_claims": 555,
    "reward_value_vnd": 6_660_000,
    "remaining_seconds": _SECONDS_3D7H,
    "ref_link": _REF,
}
_PROGRESS_BLOCK = texts.campaign_block(
    interval=444,
    reward_value_vnd=6_660_000,
    remaining_seconds=_SECONDS_3D7H,
    max_claims=555,
    claimed=222,
    ref_link=_REF,
)
_PROGRESS_STATUS = texts.share_status_line(qualified=111, claimed=222, interval=444, max_claims=555)

#: Hai câu kết của `redeem_menu` là hai nhánh của một `if`, nên chúng là hai khoá riêng —
#: một template không diễn đạt được một nhánh rẽ. Cắt ra khỏi chuỗi đã render chứ không gõ
#: lại: `texts.py` đổi câu thì `_menu_footer` nổ ngay lúc import.
_REDEEM_READY = texts.redeem_menu(balance=2_222_222, tiers=[2_222_222], points_per_day=333)
_REDEEM_NOT_READY = texts.redeem_menu(balance=1_111_111, tiers=[2_222_222], points_per_day=333)

#: Dòng cuối của phần thân `redeem_menu`; mọi thứ sau nó là câu kết của nhánh.
_MENU_BODY_END = f"• Điểm danh mỗi ngày: +{texts.format_vnd(333)}đ\n\n"


def _menu_footer(rendered: str) -> str:
    _, marker, footer = rendered.partition(_MENU_BODY_END)
    if not marker or not footer:
        raise RuntimeError("redeem_menu đã đổi bố cục — không cắt được câu kết của hai nhánh")
    return footer


_FOOTER_READY = _menu_footer(_REDEEM_READY)
_FOOTER_NOT_READY = _menu_footer(_REDEEM_NOT_READY)

_REDEEM_TIER_LINE = (
    f"⏳ Code {texts.value_label(2_222_222)} ({texts.format_vnd(2_222_222)}đ)"
    f" - Còn thiếu {texts.format_vnd(1_111_111)}đ (~3337 ngày)"
)

_CHECKIN_TIER_LINE = f"   • {texts.format_vnd(1_000_000)}đ = Code {texts.value_label(1_000_000)}"

_PRIZE_LINES = "🤮 Hộp rỗng 12.34% 👻\n🎲 5k = 🎯 87.66%"


def _spec(key: str, label_vi: str, content: str, **sample: Any) -> TemplateSpec:
    return TemplateSpec(key=key, label_vi=label_vi, content=content, sample=sample)


_SPECS: Final[tuple[TemplateSpec, ...]] = (
    # ── Alert của answerCallbackQuery ───────────────────────────────
    _spec("alert.already_verified", "Alert: đã xác thực rồi", texts.ALERT_ALREADY_VERIFIED),
    _spec("alert.need_verify", "Alert: cần xác thực trước", texts.ALERT_NEED_VERIFY),
    _spec("alert.checking", "Alert: đang kiểm tra", texts.ALERT_CHECKING),
    _spec(
        "alert.loading_share_post", "Alert: đang tải bài chia sẻ", texts.ALERT_LOADING_SHARE_POST
    ),
    _spec(
        "alert.already_joined_event", "Alert: đã tham gia event", texts.ALERT_ALREADY_JOINED_EVENT
    ),
    _spec("alert.box_empty", "Alert: hộp rỗng", texts.ALERT_BOX_EMPTY),
    _spec("alert.redeem_ok", "Alert: đổi code thành công", texts.ALERT_REDEEM_OK),
    _spec("alert.out_of_stock", "Alert: hết code", texts.ALERT_OUT_OF_STOCK),
    _spec("alert.not_enough_points", "Alert: không đủ điểm", texts.ALERT_NOT_ENOUGH_POINTS),
    # ── /start ──────────────────────────────────────────────────────
    _spec("start.welcome", "Lời chào /start (tin 1)", texts.start_welcome()),
    _spec("start.gift_teaser", "Mời mở quà /start (tin 2)", texts.start_gift_teaser()),
    # ── Xác minh ────────────────────────────────────────────────────
    _spec("verify.not_verified", "Cổng chặn: chưa xác thực", texts.not_verified()),
    _spec("verify.already_verified", "Đã xác thực rồi", texts.already_verified()),
    _spec("verify.success", "Xác minh thành công", texts.verify_success()),
    _spec("verify.session_expired", "Phiên xác minh hết hạn", texts.verify_session_expired()),
    _spec(
        "verify.rejected",
        "Từ chối xác minh (rủi ro)",
        _tmpl(texts.verify_rejected(_SUPPORT), (_SUPPORT, "{support_link}")),
        support_link="https://t.me/cskh_televip",
    ),
    # ── Code tân thủ ────────────────────────────────────────────────
    _spec(
        "tanthu.already_claimed",
        "Đã nhận code tân thủ rồi",
        _tmpl(texts.tanthu_already_claimed(_GAME), (_GAME, "{game_link}")),
        game_link="https://televip.game",
    ),
    _spec("tanthu.step1", "Code tân thủ — bước 1", texts.tanthu_step1()),
    _spec(
        "tanthu.step2",
        "Code tân thủ — bước 2 (vào nhóm)",
        _tmpl(
            texts.tanthu_step2([_INVITE], _FANPAGE),
            ("👉 Tham gia đủ 1 nhóm/kênh sau:", "👉 Tham gia đủ {total} nhóm/kênh sau:"),
            (f"1️⃣ {_INVITE}", "{invite_list}"),
            (_FANPAGE, "{fanpage_link}"),
        ),
        total=2,
        invite_list="1️⃣ https://t.me/+aaaaa\n2️⃣ https://t.me/+bbbbb",
        fanpage_link="https://facebook.com/televip",
    ),
    _spec(
        "tanthu.not_verified_retry",
        "Bấm nhận code khi chưa xác minh",
        texts.not_verified_retry_tanthu(),
    ),
    _spec(
        "tanthu.missing_groups",
        "Chưa vào đủ nhóm",
        _tmpl(texts.missing_groups(111, 222), ("111", "{joined}"), ("222", "{total}")),
        joined=1,
        total=2,
    ),
    _spec(
        "code.delivered",
        "Trả code tân thủ",
        _tmpl(
            texts.code_delivered(_CODE, 1_234_567),
            ("1.234.567", "{value_vnd:vnd}"),
            (_CODE, "{code_value}"),
        ),
        code_value="HK79-ABC123",
        value_vnd=10_000,
    ),
    _spec(
        "code.out_of_stock",
        "Hết kho code",
        _tmpl(texts.out_of_stock(_SUPPORT), (_SUPPORT, "{support_link}")),
        support_link="https://t.me/cskh_televip",
    ),
    # ── Mời bạn bè ──────────────────────────────────────────────────
    _spec(
        "referral.countdown_running",
        "Đếm ngược chiến dịch — còn hạn",
        _tmpl(_COUNTDOWN, ("Còn 3 ngày 7 giờ", "Còn {days} ngày {hours} giờ")),
        days=3,
        hours=7,
    ),
    _spec(
        "referral.countdown_ended",
        "Đếm ngược chiến dịch — đã kết thúc",
        texts.countdown(0),
    ),
    _spec(
        "referral.campaign_block",
        "Khối mô tả chiến dịch mời bạn",
        _tmpl(
            _CAMPAIGN_BLOCK,
            ("Còn 3 ngày 7 giờ", "{countdown}"),
            ("1234K", "{reward_label}"),
            ("24642", "{max_people}"),
            ("111", "{interval}"),
            ("222", "{max_claims}"),
            ("333", "{claimed}"),
            (_REF, "{ref_link}"),
        ),
        countdown="Còn 3 ngày 7 giờ",
        reward_label="10K",
        max_people=50,
        interval=5,
        max_claims=10,
        claimed=2,
        ref_link="https://t.me/televip_bot?start=ref_1",
    ),
    _spec(
        "referral.invite",
        "Màn hình 💎 Mời bạn nhận quà",
        _tmpl(
            texts.referral_invite(**_CAMPAIGN_ARGS),
            (_CAMPAIGN_BLOCK, "{campaign_block}"),
        ),
        campaign_block="💎 PHẦN THƯỞNG: …",
    ),
    _spec("referral.share_post_intro", "Hướng dẫn trước bài chia sẻ", texts.share_post_intro()),
    _spec("referral.share_post", "Caption bài viết chia sẻ", texts.share_post()),
    _spec(
        "referral.milestone",
        "Đạt mốc mời bạn — trả code",
        _tmpl(
            texts.referral_milestone(
                tier_no=111,
                max_claims=222,
                qualified=333,
                code_value=_CODE,
                value_vnd=1_234_567,
                interval=444,
            ),
            ("1.234.567", "{value_vnd:vnd}"),
            (_CODE, "{code_value}"),
            ("111", "{tier_no}"),
            ("222", "{max_claims}"),
            ("333", "{qualified}"),
            ("444", "{interval}"),
        ),
        tier_no=1,
        max_claims=10,
        qualified=5,
        code_value="HK79-ABC123",
        value_vnd=10_000,
        interval=5,
    ),
    _spec(
        "referral.milestone_out_of_stock",
        "Đạt mốc mời bạn nhưng hết code",
        _tmpl(
            texts.referral_milestone_out_of_stock(
                qualified=111, tier_no=222, max_claims=333, support_link=_SUPPORT
            ),
            (_SUPPORT, "{support_link}"),
            ("111", "{qualified}"),
            ("222", "{tier_no}"),
            ("333", "{max_claims}"),
        ),
        qualified=5,
        tier_no=1,
        max_claims=10,
        support_link="https://t.me/cskh_televip",
    ),
    # ── Thống kê tài khoản ──────────────────────────────────────────
    _spec(
        "stats.account",
        "Màn hình 📊 Thống Kê TK",
        _tmpl(
            texts.account_stats(
                total_refs=1_111_111,
                total_value_vnd=2_222_222,
                available_codes=3_333_333,
                used_codes=4_444_444,
                total_users=5_555_555,
                user_id=666_666_666,
                qualified_refs=777,
                rank_today="«rank_today»",
                rank_alltime="«rank_alltime»",
                total_codes_received=888,
                total_value_received_vnd=9_999_999,
                updated_at=_STAMP,
            ),
            (_STAMP_SECONDS, "{updated_at}"),
            ("1.111.111", "{total_refs:vnd}"),
            ("2.222.222", "{total_value_vnd:vnd}"),
            ("3.333.333", "{available_codes:vnd}"),
            ("4.444.444", "{used_codes:vnd}"),
            ("5.555.555", "{total_users:vnd}"),
            ("9.999.999", "{total_value_received_vnd:vnd}"),
            ("666666666", "{user_id}"),
            ("777", "{qualified_refs}"),
            ("888", "{total_codes_received}"),
            ("«rank_today»", "{rank_today}"),
            ("«rank_alltime»", "{rank_alltime}"),
        ),
        updated_at="03:04:05 02/01/2026",
        total_refs=4_020,
        total_value_vnd=147_990_000,
        available_codes=1_769,
        used_codes=3_412,
        total_users=19_151,
        total_value_received_vnd=30_000,
        user_id=123_456_789,
        qualified_refs=5,
        total_codes_received=3,
        rank_today="12",
        rank_alltime="34",
    ),
    # ── Bảng xếp hạng ───────────────────────────────────────────────
    _spec(
        "leaderboard.screen",
        "Màn hình 👑 BXH",
        _tmpl(
            texts.leaderboard(
                today=True,
                top_referrers=[("«r»", 111)],
                top_receivers=[("«c»", 2_222_222)],
                top_streaks=[("«s»", 333)],
                updated_at=_STAMP,
            ),
            (_STAMP_MINUTES, "{updated_at}"),
            ("🥇 «r»\n   💎 111 người", "{block_referrers}"),
            ("🥇 «c»\n   💵 2.222.222đ", "{block_receivers}"),
            ("🥇 «s»\n   🔥 333 ngày liên tiếp", "{block_streaks}"),
            ("HÔM NAY", "{mode}"),
        ),
        mode="HÔM NAY",
        block_referrers="🥇 @an\n   💎 12 người",
        block_receivers="🥇 @binh\n   💵 120.000đ",
        block_streaks="🥇 @cuc\n   🔥 30 ngày liên tiếp",
        updated_at="03:04 02/01/2026",
    ),
    # ── Điểm danh ───────────────────────────────────────────────────
    _spec(
        "checkin.success",
        "Điểm danh thành công",
        _tmpl(
            texts.checkin_success(points=111, balance=2_222_222, streak=333, tiers=[1_000_000]),
            (f"{_CHECKIN_TIER_LINE} (TỐI ĐA)", "{tier_lines}"),
            ("2.222.222", "{balance:vnd}"),
            ("333", "{streak}"),
            ("111", "{points:vnd}"),
        ),
        points=1_000,
        balance=12_000,
        streak=7,
        tier_lines="   • 10.000đ = Code 10K\n   • 20.000đ = Code 20K (TỐI ĐA)",
    ),
    _spec(
        "checkin.already",
        "Đã điểm danh hôm nay",
        _tmpl(
            texts.checkin_already(balance=1_111_111, streak=222),
            ("1.111.111", "{balance:vnd}"),
            ("222", "{streak}"),
        ),
        balance=12_000,
        streak=7,
    ),
    # ── Đổi điểm lấy code ───────────────────────────────────────────
    _spec(
        "redeem.menu",
        "Màn hình 🎁 Đổi CODE",
        _tmpl(
            _REDEEM_NOT_READY,
            (_REDEEM_TIER_LINE, "{tier_lines}"),
            (_FOOTER_NOT_READY, "{footer}"),
            ("1.111.111", "{balance:vnd}"),
            ("333", "{points_per_day:vnd}"),
        ),
        balance=12_000,
        tier_lines="✅ Code 10K (10.000đ) - ĐỦ ĐIỂM",
        points_per_day=1_000,
        footer="👇 Chọn loại code bạn muốn đổi:",
    ),
    _spec("redeem.menu_footer_ready", "Đổi CODE — câu kết khi đủ điểm", _FOOTER_READY),
    _spec("redeem.menu_footer_not_ready", "Đổi CODE — câu kết khi chưa đủ điểm", _FOOTER_NOT_READY),
    _spec(
        "redeem.success",
        "Đổi điểm lấy code thành công",
        _tmpl(
            texts.redeem_success(code_value=_CODE, value_vnd=1_234_567, balance=2_345_678),
            ("1.234.567", "{value_vnd:vnd}"),
            ("2.345.678", "{balance:vnd}"),
            (_CODE, "{code_value}"),
        ),
        code_value="HK79-ABC123",
        value_vnd=10_000,
        balance=2_000,
    ),
    _spec(
        "redeem.out_of_stock",
        "Đổi điểm — hết code mệnh giá này",
        _tmpl(
            texts.redeem_out_of_stock(
                value_vnd=1_234_567, balance=2_345_678, support_link=_SUPPORT
            ),
            ("1.234.567", "{value_vnd:vnd}"),
            ("2.345.678", "{balance:vnd}"),
            (_SUPPORT, "{support_link}"),
        ),
        value_vnd=10_000,
        balance=12_000,
        support_link="https://t.me/cskh_televip",
    ),
    _spec(
        "redeem.not_enough",
        "Đổi điểm — không đủ điểm",
        _tmpl(
            texts.redeem_not_enough(balance=1_111_111, value_vnd=2_222_222),
            ("1.111.111", "{balance:vnd}"),
            ("2.222.222", "{value_vnd:vnd}"),
        ),
        balance=5_000,
        value_vnd=10_000,
    ),
    # ── Event đập hộp ───────────────────────────────────────────────
    _spec(
        "event.box_caption",
        "Caption event đập hộp",
        _tmpl(
            texts.event_box_caption([(0, 1_234), (5_000, 8_766)], _GAME),
            (_PRIZE_LINES, "{prize_lines}"),
            (_GAME, "{game_link}"),
        ),
        prize_lines="🤮 Hộp rỗng 50% 👻\n🎲 10k = 🎯 50%",
        game_link="https://televip.game",
    ),
    _spec(
        "event.box_win",
        "Đập hộp trúng code",
        _tmpl(
            texts.event_box_win(_CODE, 1_234_567),
            ("1.234.567", "{value_vnd:vnd}"),
            (_CODE, "{code_value}"),
        ),
        code_value="HK79-ABC123",
        value_vnd=10_000,
    ),
    _spec(
        "event.box_empty",
        "Đập hộp không trúng",
        _tmpl(texts.event_box_empty(_GAME), (_GAME, "{game_link}")),
        game_link="https://televip.game",
    ),
    _spec(
        "event.box_budget_capped",
        "Đập hộp — đợt đã chạm trần ngân sách",
        _tmpl(texts.event_box_budget_capped(_GAME), (_GAME, "{game_link}")),
        game_link="https://televip.game",
    ),
    _spec(
        "event.share_default",
        "Nội dung mặc định của event chia sẻ",
        _tmpl(
            texts.share_event_default(
                bot_link=_BOT, game_link=_GAME, group_link=_GROUP, required_groups=111
            ),
            (_BOT, "{bot_link}"),
            (_GAME, "{game_link}"),
            (_GROUP, "{group_link}"),
            ("111", "{required_groups}"),
        ),
        bot_link="https://t.me/televip_bot",
        game_link="https://televip.game",
        group_link="https://t.me/televip_group",
        required_groups=5,
    ),
    # ── Hỗ trợ · chơi game · hướng dẫn ──────────────────────────────
    _spec(
        "support.contact",
        "Màn hình 💁‍♀️ Hỗ Trợ – CSKH",
        _tmpl(texts.support(_SUPPORT), (_SUPPORT, "{support_link}")),
        support_link="https://t.me/cskh_televip",
    ),
    _spec("game.play", "Màn hình 🎮 Chơi Game", texts.play_game()),
    _spec(
        "help.text",
        "Nội dung /help",
        _tmpl(
            texts.help_text(interval=111, reward_value_vnd=1_234_000, max_claims=222),
            ("1234K", "{reward_label}"),
            ("24642", "{max_people}"),
            ("111", "{interval}"),
            ("222", "{max_claims}"),
        ),
        interval=5,
        reward_label="10K",
        max_claims=10,
        max_people=50,
    ),
    # ── Check chia sẻ ───────────────────────────────────────────────
    _spec(
        "share.progress",
        "Màn hình 📈 Check Chia sẻ",
        _tmpl(
            texts.share_progress(**_PROGRESS_ARGS),
            (_PROGRESS_BLOCK, "{campaign_block}"),
            (_PROGRESS_STATUS, "{status_line}"),
            ("3.333.333", "{total_vnd:vnd}"),
            ("111", "{qualified}"),
            ("222", "{claimed}"),
            ("555", "{max_claims}"),
        ),
        qualified=12,
        claimed=2,
        max_claims=10,
        total_vnd=20_000,
        status_line="⏳ Còn 3 người nữa để nhận lần thứ 3",
        campaign_block="💎 PHẦN THƯỞNG: …",
    ),
    _spec(
        "share.status_max",
        "Check chia sẻ — đã đạt tối đa",
        _tmpl(
            texts.share_status_line(qualified=0, claimed=222, interval=1, max_claims=222),
            ("222", "{max_claims}"),
        ),
        max_claims=10,
    ),
    _spec(
        "share.status_waiting",
        "Check chia sẻ — còn thiếu người",
        _tmpl(
            texts.share_status_line(qualified=111, claimed=0, interval=444, max_claims=555),
            ("lần thứ 1", "lần thứ {next_tier}"),
            ("333", "{people_needed}"),
            ("444", "{next_milestone}"),
        ),
        people_needed=3,
        next_tier=3,
        next_milestone=15,
    ),
    _spec(
        "share.status_ready",
        "Check chia sẻ — đủ điều kiện, đang xử lý",
        _tmpl(
            texts.share_status_line(qualified=444, claimed=0, interval=444, max_claims=555),
            ("lần thứ 1!", "lần thứ {next_tier}!"),
        ),
        next_tier=3,
    ),
    # ── Lỗi chung ───────────────────────────────────────────────────
    _spec(
        "error.rate_limited",
        "Bấm quá nhanh",
        _tmpl(texts.rate_limited(111), ("111", "{seconds}")),
        seconds=3,
    ),
    _spec("error.system_busy", "Hệ thống đang bận", texts.system_busy()),
    # ── Hướng dẫn vận hành (/huongdan, chỉ admin đọc) ───────────────
    _spec("guide.codes", "Hướng dẫn 1/3 — kho code", texts.guide_codes()),
    _spec("guide.broadcast", "Hướng dẫn 2/3 — bắn tin & event", texts.guide_broadcast()),
    _spec("guide.config", "Hướng dẫn 3/3 — cấu hình & sự cố", texts.guide_config()),
)

TEMPLATES: Final[dict[str, TemplateSpec]] = {spec.key: spec for spec in _SPECS}

#: Dữ liệu để migration `0005` seed vào bảng. Tuple phẳng, không mang theo hành vi nào —
#: migration chỉ cần bốn cột này và không được phụ thuộc vào phần còn lại của module.
TEMPLATE_SEED: Final[tuple[dict[str, Any], ...]] = tuple(
    {
        "key": spec.key,
        "content": spec.content,
        "label_vi": spec.label_vi,
        "required_vars": list(spec.required_vars),
    }
    for spec in _SPECS
)


def _self_check() -> None:
    """Bản mặc định phải render được bằng chính bộ mẫu của nó, ngay lúc import.

    Đây là chỗ bắt lỗi rẻ nhất: một placeholder gõ nhầm trong `_SPECS` sẽ nổ khi nạp
    module (tức khi chạy test), không phải khi một người thật bấm nút.
    """
    for spec in _SPECS:
        used = set(template_vars(spec.content))
        declared = set(spec.required_vars)
        if used != declared:
            raise RuntimeError(
                f"khoá {spec.key!r}: biến trong nội dung {sorted(used)} "
                f"khác biến khai báo {sorted(declared)}"
            )
        format_template(spec.content, spec.sample)


_self_check()


def default_content(key: str) -> str:
    """Nội dung mặc định của một khoá, lấy từ `domain/texts.py`.

    Ném:
        TemplateError: khoá không có trong đăng ký.
    """
    spec = TEMPLATES.get(key)
    if spec is None:
        raise TemplateError(f"không có khoá câu chữ {key!r}")
    return spec.content


def preview(key: str, content: str | None = None) -> str:
    """Bản xem thử: nội dung đã điền bộ giá trị mẫu của khoá.

    Đây là thứ `/xemnoidung` và `/suanoidung` in ra cho admin — nhìn một template đầy
    `{ngoặc}` thì không ai biết tin thật trông thế nào.
    """
    spec = TEMPLATES.get(key)
    if spec is None:
        raise TemplateError(f"không có khoá câu chữ {key!r}")
    return format_template(spec.content if content is None else content, spec.sample)


# ══════════════════════════════════════════════════════════════════════════════
# Cache
# ══════════════════════════════════════════════════════════════════════════════

# Chỉ chứa bản ĐÃ SỬA trong database. Khoá vắng mặt ở đây là khoá đang dùng bản mặc định
# trong code — trạng thái bình thường, không phải lỗi.
_cache: dict[str, str] = {}
# `time.monotonic()` chứ không phải đồng hồ treo tường: đây là phép đo KHOẢNG, một lần NTP
# chỉnh giờ lùi sẽ làm cache treo quá hạn nếu so bằng `now_utc()`.
_loaded_at: float | None = None
_lock: Final[asyncio.Lock] = asyncio.Lock()


def invalidate() -> None:
    """Đánh dấu cache hết hạn; lượt đọc kế tiếp nạp lại."""
    global _loaded_at
    _loaded_at = None


async def refresh(*, db: AsyncSession | None = None) -> None:
    """Nạp lại toàn bộ bảng ngay, bỏ qua TTL."""
    async with _lock:
        await _load(db)


def _is_stale() -> bool:
    return _loaded_at is None or (time.monotonic() - _loaded_at) >= CACHE_TTL_SECONDS


async def _ensure_fresh(db: AsyncSession | None) -> None:
    if not _is_stale():
        return
    async with _lock:
        if _is_stale():
            await _load(db)


async def _load(db: AsyncSession | None) -> None:
    global _cache, _loaded_at

    stmt = select(MessageTemplate.key, MessageTemplate.content)
    try:
        if db is not None:
            rows = (await db.execute(stmt)).all()
        else:
            async with session() as own:
                rows = (await own.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001 - xem docstring bên dưới
        # Database hỏng KHÔNG được làm bot câm. Giữ nguyên bản sao cũ (hoặc rỗng, tức dùng
        # bản mặc định trong code) và thử lại ở lượt sau — đúng lý do module này tồn tại.
        _loaded_at = time.monotonic()
        log.warning("text_templates.load_failed", error=str(exc))
        return

    # Dựng dict mới rồi gán đè thay vì `clear()` + điền: lượt đọc chen vào giữa không bao
    # giờ nhìn thấy một bảng câu chữ rỗng một nửa.
    _cache = {key: content for key, content in rows}
    _loaded_at = time.monotonic()
    log.debug("text_templates.loaded", count=len(_cache))


# ══════════════════════════════════════════════════════════════════════════════
# Đọc
# ══════════════════════════════════════════════════════════════════════════════


async def render(key: str, **variables: Any) -> str:
    """Câu chữ của `key`, đã điền biến.

    Ưu tiên bản trong `message_templates`; thiếu khoá — hoặc bản trong bảng render hỏng —
    thì rơi về bản mặc định trong `domain/texts.py`.

    Ném:
        TemplateError: khoá không tồn tại ở cả hai nơi.
    """
    await _ensure_fresh(None)

    stored = _cache.get(key)
    fallback = TEMPLATES[key].content if key in TEMPLATES else None

    if stored is None:
        if fallback is None:
            raise TemplateError(f"không có khoá câu chữ {key!r}")
        return format_template(fallback, variables)

    try:
        return format_template(stored, variables)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        if fallback is None:
            raise
        # Bản trong bảng lẽ ra không thể hỏng (`set_content` đã thử render trước khi ghi),
        # nên tới được đây nghĩa là ai đó sửa thẳng database. Ghi ERROR để có người biết,
        # và vẫn trả về câu tử tế cho người đang chờ.
        log.error("text_template.render_failed", key=key, error=str(exc))
        return format_template(fallback, variables)


@dataclass(frozen=True, slots=True)
class TemplateInfo:
    """Một dòng của `/noidung`."""

    key: str
    label_vi: str
    content: str
    required_vars: tuple[str, ...]
    #: True khi nội dung đang dùng khác bản mặc định trong code.
    customized: bool
    updated_at: datetime | None = None
    updated_by: int | None = None


async def list_keys(
    prefix: str | None = None, *, db: AsyncSession | None = None
) -> list[TemplateInfo]:
    """Mọi khoá câu chữ, lọc theo tiền tố, sắp theo khoá.

    Hợp nhất hai nguồn: đăng ký trong code (tập khoá đầy đủ) và bảng (bản admin đang
    chạy). Chỉ liệt kê từ bảng thì một khoá chưa seed sẽ vô hình với admin — mà nó vẫn
    đang được gửi cho người dùng mỗi ngày.
    """
    stmt = select(MessageTemplate)
    if db is not None:
        rows = list((await db.execute(stmt)).scalars())
    else:
        async with session() as own:
            rows = list((await own.execute(stmt)).scalars())
    stored = {row.key: row for row in rows}

    keys = sorted(set(TEMPLATES) | set(stored))
    out: list[TemplateInfo] = []
    for key in keys:
        if prefix and not key.startswith(prefix):
            continue
        spec = TEMPLATES.get(key)
        row = stored.get(key)
        content = row.content if row is not None else (spec.content if spec else "")
        required = (
            spec.required_vars
            if spec is not None
            else tuple(row.required_vars if row is not None else ())
        )
        out.append(
            TemplateInfo(
                key=key,
                label_vi=(row.label_vi if row is not None else spec.label_vi if spec else key),
                content=content,
                required_vars=required,
                customized=spec is not None and content != spec.content,
                updated_at=row.updated_at if row is not None else None,
                updated_by=row.updated_by if row is not None else None,
            )
        )
    return out


async def get_info(key: str, *, db: AsyncSession | None = None) -> TemplateInfo:
    """Một khoá. Ném `TemplateError` nếu không có ở cả đăng ký lẫn bảng."""
    for info in await list_keys(db=db):
        if info.key == key:
            return info
    raise TemplateError(f"không có khoá câu chữ {key!r}")


# ══════════════════════════════════════════════════════════════════════════════
# Ghi
# ══════════════════════════════════════════════════════════════════════════════


#: Tran do dai an toan cho mot tin nhan Telegram. Tran cung la 4.096; chua 96 ky tu
#: dem vi gia tri that cua bien co the dai hon gia tri mau dung luc kiem.
MAX_RENDERED_LENGTH = 4000


def validate(key: str, content: str) -> None:
    """Ba lớp kiểm của docstring module. Không chạm database.

    Ném:
        TemplateError: nội dung rỗng, thiếu biến bắt buộc, có biến lạ, hoặc render hỏng.
    """
    if not content.strip():
        raise TemplateError("nội dung rỗng")

    spec = TEMPLATES.get(key)
    if spec is None:
        raise TemplateError(f"không có khoá câu chữ {key!r}")

    used = template_vars(content)
    missing = [name for name in spec.required_vars if name not in used]
    if missing:
        raise TemplateError("thiếu biến bắt buộc: " + ", ".join(f"{{{name}}}" for name in missing))

    unknown = [name for name in used if name not in spec.required_vars]
    if unknown:
        allowed = ", ".join(f"{{{name}}}" for name in spec.required_vars) or "(không có biến nào)"
        raise TemplateError(
            "biến lạ: "
            + ", ".join(f"{{{name}}}" for name in unknown)
            + f" — khoá này chỉ nhận: {allowed}"
        )

    try:
        rendered = format_template(content, spec.sample)
    except Exception as exc:  # noqa: BLE001 - mọi lỗi render đều là lý do từ chối
        raise TemplateError(f"nội dung không render được: {type(exc).__name__}: {exc}") from exc

    # Lớp thứ tư: độ dài. Telegram cắt một tin nhắn ở 4096 ký tự và trả `BadRequest`,
    # thứ mà lớp gửi coi là **lỗi vĩnh viễn** nên không thử lại. Nghĩa là một lần dán
    # nhầm nội dung dài làm câu đó chết với TOÀN BỘ người dùng, và log chỉ hiện
    # "lỗi vĩnh viễn phía Telegram" — không ai đoán ra nguyên nhân là admin sửa chữ.
    #
    # Đo trên bản ĐÃ ĐIỀN BIẾN, không đo trên khuôn: `{code_value}` chỉ 12 ký tự nhưng
    # giá trị thật có thể dài hơn. Chừa 96 ký tự đệm vì giá trị thật cũng có thể dài
    # hơn giá trị mẫu.
    if len(rendered) > MAX_RENDERED_LENGTH:
        raise TemplateError(
            f"nội dung sau khi điền biến dài {len(rendered):,} ký tự, "
            f"vượt mức an toàn {MAX_RENDERED_LENGTH:,} (trần cứng của Telegram là 4.096). "
            f"Hãy rút ngắn hoặc tách thành nhiều tin."
        )


async def set_content(
    key: str, content: str, *, updated_by: int, db: AsyncSession | None = None
) -> str:
    """Đổi câu chữ của một khoá. Trả về nội dung đã ghi.

    Kiểm **trước** khi ghi (`validate`), rồi ghi `message_templates` và
    `message_templates_audit` trong **cùng một giao dịch** — không có đường nào đổi câu
    chữ mà không để lại đường quay lui.

    Ném:
        TemplateError: khoá không tồn tại, hoặc nội dung mới không qua được `validate`.
    """
    validate(key, content)

    if db is not None:
        await _write(db, key, content, updated_by)
    else:
        async with transaction() as own:
            await _write(own, key, content, updated_by)

    invalidate()
    log.info("text_template.changed", key=key, changed_by=updated_by)
    return content


async def reset(key: str, *, updated_by: int, db: AsyncSession | None = None) -> str:
    """Trả một khoá về bản mặc định trong `domain/texts.py`. Trả về nội dung đó.

    Vẫn đi qua đường ghi bình thường: một lần khôi phục cũng là một lần đổi câu chữ, và nó
    phải nằm trong sổ đúng như mọi lần khác.
    """
    return await set_content(key, default_content(key), updated_by=updated_by, db=db)


async def _write(db: AsyncSession, key: str, content: str, updated_by: int) -> None:
    spec = TEMPLATES[key]
    # `with_for_update` để hai admin sửa cùng một khoá phải xếp hàng: nếu không, cả hai đọc
    # cùng `old_content` và sổ ghi lại một lịch sử không có thật.
    row = await db.get(MessageTemplate, key, with_for_update=True)

    if row is None:
        # Khoá có trong code mà chưa có trong bảng (database dựng trước migration seed, hoặc
        # test vừa TRUNCATE). Tạo dòng thay vì báo lỗi: tập khoá do code định nghĩa.
        db.add(
            MessageTemplate(
                key=key,
                content=content,
                label_vi=spec.label_vi,
                required_vars=list(spec.required_vars),
                updated_by=updated_by,
            )
        )
        old_content = None
    else:
        old_content = row.content
        row.content = content
        row.label_vi = spec.label_vi
        row.required_vars = list(spec.required_vars)
        row.updated_by = updated_by

    db.add(
        MessageTemplateAudit(
            key=key,
            old_content=old_content,
            new_content=content,
            changed_by=updated_by,
        )
    )


__all__ = [
    "CACHE_TTL_SECONDS",
    "TEMPLATES",
    "TEMPLATE_SEED",
    "TemplateError",
    "TemplateInfo",
    "TemplateSpec",
    "default_content",
    "format_template",
    "get_info",
    "invalidate",
    "list_keys",
    "preview",
    "refresh",
    "render",
    "reset",
    "set_content",
    "template_vars",
    "validate",
]
