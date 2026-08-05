"""khoa cau hinh link.show_full cho nut XEM SHOW FULL

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-05

Một khoá cấu hình, không bảng nào.

Seed để **rỗng**, giống bốn khoá link còn lại của migration 0002: chép một URL cứng vào
migration là dựng lại đúng cái tật mà §13.6.8 mô tả (link game nằm rải 7 chỗ trong source
cũ). Vận hành đặt link trên panel, màn Cấu hình.

Handler đã hỏng-theo-hướng-đóng: link rỗng thì màn hình vẫn hiện chữ nhưng KHÔNG dựng nút.
`InlineKeyboardButton(url="")` bị Telegram từ chối cả tin nhắn, và khi đó người dùng bấm
nút xong không thấy gì.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO settings (key, value, value_type, label_vi, sensitive)
                 VALUES ('link.show_full', '""'::jsonb, 'string',
                         'Link màn XEM SHOW FULL', false)
            ON CONFLICT (key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM settings WHERE key = 'link.show_full'"))
