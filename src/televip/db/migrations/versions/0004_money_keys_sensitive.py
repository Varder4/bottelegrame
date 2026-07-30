"""danh dau cac khoa tien la sensitive

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30

Đặc tả §13.4.1 yêu cầu **duyệt hai người** cho mọi lệnh `/setcauhinh` chạm vào khoá tiền.
Handler đã có nhánh từ chối đúng như vậy, nhưng nó chỉ kích hoạt khi `sensitive = true`,
mà toàn bộ khoá tiền lại đang là `false` — nên luật hai chữ ký chưa từng chạy một lần nào.

Nguy hiểm nhất là `admin.dual_approval_threshold_vnd`: chính cái ngưỡng quyết định khi nào
cần hai người lại sửa được bằng một người. Ai đó nâng nó lên một tỉ là mọi luật còn lại
tắt theo, chỉ bằng một tin nhắn.

Sửa ở tầng dữ liệu chứ không ở handler, vì như vậy mọi đường vào bảng `settings` — kể cả
lệnh viết sau này — đều được che cùng một lúc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Khoá kiểu `json` nhưng nội dung là danh sách mệnh giá tiền — đổi chúng là đổi số tiền
#: được phép nạp vào kho, nên nguy hiểm ngang khoá `money_vnd`.
MONEY_JSON_KEYS = ("code.allowed_values", "code.category_values")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
        UPDATE settings
           SET sensitive = true
         WHERE value_type = 'money_vnd'
            OR key = ANY(:json_keys)
        """),
        {"json_keys": list(MONEY_JSON_KEYS)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
        UPDATE settings
           SET sensitive = false
         WHERE value_type = 'money_vnd'
            OR key = ANY(:json_keys)
        """),
        {"json_keys": list(MONEY_JSON_KEYS)},
    )
