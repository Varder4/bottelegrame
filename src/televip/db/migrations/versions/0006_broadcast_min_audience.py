"""nguong so dich toi thieu cua broadcast va quyen /broadcast_cancel

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-30

Migration **dữ liệu**, không phải schema — viết tay, không autogenerate.

Hai dòng, cả hai đều là điều kiện để khối lệnh `/broadcast` chạy đúng:

1. `settings['broadcast.min_audience']` — số đích tối thiểu để một đợt được phép chạy.
   Dưới ngưỡng này thì lệnh **từ chối** thay vì bắn: một đợt chỉ có vài đích gần như luôn
   là bộ lọc sai (chưa ai `/start` sau khi đổi bot, hoặc gõ nhầm audience), chứ không phải
   "tệp thật chỉ có ngần ấy người". Muốn gửi thật cho nhóm nhỏ thì gõ `--force-small`.

   Seed **1** chứ không phải một con số lớn: ở dev tệp thật chỉ có vài tài khoản test, và
   một ngưỡng cao seed sẵn sẽ chặn mọi lần thử. Nâng nó lên trước khi mở cho người thật là
   một dòng `/setcauhinh`, không phải một migration.

2. `admin_permissions('owner', '/broadcast_cancel')` — `0003_seed_admin` đã seed
   `/broadcast`, `/broadcast_pause`, `/broadcast_resume`, `/broadcast_status` nhưng chưa có
   lệnh huỷ. Thiếu dòng này thì `can_run()` từ chối, và cách duy nhất để dừng một đợt đang
   chạy là tắt tiến trình — đúng thứ lệnh huỷ sinh ra để thay thế.

Cả hai câu đều `ON CONFLICT DO NOTHING`: chạy lại migration trên database đã có sẵn dòng
đó không được ghi đè giá trị mà vận hành đã chỉnh bằng `/setcauhinh`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING_KEY = "broadcast.min_audience"
SEED_VALUE = 1

CANCEL_COMMAND = "/broadcast_cancel"
CANCEL_ROLE = "owner"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
        INSERT INTO settings (key, value, value_type, label_vi, min_value, max_value, sensitive)
             VALUES (:key, CAST(:value AS jsonb), 'int', :label, 0, 1000000, false)
        ON CONFLICT (key) DO NOTHING
        """),
        {
            "key": SETTING_KEY,
            "value": str(SEED_VALUE),
            "label": "Số đích tối thiểu để một đợt broadcast được phép chạy",
        },
    )
    conn.execute(
        sa.text("""
        INSERT INTO admin_permissions (role, command)
             VALUES (:role, :command)
        ON CONFLICT DO NOTHING
        """),
        {"role": CANCEL_ROLE, "command": CANCEL_COMMAND},
    )


def downgrade() -> None:
    conn = op.get_bind()
    # `settings_audit` KHÔNG bị đụng tới: nó append-only, và lịch sử ai từng đổi ngưỡng này
    # vẫn phải đọc được sau khi hạ migration.
    conn.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": SETTING_KEY})
    conn.execute(
        sa.text("DELETE FROM admin_permissions WHERE role = :role AND command = :command"),
        {"role": CANCEL_ROLE, "command": CANCEL_COMMAND},
    )
