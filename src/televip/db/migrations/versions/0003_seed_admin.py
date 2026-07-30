"""seed admin permissions

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30 14:30:00.000000

Migration **dữ liệu**, không phải schema — viết tay, không autogenerate.

Nạp bảng `admin_permissions`: bản đồ **vai trò → lệnh** của `13-dac-ta-bot-moi.md` §13.4.2
(15 lệnh kế thừa) và §13.4.3 (13 lệnh bổ sung). Đây là chỗ quyết định D14 trở thành dữ
liệu: luật phân quyền nằm trong bảng, đổi được bằng lệnh admin, không phải một dict Python
phải deploy lại mới sửa được.

Ba điều cần biết trước khi sửa file này:

1. **Bản đồ dưới đây là ảnh chụp ĐÔNG CỨNG, không phải cấu hình đang chạy.** Nó cố ý
   không import từ `televip.services.admin`: một migration lấy hằng số từ code ứng dụng sẽ
   âm thầm đổi nghĩa mỗi lần hằng số đó đổi, và database cài mới sẽ khác database đã nâng
   cấp. Thêm lệnh mới ⇒ viết migration mới, không sửa file này.
2. **KHÔNG seed `admin_users`.** Không một `user_id` nào được hard-code vào lịch sử
   schema: id đó sẽ sống mãi trong repo, và ai đọc được repo là biết chính xác phải chiếm
   tài khoản nào. Người đầu tiên thêm bằng `scripts/add_admin.py`.
3. **Bảng rỗng nghĩa là mọi lệnh admin bị từ chối** (`services/admin.py: can_run`). Hỏng
   theo hướng an toàn, nhưng vẫn là hỏng — nên migration này là điều kiện để lệnh chạy.

`owner` có **tất cả** các lệnh. `cskh` chỉ lệnh đọc cộng `/done_event` (trao tay code event
chia sẻ sau khi xác minh bài share). `ketoan` chỉ lệnh đọc báo cáo và tồn kho.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ALL_ROLES: tuple[str, ...] = ("owner", "admin", "cskh", "ketoan")

#: lệnh → các vai trò được chạy. Cột "Quyền" của §13.4.2 và §13.4.3, chép nguyên.
#:
#: `/codes used` KHÔNG có dòng riêng: `used` là **tham số** của `/codes`, không phải lệnh
#: thứ hai — Telegram giao cho handler chuỗi `/codes` với `args=['used']`. Đặc tả đếm nó
#: thành hai vì đó là hai màn hình; phân quyền chỉ có một cửa để canh.
COMMAND_ROLES: dict[str, tuple[str, ...]] = {
    # ── §13.4.2 — 15 lệnh kế thừa từ bot cũ ─────────────────────────
    "/add_giffcode": ("owner", "admin"),
    "/del_code": ("owner", "admin"),
    "/del_all_code": ("owner",),
    "/resend_tanthu": ("owner", "admin"),
    "/stats": _ALL_ROLES,
    "/users": ("owner", "admin", "cskh"),
    "/codes": ("owner", "admin", "ketoan"),
    "/broadcast": ("owner",),
    "/send_event": ("owner", "admin"),
    "/update_share_event": ("owner", "admin"),
    "/show_share_event": ("owner", "admin", "cskh"),
    "/done_event": ("owner", "admin", "cskh"),
    "/checkip": ("owner", "admin"),
    "/help_admin": _ALL_ROLES,
    "/huongdan": _ALL_ROLES,
    # ── §13.4.3 — 13 lệnh bổ sung ───────────────────────────────────
    "/tonkho": ("owner", "admin", "ketoan"),
    "/cauhinh": ("owner", "admin"),
    "/setcauhinh": ("owner",),
    "/baocao": ("owner", "ketoan"),
    "/user": ("owner", "admin", "cskh"),
    "/ban": ("owner",),
    "/unban": ("owner",),
    "/admin_add": ("owner",),
    "/admin_del": ("owner",),
    "/broadcast_pause": ("owner",),
    "/broadcast_resume": ("owner",),
    "/broadcast_status": ("owner",),
    "/chiendich": ("owner",),
}


def seed_rows() -> list[dict[str, str]]:
    """Bản đồ trên, trải phẳng thành từng hàng `(role, command)`.

    Thứ tự đi theo `COMMAND_ROLES` để hai lần chạy sinh ra cùng một thứ tự chèn — có ích
    khi so hai database bằng dump.
    """
    return [
        {"role": role, "command": command}
        for command, roles in COMMAND_ROLES.items()
        for role in roles
    ]


def _permissions_table() -> sa.TableClause:
    """Bảng rút gọn cho INSERT/DELETE.

    Cố ý không dùng model ORM: migration phải chạy được với schema **tại thời điểm nó được
    viết ra**, còn model thì trôi theo phiên bản mới nhất của code.
    """
    return sa.table(
        "admin_permissions",
        sa.column("role", sa.Text),
        sa.column("command", sa.Text),
    )


def delete_seeded() -> sa.Delete:
    """Câu lệnh `downgrade()` chạy. Tách ra thành hàm để test kiểm được đúng nó.

    Xoá theo **cặp (role, command)** chứ không `DELETE FROM admin_permissions` trần: một
    quyền do admin tự thêm sau này, hoặc do migration sau nạp vào, không được cuốn theo.
    """
    table = _permissions_table()
    pairs = [(row["role"], row["command"]) for row in seed_rows()]
    return table.delete().where(sa.tuple_(table.c.role, table.c.command).in_(pairs))


def upgrade() -> None:
    op.bulk_insert(_permissions_table(), seed_rows())


def downgrade() -> None:
    op.execute(delete_seeded())

    # `admin_users` KHÔNG bị đụng tới: migration này không tạo ra hàng nào ở đó, và xoá
    # danh bạ admin khi hạ một migration dữ liệu là cách chắc chắn nhất để tự khoá mình ra
    # ngoài hệ thống. `audit_log` cũng vậy — nó append-only.
