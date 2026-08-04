"""dang nhap web cho admin: ten dang nhap, mat khau bam, va phien

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-05

Chu du an chot: **admin thao tac hoan toan tren web**, bot chi con la san pham cho nguoi
dung. Dang nhap bang ten tai khoan + mat khau nhu moi trang quan tri thong thuong.

## Vi sao them COT vao `admin_users` chu khong tao bang nguoi dung moi

`admin_users` x `admin_permissions` la **nguon quyen duy nhat** cua he thong — co bai kiem
tra khoa hai chieu (`tests/test_registration.py`) va mot bai chan viec dung `ADMIN_GROUP_ID`
de cap quyen. De ra mot bang `web_users` rieng nghia la co hai su that ve "ai la admin", va
hai su that thi som muon cung lech nhau. Luc lech, mot trong hai se noi rang mot nguoi da
bi thu hoi quyen van con quyen.

Nen: `login_name` va `password_hash` la hai COT tren chinh bang do. Thu hoi quyen bang
`/admin_del` (dat `revoked_at`) lam mat luon duong dang nhap web — mot thao tac, mot su that.

## Vi sao `login_name` rieng chu khong dung `users.username`

`users.username` la @username Telegram, nguoi dung TU DOI duoc bat cu luc nao. Neu no la
ten dang nhap thi admin doi username tren Telegram la mat quyen vao panel, va khong co
dong log nao giai thich. `login_name` do chinh admin dat va khong troi theo Telegram.

## Vi sao KHONG them thu vien bam mat khau

`hashlib.scrypt` nam san trong thu vien chuan Python, la KDF duoc thiet ke dung cho viec
nay (cham co chu dich, ton bo nho, chong may dao ASIC). Them `argon2-cffi` hay `bcrypt`
chi de lam viec ma stdlib da lam tot la them mot thu nua phai cap nhat sau sau thang —
dung van hoa it phu thuoc ma repo nay dang giu.

Tham so va muoi duoc nhung ngay trong chuoi bam (`scrypt$n$r$p$muoi$bam`), nen nang tham
so sau nay khong lam hong mat khau cu: ban ghi cu van doc duoc bang tham so cua chinh no.

## `admin_sessions`

`session_id` la **bam SHA-256 cua gia tri cookie**, khong phai chinh gia tri. Dump database
lot ra ngoai thi khong co phien song nao trong do.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("admin_users", sa.Column("login_name", sa.Text(), nullable=True))
    op.add_column("admin_users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # UNIQUE nhung cho phep NULL: admin chua duoc dat mat khau thi chua co ten dang nhap,
    # va Postgres coi moi NULL la khac nhau nen nhieu dong NULL van hop le.
    op.create_unique_constraint("uq_admin_users_login_name", "admin_users", ["login_name"])

    op.create_table(
        "admin_sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("ua_hash", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name=op.f("fk_admin_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_admin_sessions")),
    )
    # Truy van nong nhat: "phien nao cua nguoi nay con song" — dung khi thu hoi quyen.
    op.create_index(
        "ix_admin_sessions_alive",
        "admin_sessions",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_alive", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_constraint("uq_admin_users_login_name", "admin_users", type_="unique")
    op.drop_column("admin_users", "password_changed_at")
    op.drop_column("admin_users", "password_hash")
    op.drop_column("admin_users", "login_name")
