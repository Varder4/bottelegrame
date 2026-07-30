"""seed config

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30 12:31:06.466928

Migration **dữ liệu**, không phải schema — viết tay, không autogenerate.

Nó nạp hai từ điển mà phần còn lại của hệ thống coi là đã có sẵn:

* ``grant_types`` — 6 dòng. ``code_grants.grant_type`` trỏ FK vào đây, nên thiếu một dòng
  là luồng phát tương ứng không chạy được.
* ``settings`` — **toàn bộ** khoá của ``13-dac-ta-bot-moi.md`` §13.6.2-13.6.8 (danh sách
  nhóm và số lượng ở ``07-db.md`` §7.2). Đây là chỗ nguyên tắc **N2** trở thành sự thật:
  mỗi con số dưới đây là một dòng dữ liệu sửa được bằng ``/setcauhinh``, không phải một
  hằng số phải sửa code rồi deploy lại.

Giá trị seed lấy đúng cột "Seed" của §13.6. Chỗ nào đặc tả không cho con số cụ thể
(``code.category_values``, nhóm ``link.*``, ``webapp.url``) thì ghi rõ lý do tại chỗ.

``min_value`` / ``max_value`` là hàng rào chống gõ nhầm của ``/setcauhinh``, không phải
luật nghiệp vụ: chúng chỉ nói "con số này chắc chắn sai", không nói "con số này đúng".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── grant_types ─────────────────────────────────────────────────────
# `once_per_life` phải khớp `ONCE_PER_LIFE_GRANT_TYPES` trong `db/models/codes.py`: hàng
# rào thật là partial unique index `uq_grant_once_semantic`, cờ này để lệnh admin và báo
# cáo đọc. Hai chỗ lệch nhau nghĩa là báo cáo nói một đằng, database chặn một nẻo.
GRANT_TYPES_SEED: tuple[dict[str, Any], ...] = (
    {"code": "tanthu", "label_vi": "Code tân thủ", "once_per_life": True},
    {"code": "referral_milestone", "label_vi": "Mốc mời bạn bè", "once_per_life": False},
    {"code": "event_box", "label_vi": "Đập hộp event", "once_per_life": False},
    {"code": "points_redeem", "label_vi": "Đổi điểm lấy code", "once_per_life": False},
    {"code": "share_event", "label_vi": "Event chia sẻ", "once_per_life": True},
    {"code": "admin_manual", "label_vi": "Admin trao tay", "once_per_life": False},
)


def _setting(
    key: str,
    value: Any,
    value_type: str,
    label_vi: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
    sensitive: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "value_type": value_type,
        "label_vi": label_vi,
        "min_value": min_value,
        "max_value": max_value,
        "sensitive": sensitive,
    }


def _cooldowns() -> tuple[dict[str, Any], ...]:
    """12 khoá `cooldown.*` của §13.6.6, seed đúng theo giá trị chạy thật của hệ cũ.

    Trần 3.600 giây là hàng rào chống gõ nhầm: đặt cooldown một tiếng cho `/start` thì coi
    như tắt bot, và đó gần như chắc chắn là lỗi thừa một số 0 chứ không phải ý định.
    """
    rows = (
        ("start", 3, "/start"),
        ("tan_thu", 5, "🎁Code Tân Thủ"),
        ("moi_ban", 3, "💎 Mời bạn nhận quà"),
        ("stats", 5, "📊Thống Kê TK"),
        ("leaderboard", 5, "👑 BXH"),
        ("check_share", 3, "📈Check Chia sẻ"),
        ("checkin", 7, "✅ Điểm Danh"),
        ("redeem_code", 5, "🎁 Đổi CODE"),
        ("play_game", 2, "🎮 Chơi Game 🎮"),
        ("support", 3, "💁‍♀️ Hỗ Trợ – CSKH"),
        ("share_event", 3, "📢 EVENT"),
        ("default", 2, "Mọi thứ khác"),
    )
    return tuple(
        _setting(
            f"cooldown.{name}",
            seconds,
            "seconds",
            f"Chờ giữa hai lần bấm: {button} (giây)",
            min_value=0,
            max_value=3600,
        )
        for name, seconds, button in rows
    )


SETTINGS_SEED: tuple[dict[str, Any], ...] = (
    # ── §13.6.2 Loại code và mệnh giá ───────────────────────────────
    _setting(
        "code.tanthu_value_vnd",
        10_000,
        "money_vnd",
        "Mệnh giá code tân thủ (VNĐ)",
        min_value=0,
        max_value=1_000_000,
    ),
    _setting(
        "code.eventchiase_value_vnd",
        10_000,
        "money_vnd",
        "Mệnh giá code event chia sẻ (VNĐ)",
        min_value=0,
        max_value=1_000_000,
    ),
    _setting(
        "code.allowed_values",
        [5_000, 10_000, 20_000, 50_000, 88_000, 100_000, 200_000, 500_000],
        "json",
        "Mệnh giá được phép nạp kho bằng /add_giffcode",
    ),
    # Đặc tả nói khoá này tồn tại và mang ràng buộc "loại nào được dùng mệnh giá nào"
    # nhưng KHÔNG liệt kê nội dung. Seed dưới đây suy ra từ chính bảng §13.6.2 (mệnh giá
    # của từng luồng), bậc đổi điểm §13.6.4 và sáu mức của bảng tỉ lệ §13.5.1. Khoá của
    # nó là `CODE_TYPES` trong `db/models/codes.py` — loại code trong KHO, không phải
    # `grant_type` của sổ cái.
    _setting(
        "code.category_values",
        {
            "tanthu": [10_000],
            "moibanbe": [10_000],
            "event": [5_000, 10_000, 20_000, 50_000, 88_000],
            "diemdanh": [10_000, 20_000, 50_000],
            "eventchiase": [10_000],
        },
        "json",
        "Loại code nào được dùng mệnh giá nào",
    ),
    _setting(
        "redeem.tiers",
        [10_000, 20_000, 50_000],
        "json",
        "Các bậc đổi điểm lấy code (VNĐ)",
    ),
    # ── §13.6.3 Mời bạn bè ──────────────────────────────────────────
    _setting(
        "referral.interval",
        5,
        "int",
        "Số người mời thành công cho một mốc",
        min_value=1,
        max_value=1_000,
    ),
    _setting(
        "referral.reward_value_vnd",
        10_000,
        "money_vnd",
        "Giá trị code mỗi mốc mời bạn (VNĐ)",
        min_value=0,
        max_value=1_000_000,
    ),
    # 10 là trần MỐC, không phải trần NGƯỜI: 10 mốc × 5 người = tối đa 50 người được tính.
    # Hệ cũ giữ con số này ở hai nguồn độc lập và chúng đã trôi khác nhau thật — có user
    # được trao milestone ở mốc 100, gấp đôi trần.
    _setting(
        "referral.max_claims",
        10,
        "int",
        "Số mốc tối đa mỗi người (⇒ tối đa 50 người)",
        min_value=0,
        max_value=1_000,
    ),
    _setting(
        "referral.require_verified",
        True,
        "bool",
        "Chỉ tính khi người được mời đã xác minh xong",
    ),
    _setting(
        "referral.max_risk_score",
        70,
        "int",
        "Điểm rủi ro tối đa của người được mời để referral được tính",
        min_value=0,
        max_value=100,
    ),
    # ── §13.6.4 Điểm danh và đổi điểm ───────────────────────────────
    _setting(
        "checkin.points_per_day",
        2_000,
        "int",
        "Điểm mỗi lần điểm danh",
        min_value=0,
        max_value=100_000,
    ),
    _setting(
        "checkin.reset_streak_on_miss",
        True,
        "bool",
        "Nghỉ một ngày là streak về 1",
    ),
    # `sensitive`: đổi múi giờ là dịch ranh giới "ngày nghiệp vụ" của toàn bộ điểm danh,
    # streak và bảng xếp hạng — một người đang giữ streak 30 ngày có thể đứt vì lệnh gõ vội.
    _setting(
        "time.business_tz",
        "Asia/Ho_Chi_Minh",
        "string",
        "Múi giờ của ngày nghiệp vụ",
        sensitive=True,
    ),
    # ── §13.6.5 Event đập hộp ───────────────────────────────────────
    # Phương án B của §13.5.1: tổng đúng 10.000 basis point, kỳ vọng 1.578đ/lượt.
    # **Đây là bảng tỉ lệ DUY NHẤT của hệ thống** — caption gửi cho người dùng render từ
    # chính dòng dữ liệu này. Hệ cũ có hai từ điển (một để quay, một để in), nên ba mệnh
    # giá được quảng cáo có xác suất trúng bằng 0. Không có bảng thứ hai dưới bất kỳ tên nào.
    # `sensitive` theo §13.5.1 điểm 4: đổi tỉ lệ phải có hai người duyệt.
    _setting(
        "event.prize_table",
        [
            {"value_vnd": 0, "weight_bp": 7_800},
            {"value_vnd": 5_000, "weight_bp": 1_700},
            {"value_vnd": 10_000, "weight_bp": 400},
            {"value_vnd": 20_000, "weight_bp": 70},
            {"value_vnd": 50_000, "weight_bp": 20},
            {"value_vnd": 88_000, "weight_bp": 10},
        ],
        "json",
        "Bảng tỉ lệ đập hộp — trọng số basis point, tổng phải đúng 10.000",
        sensitive=True,
    ),
    _setting(
        "event.window_minutes",
        10,
        "int",
        "Cửa sổ trúng thưởng, tính từ lúc CHÍNH người đó nhận tin (phút)",
        min_value=1,
        max_value=1_440,
    ),
    _setting(
        "event.delete_hours",
        24,
        "int",
        "Sau bao lâu bot tự xoá tin event (giờ)",
        min_value=1,
        max_value=720,
    ),
    _setting(
        "event.budget_cap_vnd",
        12_000_000,
        "money_vnd",
        "Trần chi mỗi đợt event (VNĐ)",
        min_value=0,
        max_value=1_000_000_000,
    ),
    _setting(
        "event.require_full_stock",
        True,
        "bool",
        "/send_event từ chối chạy nếu thiếu mệnh giá có trọng số > 0",
    ),
    # ── §13.6.6 Chống spam ──────────────────────────────────────────
    *_cooldowns(),
    # ── §13.6.7 Xác minh, rủi ro, hạ tầng ───────────────────────────
    _setting(
        "verify.initdata_ttl_s",
        300,
        "seconds",
        "Hạn auth_date của initData (giây)",
        min_value=30,
        max_value=3_600,
    ),
    _setting(
        "verify.captcha_ttl_s",
        120,
        "seconds",
        "Hạn challenge captcha ở server (giây)",
        min_value=30,
        max_value=3_600,
    ),
    _setting(
        "verify.require_username",
        False,
        "bool",
        "Bắt buộc có @username mới được nhận quà",
    ),
    # `sensitive`: hạ ngưỡng này xuống là từ chối phục vụ người dùng thật.
    _setting(
        "risk.block_threshold",
        85,
        "int",
        "Điểm rủi ro từ mức này trở lên thì từ chối",
        min_value=0,
        max_value=100,
        sensitive=True,
    ),
    _setting(
        "risk.review_threshold",
        60,
        "int",
        "Vùng xám: cho qua nhưng gắn cờ",
        min_value=0,
        max_value=100,
    ),
    # `shadow` = chấm điểm và ghi log, KHÔNG chặn ai. Chỉ được chuyển sang `enforce` sau
    # khi chạy shadow ≥ 4 tuần và dương tính giả < 2% (`14` G4.10 + cổng G4.16).
    # Cố ý KHÔNG có khoá `max_accounts_per_ip`: §13.6.7 ghi rõ luật đó không tồn tại.
    _setting(
        "risk.mode",
        "shadow",
        "string",
        "Chế độ chống gian lận: shadow (chỉ ghi) hoặc enforce (chặn thật)",
    ),
    _setting(
        "alert.low_code_threshold",
        50,
        "int",
        "Tồn kho dưới mức này thì cảnh báo nhóm admin",
        min_value=0,
        max_value=100_000,
    ),
    _setting(
        "alert.repeat_hours",
        6,
        "int",
        "Khoảng lặp cảnh báo (giờ)",
        min_value=1,
        max_value=168,
    ),
    _setting(
        "broadcast.rate_per_sec",
        30,
        "int",
        "Trần token bucket khi gửi tin hàng loạt (tin/giây)",
        min_value=1,
        max_value=100,
    ),
    _setting(
        "admin.dual_approval_threshold_vnd",
        1_000_000,
        "money_vnd",
        "Tổng giá trị /add_giffcode vượt mức này thì cần hai người duyệt (VNĐ)",
        min_value=0,
        max_value=1_000_000_000,
    ),
    # ── §13.6.8 Liên kết ────────────────────────────────────────────
    # Seed rỗng CÓ CHỦ Ý. Đặc tả không cho URL nào cho nhóm này, và chép link cứng của hệ
    # cũ vào đây là dựng lại đúng cái tật mà §13.6.8 đang mô tả (link game nằm rải 7 chỗ
    # trong source cũ). Vận hành phải đặt 5 khoá này bằng /setcauhinh trước khi mở bot.
    _setting("link.game_bot", "", "string", "Link bot game"),
    _setting("link.support", "", "string", "Link CSKH"),
    _setting("link.fanpage", "", "string", "Link fanpage"),
    _setting("link.share_url", "", "string", "URL cho nút Chia Sẻ Ngay"),
    _setting("webapp.url", "", "string", "URL Mini App xác minh"),
)


def _settings_table() -> sa.TableClause:
    """Bảng rút gọn cho INSERT/DELETE.

    Cố ý không dùng model ORM: migration phải chạy được với schema **tại thời điểm nó được
    viết ra**, còn model thì trôi theo phiên bản mới nhất của code.
    """
    return sa.table(
        "settings",
        sa.column("key", sa.Text),
        sa.column("value", postgresql.JSONB),
        sa.column("value_type", sa.Text),
        sa.column("label_vi", sa.Text),
        sa.column("min_value", sa.Numeric),
        sa.column("max_value", sa.Numeric),
        sa.column("sensitive", sa.Boolean),
    )


def _grant_types_table() -> sa.TableClause:
    return sa.table(
        "grant_types",
        sa.column("code", sa.Text),
        sa.column("label_vi", sa.Text),
        sa.column("once_per_life", sa.Boolean),
    )


def upgrade() -> None:
    op.bulk_insert(_grant_types_table(), list(GRANT_TYPES_SEED))
    op.bulk_insert(_settings_table(), list(SETTINGS_SEED))


def downgrade() -> None:
    settings = _settings_table()
    grant_types = _grant_types_table()

    # Xoá đúng những khoá migration này đã nạp — không `DELETE FROM settings` trần, để một
    # khoá do migration sau thêm vào không bị cuốn theo.
    op.execute(settings.delete().where(settings.c.key.in_([r["key"] for r in SETTINGS_SEED])))

    # `settings_audit` KHÔNG bị đụng tới: nó là sổ append-only. Ai đó đã đổi cấu hình rồi
    # hạ migration thì lịch sử lần đổi đó vẫn phải còn — chính là thứ hệ cũ không có.

    # Có `code_grants` trỏ vào thì FK `ondelete=RESTRICT` chặn lệnh dưới đây, và đó là hành
    # vi đúng: từ điển không được biến mất trong khi sổ cái còn tham chiếu tới nó.
    op.execute(
        grant_types.delete().where(grant_types.c.code.in_([r["code"] for r in GRANT_TYPES_SEED]))
    )
