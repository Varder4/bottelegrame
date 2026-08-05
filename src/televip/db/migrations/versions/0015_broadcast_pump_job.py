"""chu ky job bom dot ban tin — de panel web bam Gui duoc

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-05

Một khoá cấu hình duy nhất: `jobs.broadcast_pump_seconds`.

## Vì sao có khoá này

Panel web không giữ kết nối Telegram nào, nên nó không gửi được. Nó chỉ ghi ý định vào
database: lật một đợt từ `draft` sang `running`. Ai đó phải nhặt lên và bơm.

Trước bản này chỉ có `resume_running_jobs()` chạy **một lần lúc khởi động** tiến trình bot.
Nghĩa là một đợt do web bắt đầu sẽ đứng im — tệp đích đầy, không một dòng `outbox_messages`
nào, không một tin nào bay, và **không một dòng log lỗi nào** — cho tới lần restart bot kế
tiếp, lúc đó cả đợt bỗng bắn đi, có thể là 3 giờ sáng.

Khoá này là chu kỳ của một job định kỳ trong tiến trình bot gọi đúng hàm đó. Nó biến độ trễ
từ "tới lần restart sau" thành "tối đa một chu kỳ".

## Vì sao 15 giây, và vì sao trần 60

15 giây là độ trễ người vận hành chấp nhận được giữa lúc bấm Gửi và lúc tin đầu tiên bay,
và đủ thưa để `SELECT job_id FROM broadcast_jobs WHERE state='running'` (một truy vấn trên
cột đã lọc, thường trả 0 dòng) không đáng kể.

Trần 60 là luật đã có của panel: "hiệu lực cấu hình tối đa 60 giây". Đặt chu kỳ bơm cao hơn
trần đó nghĩa là một đợt có thể chờ lâu hơn cả thời gian một khoá cấu hình lan ra — người
vận hành sẽ kết luận đợt hỏng.

Sàn 5 giây: thấp hơn nữa là một truy vấn mỗi vài giây suốt ngày cho một bảng hầu như luôn
rỗng, và không mua thêm gì — vòng bơm đã tự chạy liên tục khi có việc.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (key, value, value_type, label_vi, min_value, max_value)
SETTINGS_SEED: tuple[tuple[str, Any, str, str, int | None, int | None], ...] = (
    (
        "jobs.broadcast_pump_seconds",
        15,
        "seconds",
        "Chu kỳ job bơm đợt bắn tin đang chạy (giây)",
        5,
        60,
    ),
)


def upgrade() -> None:
    # Khuôn giống hệt `0010`: `ON CONFLICT DO NOTHING` để chạy lại trên database đã có dòng
    # đó không ghi đè giá trị mà vận hành đã chỉnh bằng `/setcauhinh` hay bằng panel.
    conn = op.get_bind()
    for key, value, value_type, label, min_value, max_value in SETTINGS_SEED:
        conn.execute(
            sa.text("""
            INSERT INTO settings (key, value, value_type, label_vi, min_value, max_value,
                                  sensitive)
                 VALUES (:key, CAST(:value AS jsonb), :value_type, :label,
                         :min_value, :max_value, false)
            ON CONFLICT (key) DO NOTHING
            """),
            {
                "key": key,
                # JSONB nhận chuỗi có nháy kép; số thì không có nháy.
                "value": f'"{value}"' if isinstance(value, str) else str(value),
                "value_type": value_type,
                "label": label,
                "min_value": min_value,
                "max_value": max_value,
            },
        )


def downgrade() -> None:
    # `settings_audit` KHÔNG bị đụng tới: nó append-only, và lịch sử ai từng đổi khoá này
    # vẫn phải trả lời được sau khi khoá bị gỡ.
    op.get_bind().execute(sa.text("DELETE FROM settings WHERE key = 'jobs.broadcast_pump_seconds'"))
