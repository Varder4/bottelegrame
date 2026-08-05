"""bang trung chuyen anh — de panel web gui tin kem anh

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-05

Một bảng và một khoá cấu hình.

## Vì sao bytes nằm trong database chứ không trên đĩa

Tiến trình web và tiến trình bot là **hai tiến trình hệ điều hành riêng**, và khi lên VPS
chúng có thể ở hai container. Chúng chia sẻ đúng hai thứ: chuỗi kết nối database và chuỗi
kết nối Redis. **Không có volume dùng chung nào được khai ở đâu.**

Chọn đĩa là phát minh ra một hợp đồng hạ tầng thứ ba — đường dẫn, quyền, volume mount —
chưa tồn tại, và nó hỏng đúng vào lúc deploy.

Còn một lý do nữa, nặng hơn: **giao dịch**. Ghi bytes và ghi hàng ý định là MỘT `INSERT`,
một `COMMIT`. Với đĩa đó là hai pha, và mọi lần chết giữa chừng để lại một trong hai — tệp
mồ côi không ai dọn, hoặc một hàng trỏ vào tệp không có. Repo này đã trả giá cho đúng lớp
lỗi hai-pha đó ở `code_grants` mồ côi. Không mở lại nó cho một tấm ảnh.

## Bytes chỉ sống vài giây

`du_lieu` bị đặt `NULL` ngay trong giao dịch ghi `file_id` vào `media_assets`. Thứ tồn tại
lâu dài chỉ là `sha256` và `file_id` — hai chuỗi văn bản. Trần 5 MB mỗi ảnh, vài ảnh mỗi
tuần, nên bytea không phải chuyện đáng bàn về dung lượng.

## Vì sao KHÔNG dùng thẳng `media_assets`

`media_assets.file_id` là `NOT NULL` (migration `0001`), nên nó không chứa nổi trạng thái
"đang chờ tải lên". Nới ràng buộc đó là đổi schema đang chạy để nhét một trạng thái tạm vào
một bảng danh mục vĩnh viễn, và bắt bảng đó mang một cột bytea mãi mãi.

`media_assets` giữ nguyên vai trò: **danh mục ảnh ĐÃ có `file_id`**. `media_uploads` là
phòng chờ.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_uploads",
        sa.Column("upload_id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Bytes của ảnh. NULL sau khi đã có `file_id` — xem docstring.
        sa.Column("du_lieu", postgresql.BYTEA),
        sa.Column("ten_tep", sa.Text, nullable=False),
        sa.Column("kieu_mime", sa.Text, nullable=False),
        sa.Column("so_byte", sa.Integer, nullable=False),
        # Vân tay nội dung. Cùng ảnh tải lại lần hai thì trả `file_id` cũ ngay, không gọi
        # Telegram lần nào — `media_assets.sha256` sinh ra cho đúng việc này.
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        # Khoá trong `media_assets` sau khi tải lên xong.
        sa.Column("asset_key", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        # Giữ chỗ của job đang xử lý, theo đúng khuôn `outbox_messages`: một tiến trình
        # chết giữa chừng thì lease hết hạn và hàng quay lại hàng đợi, không kẹt vĩnh viễn.
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "visible_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.BigInteger, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("state IN ('pending','done','failed')", name="state"),
        sa.CheckConstraint("so_byte > 0", name="so_byte_duong"),
    )

    # Index của câu nhặt việc: chỉ hàng `pending` mới cần tra, nên partial giữ index nhỏ
    # đúng bằng phần đang chờ — nó rỗng gần như mọi lúc.
    op.execute(
        sa.text("""
        CREATE INDEX ix_media_uploads_cho
            ON media_uploads (visible_at)
         WHERE state = 'pending'
        """)
    )
    # Tra theo vân tay để khỏi tải lên lần hai.
    op.execute(sa.text("CREATE INDEX ix_media_uploads_sha ON media_uploads (sha256)"))

    op.get_bind().execute(
        sa.text("""
        INSERT INTO settings (key, value, value_type, label_vi, min_value, max_value,
                              sensitive)
             VALUES ('jobs.media_upload_seconds', '10'::jsonb, 'seconds',
                     'Chu kỳ job tải ảnh của panel lên Telegram (giây)', 5, 60, false)
        ON CONFLICT (key) DO NOTHING
        """)
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM settings WHERE key = 'jobs.media_upload_seconds'"))
    op.drop_table("media_uploads")
