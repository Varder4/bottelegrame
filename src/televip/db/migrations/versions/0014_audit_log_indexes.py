"""Index cho màn hình nhật ký.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-05

`audit_log` chỉ có `ix_audit_log_entity(entity_type, entity_id)` — đủ cho câu hỏi "đối
tượng X đã bị làm gì", không đủ cho ba câu mà màn hình nhật ký hỏi: *gần đây có gì*,
*hành động này xảy ra khi nào*, *người này đã làm gì*.

Không thêm index thì mỗi lần bấm lọc là một lần Seq Scan toàn bảng. Và bảng này tăng
**một dòng cho mỗi lượt gõ lệnh có `mutates=True`** — nên nó không đứng yên.

Ba index, mỗi cái cho đúng một câu hỏi. `created_at DESC` trong định nghĩa vì mọi truy vấn
đều `ORDER BY ... DESC`: một index ASC vẫn quét ngược được, nhưng khai đúng chiều thì kế
hoạch truy vấn không cần bước sắp xếp nào.

`ix_audit_log_actor_at` là **partial**: `actor_id` NULL với mọi dòng `actor_type='system'`,
và không ai lọc theo "người thực hiện là NULL". Bỏ chúng ra khỏi index giữ index nhỏ đúng
bằng phần thật sự được tra.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # "Gần đây có gì" — trang đầu của màn hình nhật ký, không lọc gì.
    op.create_index(
        "ix_audit_log_created_at",
        "audit_log",
        ["created_at"],
        postgresql_ops={"created_at": "DESC"},
    )
    # "Hành động này xảy ra khi nào" — lọc theo `action`, vẫn sắp theo thời gian.
    op.create_index(
        "ix_audit_log_action_at",
        "audit_log",
        ["action", "created_at"],
        postgresql_ops={"created_at": "DESC"},
    )
    # "Người này đã làm gì" — bỏ qua dòng của hệ thống.
    op.create_index(
        "ix_audit_log_actor_at",
        "audit_log",
        ["actor_id", "created_at"],
        postgresql_ops={"created_at": "DESC"},
        postgresql_where="actor_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_actor_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action_at", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
