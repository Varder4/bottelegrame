"""Lớp nền của tầng dữ liệu: declarative base, quy ước đặt tên, mixin dùng chung.

Quy ước đặt tên ràng buộc là thứ bắt buộc phải có TRƯỚC khi viết bảng đầu tiên: nếu để
PostgreSQL tự sinh tên, Alembic sẽ không tạo được lệnh `downgrade` cho `CHECK` và
`UNIQUE`, và mọi migration về sau chỉ đi được một chiều.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Tên ràng buộc sinh tự động, ổn định giữa các lần chạy.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - chỉ để debug
        pk = self.__mapper__.primary_key
        vals = ", ".join(f"{c.name}={getattr(self, c.name)!r}" for c in pk)
        return f"<{type(self).__name__} {vals}>"


class TimestampedMixin:
    """`created_at` do database sinh, không do Python sinh.

    Lý do: đồng hồ của tiến trình ứng dụng có thể lệch nhau, còn `now()` của Postgres
    là một nguồn thời gian duy nhất cho mọi worker. Luôn là `TIMESTAMPTZ`.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        # KHÔNG index mặc định. Bảng nào thật sự truy vấn theo `created_at` thì tự khai
        # index trong `__table_args__` của nó. Đánh index cho mọi bảng nghe có vẻ vô hại
        # nhưng `outbox_messages` và `points_ledger` là hai bảng ghi nóng nhất hệ thống —
        # mỗi index thừa ở đó là chi phí trả trên từng lượt INSERT, đổi lại không có
        # truy vấn nào dùng tới.
    )


class UpdatedAtMixin:
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def user_fk_column(*, primary_key: bool = False, nullable: bool = False) -> Mapped[int]:
    """Cột trỏ tới `users.user_id`.

    Luôn là `BIGINT`: `user_id` của Telegram đã vượt ngưỡng 2^31, dùng `INTEGER`
    là lỗi tràn số trong tương lai gần.
    """
    from sqlalchemy import ForeignKey

    return mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        primary_key=primary_key,
        nullable=nullable,
    )
