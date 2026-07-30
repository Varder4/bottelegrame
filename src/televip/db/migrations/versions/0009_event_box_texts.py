"""cau chu event dap hop cham tran ngan sach

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-30

Migration **dữ liệu**, không phải schema — viết tay, không autogenerate.

Khối event đập hộp (§13.2.9) dùng lại toàn bộ cấu hình đã seed ở `0002`
(`event.prize_table`, `event.window_minutes`, `event.budget_cap_vnd`,
`event.require_full_stock`) và quyền `/send_event` đã seed ở `0003`. Thứ duy nhất còn
thiếu là **một khoá câu chữ mới**: `event.box_budget_capped`.

Vì sao nó phải tồn tại như một câu riêng, không dùng lại "hộp rỗng": §13.5.1 điểm 3 nói
chạm trần ngân sách là điều **duy nhất** được phép làm lệch tỉ lệ đã quảng cáo, nên nó
phải hiện ra thành thông báo chứ không được giấu trong code. Trả lại đúng câu "hộp rỗng"
khi xác suất trúng thật đã bằng 0 là nói dối người dùng — đúng loại lỗi mà cả khối này
sinh ra để xoá.

Nội dung nhập từ `televip.services.text_service.TEMPLATE_SEED`, cùng lý do đã ghi ở
`0005`: bản gốc sống trong `domain/texts.py`, dòng trong bảng chỉ là bản ghi đè cho admin.
`ON CONFLICT DO NOTHING` để chạy lại không xoá bản admin vừa sửa.

Thiếu dòng này bot vẫn chạy đúng — `text_service.render()` rơi về bản mặc định trong code.
Nó tồn tại để câu đó **sửa được** bằng `/suanoidung` như 54 câu còn lại.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from televip.services.text_service import TEMPLATE_SEED

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Khoá câu chữ mới của khối này.
NEW_KEYS: tuple[str, ...] = ("event.box_budget_capped",)

_INSERT = """
INSERT INTO message_templates (key, content, label_vi, required_vars)
     VALUES (:key, :content, :label_vi, CAST(:required_vars AS jsonb))
ON CONFLICT (key) DO NOTHING
"""


def _rows() -> list[dict[str, str]]:
    seed = {row["key"]: row for row in TEMPLATE_SEED}
    missing = [key for key in NEW_KEYS if key not in seed]
    if missing:
        # Nổ lúc chạy migration, không phải lúc có người bấm nút: khoá bị đổi tên trong
        # code mà migration còn giữ tên cũ là một dòng seed lặng lẽ không bao giờ khớp.
        raise RuntimeError(f"khoá câu chữ không có trong TEMPLATE_SEED: {missing}")
    return [
        {
            "key": key,
            "content": seed[key]["content"],
            "label_vi": seed[key]["label_vi"],
            "required_vars": json.dumps(seed[key]["required_vars"], ensure_ascii=False),
        }
        for key in NEW_KEYS
    ]


def upgrade() -> None:
    conn = op.get_bind()
    for row in _rows():
        conn.execute(sa.text(_INSERT), row)


def downgrade() -> None:
    conn = op.get_bind()
    # `message_templates_audit` KHÔNG bị đụng tới: nó append-only, và lịch sử ai từng sửa
    # câu này vẫn phải đọc được sau khi hạ migration.
    conn.execute(
        sa.text("DELETE FROM message_templates WHERE key = ANY(:keys)"), {"keys": list(NEW_KEYS)}
    )
