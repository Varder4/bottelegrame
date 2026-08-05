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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.db.models.codes import CODE_TYPES
from televip.domain import texts
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


# ══════════════════════════════════════════════════════════════════════
# Nạp kho
# ══════════════════════════════════════════════════════════════════════
#
# Nạp kho là hành động **tạo ra tiền**: 1.000 mã mệnh giá 500.000đ là nửa tỉ đồng nghĩa vụ
# sinh ra bằng một lần bấm. Bốn hàng rào dưới đây vốn nằm rải trong handler Telegram, tức
# mọi đường vào không-phải-Telegram đều đi vòng qua chúng.
#
# Gói cả bốn vào MỘT hàm để không đường vào nào bỏ sót được, kể cả một script chạy tay.


#: Khoá cấu hình ngưỡng duyệt hai người, và đường lui khi `settings` chưa seed.
DUAL_APPROVAL_KEY: Final = "admin.dual_approval_threshold_vnd"
DEFAULT_DUAL_APPROVAL_VND: Final = 1_000_000

_VALUE_RE: Final = re.compile(r"^(\d+)([kK])?$")


class NapKhoBiTuChoi(ValueError):
    """Lô không nạp được. `args[0]` là câu để hiện thẳng cho người vận hành."""


class VuotNguongDuyetHaiNguoi(NapKhoBiTuChoi):
    """Lô vượt ngưỡng cần chữ ký thứ hai.

    Là lớp con của `NapKhoBiTuChoi` để nơi gọi bắt chung được, nhưng có kiểu riêng vì đây
    là hàng rào **tiền**, không phải một lỗi nhập liệu — và màn hình cần nói khác hẳn.
    """

    def __init__(self, *, batch_value_vnd: int, threshold_vnd: int, so_ma: int) -> None:
        self.batch_value_vnd = batch_value_vnd
        self.threshold_vnd = threshold_vnd
        self.so_ma = so_ma
        super().__init__(
            f"lô trị giá {batch_value_vnd} đ ({so_ma} mã), vượt ngưỡng cần duyệt hai người "
            f"là {threshold_vnd} đ"
        )


def doc_menh_gia(raw: str) -> int | None:
    """`10000` · `10.000` · `10k` · `88K` → số VNĐ. `None` nếu không đọc được.

    Bốn dạng vì tài liệu viết cả `{value_k}k` lẫn `10.000`, và người vận hành sẽ gõ cả hai.
    Một lần gõ đúng-theo-tài-liệu mà hệ thống từ chối là lý do người ta quay lại sửa thẳng
    database.
    """
    cleaned = raw.replace(".", "").replace(",", "").replace("_", "")
    match = _VALUE_RE.match(cleaned.strip())
    if match is None:
        return None
    value = int(match.group(1))
    if match.group(2):
        value *= 1_000
    return value or None


def tach_ma(raw: str) -> list[str]:
    """Chuỗi nhiều mã (xuống dòng, dấu phẩy, khoảng trắng) → danh sách đã khử trùng.

    Khử trùng **giữ thứ tự** và **phân biệt hoa thường**: mã quà tặng là chuỗi do nhà cung
    cấp sinh, và `ABC123` với `abc123` có thể là hai mã khác nhau. Gộp chúng là tự ý vứt
    một mã có giá trị tiền.
    """
    tho = raw.replace(",", "\n").replace("\t", "\n").split()
    da_thay: set[str] = set()
    ket_qua: list[str] = []
    for ma in tho:
        if ma and ma not in da_thay:
            da_thay.add(ma)
            ket_qua.append(ma)
    return ket_qua


@dataclass(frozen=True, slots=True)
class LoNapKho:
    """Lô đã qua đủ bốn hàng rào và sẵn sàng ghi vào kho."""

    code_type: str
    value_vnd: int
    ma: list[str]
    #: Số mã bị loại vì trùng ngay trong chuỗi vừa dán, đếm TRƯỚC khi ghi.
    trung_trong_lo: int
    batch_value_vnd: int
    threshold_vnd: int


