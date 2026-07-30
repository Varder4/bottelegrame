"""Lệnh admin. Mỗi nhóm nghiệp vụ một module, không có `admin.py` khổng lồ như hệ cũ.

Khung của gói, và ba luật mọi module trong đây phải tuân theo:

1. **Mọi handler được bọc bằng `@admin_command("/ten_lenh")`.** Không có handler admin
   nào tự kiểm quyền lấy: một bản sao thứ hai của luật phân quyền là một chỗ nữa để sai,
   và nó sẽ sai vào đúng lúc không ai nhìn.
2. **Quyền chỉ đến từ `admin_users` × `admin_permissions`** (`15-quyet-dinh-xay-moi.md`
   D14). Không module nào trong gói này được đọc id nhóm cảnh báo, và không nhánh `if`
   nào được cấp quyền theo `chat_id` của tin nhắn.
3. **Gửi tin chỉ qua `bot_data["sender"]`** — `telegram/sender.py` là cửa duy nhất được
   gọi Bot API.

Bản thân `admin_command` sống ở `televip.services.admin` chứ không ở đây: nó chạm database
và không phụ thuộc vào tầng handler, nên nó là dịch vụ. Gói này chỉ dùng lại.
"""

from __future__ import annotations

from televip.services.admin import Handler, admin_command

__all__ = ["Handler", "admin_command"]
