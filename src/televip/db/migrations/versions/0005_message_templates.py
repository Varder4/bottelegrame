"""message templates

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30

Bảng cho **câu chữ sửa được từ admin**, cùng vai trò với `settings` nhưng cho văn bản:
chủ bot đổi một câu bằng `/suanoidung`, có hiệu lực trong 60 giây, không deploy.

Ba điều cần biết trước khi sửa file này:

1. **Seed nhập từ `televip.services.text_service.TEMPLATE_SEED`, cố ý.** Ngược với
   `0003_seed_admin` — nó đông cứng bản đồ quyền vì bản đồ đó là *luật*, phải giống nhau
   trên mọi database. Ở đây dữ liệu seed là *bản sao* của câu chữ mặc định đang nằm trong
   `domain/texts.py`, và bản gốc thì vẫn phải sống trong code: `render()` rơi về nó khi
   bảng thiếu dòng, và `/resetnoidung` khôi phục từ chính nó. Gõ lại 54 đoạn văn tiếng
   Việt vào đây là tạo nguồn sự thật thứ hai cho cùng một câu — thứ chắc chắn trôi khác đi.
   Hệ quả chấp nhận được: database cài mới nhận câu chữ **hiện tại**, database cũ giữ câu
   chữ lúc nó chạy migration. Cả hai đều đúng, vì dòng trong bảng chỉ là bản ghi đè.

2. **`ON CONFLICT DO NOTHING`**: chạy lại migration trên database đã có dữ liệu không được
   xoá bản admin vừa sửa.

3. **Quyền của bốn lệnh mới nạp luôn ở đây.** Thiếu hàng trong `admin_permissions` là lệnh
   bị từ chối với tất cả mọi người (`services/admin.py: can_run`) — bảng đã tạo mà không
   ai gõ được lệnh nào.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from televip.services.text_service import TEMPLATE_SEED

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: lệnh → vai trò được chạy. Xem được thì cả ba vai trò vận hành; SỬA thì chỉ `owner`:
#: một câu chữ sai làm hỏng trải nghiệm của toàn bộ người dùng cùng lúc, ngang một lần đổi
#: cấu hình, nên nó đi cùng nhóm quyền với `/setcauhinh`.
COMMAND_ROLES: dict[str, tuple[str, ...]] = {
    "/noidung": ("owner", "admin", "cskh"),
    "/xemnoidung": ("owner", "admin", "cskh"),
    "/suanoidung": ("owner",),
    "/resetnoidung": ("owner",),
}


def _permissions_rows() -> list[dict[str, str]]:
    return [
        {"role": role, "command": command}
        for command, roles in COMMAND_ROLES.items()
        for role in roles
    ]


def upgrade() -> None:
    op.create_table(
        "message_templates",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("label_vi", sa.Text(), nullable=False),
        sa.Column(
            "required_vars",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_message_templates")),
    )
    op.create_table(
        "message_templates_audit",
        sa.Column("audit_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("old_content", sa.Text(), nullable=True),
        sa.Column("new_content", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_id", name=op.f("pk_message_templates_audit")),
    )
    op.create_index(
        "ix_message_templates_audit_key_at",
        "message_templates_audit",
        ["key", "changed_at"],
        unique=False,
    )

    conn = op.get_bind()
    conn.execute(
        sa.text("""
        INSERT INTO message_templates (key, content, label_vi, required_vars)
             VALUES (:key, :content, :label_vi, CAST(:required_vars AS jsonb))
        ON CONFLICT (key) DO NOTHING
        """),
        [
            {
                "key": row["key"],
                "content": row["content"],
                "label_vi": row["label_vi"],
                "required_vars": json.dumps(row["required_vars"], ensure_ascii=False),
            }
            for row in TEMPLATE_SEED
        ],
    )
    conn.execute(
        sa.text("""
        INSERT INTO admin_permissions (role, command) VALUES (:role, :command)
        ON CONFLICT DO NOTHING
        """),
        _permissions_rows(),
    )


def downgrade() -> None:
    table = sa.table(
        "admin_permissions",
        sa.column("role", sa.Text),
        sa.column("command", sa.Text),
    )
    # Xoá theo **cặp** (role, command), không `DELETE` trần: quyền do admin tự thêm sau này
    # không được cuốn theo.
    pairs = [(row["role"], row["command"]) for row in _permissions_rows()]
    op.execute(table.delete().where(sa.tuple_(table.c.role, table.c.command).in_(pairs)))

    op.drop_index("ix_message_templates_audit_key_at", table_name="message_templates_audit")
    op.drop_table("message_templates_audit")
    op.drop_table("message_templates")
