"""Gói model. Import ở đây là thứ ĐĂNG KÝ bảng vào `Base.metadata`.

Thiếu file này thì `televip.db.models` trở thành namespace package: câu
`from televip.db import models` trong `migrations/env.py` vẫn chạy êm, nhưng
`Base.metadata` rỗng và `alembic revision --autogenerate` sinh ra một migration
KHÔNG có bảng nào — hỏng im lặng, không có lỗi nào để thấy.
"""

from __future__ import annotations

from televip.db.models import codes, delivery, engagement, identity  # noqa: F401

__all__ = ["codes", "delivery", "engagement", "identity"]