async def kiem_lo_nap(
    *, code_type: str, menh_gia_tho: str, ma_tho: str, db: AsyncSession | None = None
) -> LoNapKho:
    """Bốn hàng rào của một lô nạp, theo đúng thứ tự của bot.

    Ném:
        NapKhoBiTuChoi: loại code lạ, mệnh giá không đọc được / ngoài danh sách / sai loại,
            hoặc lô rỗng.
        VuotNguongDuyetHaiNguoi: lô vượt ngưỡng.
        ConfigError: thiếu khoá cấu hình mệnh giá.
    """
    if code_type not in CODE_TYPES:
        raise NapKhoBiTuChoi(
            f"Loại code không hợp lệ: {code_type}. Hợp lệ: {', '.join(CODE_TYPES)}"
        )

    value_vnd = doc_menh_gia(menh_gia_tho)
    if value_vnd is None:
        raise NapKhoBiTuChoi(f"Mệnh giá không đọc được: {menh_gia_tho}. Dùng dạng 10k hoặc 10000.")

    # Hai khoá này do migration seed và KHÔNG có default viết trong code: một giá trị mặc
    # định ở đây sẽ âm thầm thay thế luật thật khi seed hỏng, và chỗ nó thay thế là câu trả
    # lời "mệnh giá nào được nạp" — tức là tiền.
    allowed_values = await settings_service.get_list("code.allowed_values", db=db)
    category_values = await settings_service.get_dict("code.category_values", db=db)

    if value_vnd not in allowed_values:
        raise NapKhoBiTuChoi(
            f"Mệnh giá {texts.format_vnd(value_vnd)}đ không nằm trong danh sách cho phép. "
            f"Hợp lệ: {', '.join(texts.value_label(v) for v in allowed_values)}"
        )

    per_type = category_values.get(code_type)
    if per_type is not None and value_vnd not in per_type:
        raise NapKhoBiTuChoi(
            f"Loại {code_type} không dùng mệnh giá {texts.format_vnd(value_vnd)}đ. "
            f"Hợp lệ cho loại này: {', '.join(texts.value_label(v) for v in per_type)}"
        )

    tho = ma_tho.replace(",", "\n").replace("\t", "\n").split()
    ma = tach_ma(ma_tho)
    if not ma:
        raise NapKhoBiTuChoi("Không có mã nào để nạp.")

    # Ngưỡng duyệt hai người. Khử trùng TRƯỚC rồi mới tính giá trị lô: tính trên chuỗi thô
    # là tính trên một tập không có thật, và ngưỡng sẽ chặn nhầm một lô hợp lệ có mã lặp.
    batch_value = len(ma) * value_vnd
    threshold = await settings_service.get_int(DUAL_APPROVAL_KEY, DEFAULT_DUAL_APPROVAL_VND, db=db)
    if batch_value > threshold:
        raise VuotNguongDuyetHaiNguoi(
            batch_value_vnd=batch_value, threshold_vnd=threshold, so_ma=len(ma)
        )

    return LoNapKho(
        code_type=code_type,
        value_vnd=value_vnd,
        ma=ma,
        trung_trong_lo=len(tho) - len(ma),
        batch_value_vnd=batch_value,
        threshold_vnd=threshold,
    )


@dataclass(frozen=True, slots=True)
class KetQuaNap:
    """Kết quả một lần nạp kho.

    ``skipped`` gộp cả hai loại trùng — trùng với kho và trùng ngay trong lô vừa dán — vì
    người vận hành chỉ cần biết "gõ N mã, vào kho M".
    """

    #: `None` khi không mã nào vào kho (cả lô đều trùng) — không có lô nào để thu hồi.
    batch_id: int | None
    added: int
    skipped: int
    samples: list[str]
    stock_after: int


