"""cau hinh thong ke, bang xep hang, job dinh ky va hai index cho bang xep hang ngay

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-30

Sáu khoá cấu hình + hai index. Không có bảng mới: `system_stats`, `user_stats`,
`referrals`, `checkins` đều đã có từ `0001`.

**Khoá cấu hình** (§13.6, nguyên tắc N2 — không con số nghiệp vụ nào nằm trong code):

* `stats.refresh_seconds` (60) — chu kỳ job làm mới `system_stats`.
* `stats.max_age_seconds` (300) — ảnh chụp cũ hơn mức này thì lượt bấm tự tính lại. Đây là
  lưới an toàn cho trường hợp job chết, không phải đường chạy bình thường; đặt bằng đúng
  chu kỳ job sẽ biến mỗi mốc hết hạn thành một cơn bão tính lại.
* `jobs.reap_reservations_seconds` (60) — chu kỳ trả mã giữ chỗ quá hạn về kho.
* `leaderboard.top_n` (3) — số dòng mỗi khối bảng xếp hạng (§13.2.6).
* `share_event.required_groups` (5) — số nhóm phải chia sẻ, in trong caption §13.2.10.
* `link.group` ("") — link nhóm cộng đồng in trong chính caption đó. §13.6.8 liệt kê bốn
  link, nhưng caption mẫu của §13.2.10 có dòng "📱 Group Cộng Đồng: <link group>"; để nó
  thành chuỗi gõ trong handler là dựng lại đúng cái tật §13.6.8 mô tả (link game của hệ cũ
  nằm rải rác 7 chỗ). Seed rỗng, cùng lý do với bốn khoá `link.*` kia.

**Hai index** phục vụ đúng hai truy vấn của bảng xếp hạng chế độ "hôm nay", vốn lọc bằng
`created_at >= :đầu_ngày AND < :đầu_ngày_kế` (`vn_day_bounds`) chứ không bằng
`date(created_at) = :ngày` — bọc cột trong một hàm là vứt bỏ index và quét toàn bảng.
`code_grants` không cần index mới: `ix_grants_recent (created_at DESC)` đã phục vụ được.

Dùng `CREATE INDEX IF NOT EXISTS` thay cho `op.create_index`: hai khối làm việc song song
có thể cùng nghĩ tới một index trên `checkins`, và một migration đổ vỡ giữa chừng vì trùng
tên thì tệ hơn nhiều so với một câu lệnh không làm gì.

Mọi câu INSERT đều `ON CONFLICT DO NOTHING`: chạy lại trên database đã có sẵn dòng đó
không được ghi đè giá trị mà vận hành đã chỉnh bằng `/setcauhinh`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (key, value, value_type, label_vi, min_value, max_value)
SETTINGS_SEED: tuple[tuple[str, Any, str, str, int | None, int | None], ...] = (
    (
        "stats.refresh_seconds",
        60,
        "seconds",
        "Chu kỳ job làm mới bảng system_stats (giây)",
        5,
        3_600,
    ),
    (
        "stats.max_age_seconds",
        300,
        "seconds",
        "Số liệu cũ hơn mức này thì lượt bấm tự tính lại (giây)",
        10,
        86_400,
    ),
    (
        "jobs.reap_reservations_seconds",
        60,
        "seconds",
        "Chu kỳ trả mã giữ chỗ quá hạn về kho (giây)",
        10,
        3_600,
    ),
    ("leaderboard.top_n", 3, "int", "Số dòng mỗi khối bảng xếp hạng", 1, 20),
    (
        "share_event.required_groups",
        5,
        "int",
        "Số nhóm phải chia sẻ bài viết event",
        1,
        50,
    ),
    ("link.group", "", "string", "Link nhóm cộng đồng", None, None),
)

_INDEXES: tuple[tuple[str, str], ...] = (
    # Khối `🏆 TOP MỜI BẠN BÈ` của chế độ hôm nay: đếm referral phát sinh trong ngày.
    (
        "ix_referrals_created_at",
        "CREATE INDEX IF NOT EXISTS ix_referrals_created_at ON referrals (created_at)",
    ),
    # Khối `🔥 TOP ĐIỂM DANH` của chế độ hôm nay: streak cao nhất trong ngày nghiệp vụ.
    # Cột dẫn đầu là `business_date` vì khoá chính `(user_id, business_date)` dẫn đầu bằng
    # `user_id` nên không phục vụ được câu hỏi "hôm nay ai điểm danh".
    (
        "ix_checkins_business_date_streak",
        "CREATE INDEX IF NOT EXISTS ix_checkins_business_date_streak "
        "ON checkins (business_date, streak_after DESC)",
    ),
)


def upgrade() -> None:
    conn = op.get_bind()
    for key, value, value_type, label, min_value, max_value in SETTINGS_SEED:
        conn.execute(
            sa.text("""
            INSERT INTO settings (key, value, value_type, label_vi, min_value, max_value,
                                  sensitive)
                 VALUES (:key, CAST(:value AS jsonb), :value_type, :label,
                         :min_value, :max_value, false)
            ON CONFLICT (key) DO NOTHING
            """),
            {
                "key": key,
                # JSONB nhận chuỗi có nháy kép; số thì không có nháy.
                "value": f'"{value}"' if isinstance(value, str) else str(value),
                "value_type": value_type,
                "label": label,
                "min_value": min_value,
                "max_value": max_value,
            },
        )

    for _name, ddl in _INDEXES:
        conn.execute(sa.text(ddl))


def downgrade() -> None:
    conn = op.get_bind()
    for name, _ddl in _INDEXES:
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    # `settings_audit` KHÔNG bị đụng tới: nó append-only, và lịch sử ai từng đổi các khoá
    # này vẫn phải đọc được sau khi hạ migration.
    conn.execute(
        sa.text("DELETE FROM settings WHERE key = ANY(:keys)"),
        {"keys": [row[0] for row in SETTINGS_SEED]},
    )
