"""Tồn kho code — đếm, ngưỡng cảnh báo, và các con số tổng.

Cho tới bản này, toàn bộ phần trên nằm trong `apps/worker/handlers/admin/codes.py`, tức
trong tầng trình bày của Telegram. Nó đứng yên được vì chỉ có **một** màn hình đọc nó.
Panel web là màn hình thứ hai, và hai màn hình cùng đọc một con số qua hai đoạn mã khác
nhau là hai con số sẽ lệch nhau — thường là vào lúc một trong hai được sửa và người sửa
không biết cái kia tồn tại.

`render_stock()` (câu chữ cho Telegram) **ở lại** bên handler: nó là cách nói, không phải
con số. Cái chuyển vào đây là cách ĐẾM.

## Vì sao đếm thẳng trên `codes`

Có sẵn bảng `code_pool_stats` trông như bảng đếm sẵn. Nó **rỗng**: không trigger nào nuôi
nó (migration `0001` tạo bảng, và không có nơi nào trong mã ghi vào). Đọc nó là báo cáo
toàn số 0 cho người vận hành — im lặng và trông hoàn toàn bình thường. Khi nào có trigger
thật thì đổi đúng `_SQL_STOCK` dưới đây.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.services import settings_service

# ── Ngưỡng cảnh báo ─────────────────────────────────────────────────
#
# ⚠️ Có HAI khoá cùng nghĩa: yêu cầu của khối kho đặt tên `stock.warn_threshold`, còn
# §13.6.7 đã khai `alert.low_code_threshold` = 50 và migration 0002 **đã seed nó**. Chỉ khoá
# đã seed mới sửa được bằng `/setcauhinh` (`settings_service.set()` từ chối khoá không tồn
# tại). Nên thứ tự đọc là: khoá mới → khoá đã seed → 50, để cái nút vặn hiện có vẫn thật sự
# vặn được. Gộp hai khoá làm một là việc của người chốt đặc tả.
STOCK_WARN_KEY: Final = "stock.warn_threshold"
LOW_CODE_KEY: Final = "alert.low_code_threshold"
DEFAULT_WARN_THRESHOLD: Final = 50


_SQL_STOCK = """
SELECT code_type,
       value_vnd,
       count(*) FILTER (WHERE status = 'available') AS con_lai,
       count(*) FILTER (WHERE status = 'issued')    AS da_phat,
       count(*) FILTER (WHERE status = 'reserved')  AS giu_cho
  FROM codes
 GROUP BY code_type, value_vnd
 ORDER BY code_type, value_vnd
"""


@dataclass(frozen=True, slots=True)
class StockRow:
    code_type: str
    value_vnd: int
    available: int
    issued: int
    reserved: int

    @property
    def value_available_vnd(self) -> int:
        """Tiền còn nằm trong kho ở mệnh giá này."""
        return self.available * self.value_vnd

    def low(self, threshold: int) -> bool:
        return self.available < threshold


@dataclass(frozen=True, slots=True)
class StockTotals:
    """Các con số ở chân bảng. Định nghĩa **một lần**, cho cả Telegram lẫn web."""

    available: int
    issued: int
    reserved: int
    value_vnd: int
    #: Số mệnh giá đang dưới ngưỡng — đếm theo DÒNG, không theo số mã.
    low_count: int


async def read_stock(db: AsyncSession) -> list[StockRow]:
    """Tồn kho theo (loại code × mệnh giá). Một truy vấn, đã sắp xếp sẵn để hiện."""
    rows = (await db.execute(text(_SQL_STOCK))).all()
    return [
        StockRow(
            code_type=row.code_type,
            value_vnd=row.value_vnd,
            available=row.con_lai,
            issued=row.da_phat,
            reserved=row.giu_cho,
        )
        for row in rows
    ]


def summarize(rows: Sequence[StockRow], *, threshold: int) -> StockTotals:
    """Cộng dồn các dòng tồn kho.

    Cộng ở Python chứ không thêm một câu `SUM()` thứ hai: tổng phải bằng đúng tổng của
    những dòng người ta đang NHÌN. Một truy vấn tổng riêng chạy ở thời điểm khác sẽ lệch
    với bảng ngay bên trên nó — và không ai đọc được vì sao.
    """
    return StockTotals(
        available=sum(r.available for r in rows),
        issued=sum(r.issued for r in rows),
        reserved=sum(r.reserved for r in rows),
        value_vnd=sum(r.value_available_vnd for r in rows),
        low_count=sum(1 for r in rows if r.low(threshold)),
    )


async def warn_threshold() -> int:
    """Ngưỡng cảnh báo tồn kho thấp — xem ghi chú ở `STOCK_WARN_KEY`."""
    seeded = await settings_service.get_int(LOW_CODE_KEY, DEFAULT_WARN_THRESHOLD)
    return await settings_service.get_int(STOCK_WARN_KEY, seeded)


__all__ = [
    "DEFAULT_WARN_THRESHOLD",
    "LOW_CODE_KEY",
    "STOCK_WARN_KEY",
    "StockRow",
    "StockTotals",
    "read_stock",
    "summarize",
    "warn_threshold",
]