#: §13.4.2 mục 1: hiện "10 code đầu" để người vận hành đối chiếu với file nguồn.
SAMPLE_LIMIT: Final = 10


async def nap(
    db: AsyncSession,
    lo: LoNapKho,
    *,
    actor_id: int,
    ip: str | None = None,
) -> KetQuaNap:
    """Ghi một lô **đã qua `kiem_lo_nap()`** vào kho và ghi `audit_log`.

    Gọi trong `transaction()` của nơi gọi. Nhận `LoNapKho` chứ không nhận chuỗi thô: kiểu
    đó chỉ `kiem_lo_nap()` dựng được, nên **không đường vào nào ghi được vào kho mà không
    đi qua bốn hàng rào**. Đây là chỗ chặn bằng kiểu, không bằng lời dặn.

    `ON CONFLICT (code_value) DO NOTHING` là toàn bộ cơ chế chống trùng với kho: hỏi trước
    rồi chèn sau để lại một khe giữa hai câu lệnh, và hai người dán cùng một danh sách
    trong khe đó sẽ cùng thấy "chưa có".
    """
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from televip.db.models.codes import Code
    from televip.services import code_issuance
    from televip.services.admin import write_audit

    stmt = (
        pg_insert(Code)
        .values(
            [
                {
                    "code_value": value,
                    "code_type": lo.code_type,
                    "value_vnd": lo.value_vnd,
                    "created_by": actor_id,
                }
                for value in lo.ma
            ]
        )
        .on_conflict_do_nothing(index_elements=["code_value"])
        .returning(Code.code_id, Code.code_value)
    )
    inserted = (await db.execute(stmt)).all()

    added = len(inserted)
    skipped = len(lo.ma) - added + lo.trung_trong_lo

    # `batch_id` = code_id nhỏ nhất của lô. Gán ở câu thứ hai vì giá trị đó chỉ biết được
    # SAU khi `ON CONFLICT` đã lọc xong mã trùng.
    batch_id = min(row.code_id for row in inserted) if inserted else None
    if batch_id is not None:
        await db.execute(
            update(Code)
            .where(Code.code_id.in_([row.code_id for row in inserted]))
            .values(batch_id=batch_id)
        )

    stock_after = await code_issuance.available_count(
        db, code_type=lo.code_type, value_vnd=lo.value_vnd
    )

    await write_audit(
        db,
        actor_id=actor_id,
        action="add_giffcode",
        entity_type="code_batch",
        entity_id=None if batch_id is None else str(batch_id),
        after={
            "code_type": lo.code_type,
            "value_vnd": lo.value_vnd,
            "requested": len(lo.ma) + lo.trung_trong_lo,
            "added": added,
            "skipped": skipped,
            # Giá trị THẬT SỰ vào kho, không phải giá trị lô đã gõ: mã trùng không sinh
            # nghĩa vụ nào. Sổ này là chỗ đối soát tiền, nên nó phải đếm cái đã ghi.
            "total_value_vnd": added * lo.value_vnd,
            "ip": ip,
        },
    )
    return KetQuaNap(
        batch_id=batch_id,
        added=added,
        skipped=skipped,
        samples=[row.code_value for row in inserted[:SAMPLE_LIMIT]],
        stock_after=stock_after,
    )


__all__ = [
    "KetQuaNap",
    "SAMPLE_LIMIT",
    "nap",
    "DEFAULT_DUAL_APPROVAL_VND",
    "DUAL_APPROVAL_KEY",
    "LoNapKho",
    "NapKhoBiTuChoi",
    "VuotNguongDuyetHaiNguoi",
    "doc_menh_gia",
    "kiem_lo_nap",
    "tach_ma",
    "DEFAULT_WARN_THRESHOLD",
    "LOW_CODE_KEY",
    "STOCK_WARN_KEY",
    "StockRow",
    "StockTotals",
    "read_stock",
    "summarize",
    "warn_threshold",
]
