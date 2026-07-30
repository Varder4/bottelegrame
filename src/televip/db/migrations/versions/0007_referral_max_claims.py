"""dong bo tran referral.max_claims voi CHECK cua bang

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30

Hai tầng đang nói khác nhau về cùng một con số:

- `settings.referral.max_claims` cho phép tới **1000**
- `referral_rewards` có `CHECK (tier_no BETWEEN 1 AND 10)`

Nghĩa là admin gõ `/setcauhinh referral.max_claims 20` sẽ được chấp nhận, rồi lúc phát
mốc thứ 11 database mới từ chối bằng `CheckViolation` — một lỗi không nằm trong nhánh
`except` nào của handler, nên nó giết cả vòng phát thưởng, kể cả những mốc hợp lệ đứng
sau. Người dùng mất thưởng vì một con số cấu hình trông hoàn toàn hợp lệ lúc gõ.

Đây đúng loại mâu thuẫn hai tầng mà `0008` được viết ra để bịt cho `checkin.points_per_day`;
file này làm việc tương đương cho khoá của luồng mời bạn bè.

*(File này cũng vá chỗ đứt của chuỗi migration: `0008` khai `down_revision = "0007"` trong
khi `0007` chưa từng tồn tại, nên `alembic upgrade head` chết ngay ở `KeyError: '0007'`.)*
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Trần cứng của `referral_rewards.tier_no`. Đổi số này thì phải đổi cả CHECK constraint
#: bằng một migration riêng — không phải bằng một lệnh admin.
TIER_HARD_CAP = 10


def upgrade() -> None:
    conn = op.get_bind()
    # Ép kiểu tường minh cho từng chỗ: `max_value` là NUMERIC còn `LEAST(...)` so sánh
    # với INT. Dùng chung một tham số cho cả hai làm asyncpg không suy ra được kiểu
    # ("inconsistent types deduced for parameter $1").
    conn.execute(
        sa.text("""
        UPDATE settings
           SET max_value = CAST(:cap_numeric AS numeric),
               -- Kéo giá trị đang chạy về trần nếu ai đó đã đặt vượt trước khi có ràng buộc này
               value = to_jsonb(LEAST((value #>> '{}')::int, CAST(:cap_int AS int)))
         WHERE key = 'referral.max_claims'
        """),
        {"cap_numeric": TIER_HARD_CAP, "cap_int": TIER_HARD_CAP},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE settings SET max_value = 1000 WHERE key = 'referral.max_claims'"))
