"""Bí danh tương thích — **không có hiện thực nào ở đây**.

Khối 2 từng dựng một `admin_command` tạm trong file này vì lúc đó `televip.services.admin`
chưa có. Nay nó có rồi, và bản tạm đã bị gỡ: giữ hai hiện thực của cùng một hàng rào quyền
là cách chắc chắn nhất để hai bên trôi khác nhau — bản tạm **trả lời** người không có quyền
còn bản thật cố ý **im lặng** (xem docstring `televip.services.admin`, mục "Vì sao từ chối
thì IM LẶNG"), tức hai luật bảo mật khác nhau cùng chạy trong một bot.

File này còn tồn tại chỉ vì `ops.py` đã import theo đường cũ. **Code mới import từ
`televip.apps.worker.handlers.admin`** (hoặc thẳng từ `televip.services.admin`); khi
`ops.py` đổi import xong thì xoá file này.
"""

from __future__ import annotations

from televip.services.admin import Handler, admin_command

__all__ = ["Handler", "admin_command"]
