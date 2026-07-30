"""san diem danh: checkin.points_per_day khong duoc bang 0

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-30

Migration **dữ liệu**, không phải schema — viết tay, không autogenerate.

Khối điểm danh và đổi điểm (§13.2.7, §13.2.8) dùng lại toàn bộ hạ tầng đã có: bảng
`checkins` và `points_ledger` từ `0001`, cấu hình `checkin.points_per_day`,
`checkin.reset_streak_on_miss`, `redeem.tiers`, `cooldown.checkin`, `cooldown.redeem_code`
từ `0002`, và tám khoá câu chữ `checkin.*` / `redeem.*` / `alert.*` từ `0005`. Không có
bảng mới, không có khoá mới.

Thứ duy nhất phải sửa là một mâu thuẫn giữa hai tầng đã tồn tại từ trước:

    settings['checkin.points_per_day'].min_value = 0        (0002)
    CHECK (points_delta > 0) trên bảng checkins             (0001)

Nghĩa là `/setcauhinh checkin.points_per_day 0` được bảng cấu hình **chấp nhận**, rồi làm
mọi lượt điểm danh của mọi người dùng nổ giữa transaction với một `IntegrityError` không
nói gì về nguyên nhân. Một lệnh gõ nhầm, và chức năng chết im lặng cho tới khi có người
đọc log.

Nâng sàn lên 1 để lỗi bị chặn tại đúng chỗ nó phát sinh — lúc admin gõ lệnh — và câu từ
chối chỉ đích danh khoá sai. Cùng nguyên tắc với `0004`: sửa ở tầng dữ liệu thì mọi đường
vào bảng `settings`, kể cả lệnh viết sau này, đều được che cùng một lúc.

`services/checkin.py: points_per_day()` vẫn giữ lớp kiểm của riêng nó. Hai lớp là cố ý:
migration che đường ghi, hàm che đường đọc — một database dựng tay hoặc một dòng UPDATE
thẳng vẫn lọt qua lớp thứ nhất.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY = "checkin.points_per_day"


def upgrade() -> None:
    conn = op.get_bind()
    # Kéo cả giá trị đang chạy lên theo nếu nó đã bị đặt về 0: để lại một dòng vi phạm
    # chính cái sàn vừa dựng là dựng một hàng rào chỉ chặn người tới sau.
    conn.execute(
        sa.text("UPDATE settings SET value = to_jsonb(1) WHERE key = :k AND value = to_jsonb(0)"),
        {"k": KEY},
    )
    conn.execute(
        sa.text("UPDATE settings SET min_value = 1 WHERE key = :k"),
        {"k": KEY},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE settings SET min_value = 0 WHERE key = :k"),
        {"k": KEY},
    )
