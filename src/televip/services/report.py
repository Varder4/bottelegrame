"""Báo cáo chi theo kỳ — phần TÍNH, dùng chung cho bot và panel web.

Toàn bộ nội dung file này vốn nằm trong `apps/worker/handlers/admin/report.py`. Phần câu
chữ (`render()`) **ở lại** bên đó: đó là cách nói. Cái chuyển sang đây là cách ĐẾM, cách
CẮT KỲ và cách XUẤT FILE — ba thứ mà hai màn hình cùng cần và không được lệch nhau.

Ba luật, giữ nguyên từ bản gốc:

1. **Mọi con số đếm trên `code_grants` với `state = 'delivered'`.** Không đếm trên `codes`
   (kho không biết ai nhận), không đếm trên bộ đếm tổng của `users` (bộ đếm là bản sao
   hiển thị và nó đã trôi khỏi sự thật ở hệ cũ). `delivered` chứ không `reserved`: một mã
   giữ chỗ mà gửi hỏng sẽ quay về kho, tính nó là đã chi là báo cáo THỪA tiền — và một báo
   cáo thừa tiền chạm trần ngân sách giả rồi đóng event sớm.

2. **Ranh giới kỳ tính theo NGÀY NGHIỆP VỤ giờ VN**, qua `vn_day_bounds`. Cắt kỳ theo UTC
   đẩy 7 tiếng cao điểm mỗi ngày (17h–24h giờ VN) sang kỳ sau — cùng cái bẫy đã làm streak
   điểm danh của bot cũ lệch một ngày.

3. **`to_csv` xuất đúng những dòng vừa hiện trên màn hình**, không phải một truy vấn thứ
   hai: hai truy vấn cho cùng một báo cáo là hai con số khác nhau ngay khi có ai đó nhận
   code giữa hai lần chạy, và người đọc không có cách nào biết bản nào đúng.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import vn_day_bounds

#: Kỳ → số ngày lùi lại tính cả hôm nay. `tuan` là 7 ngày gần nhất chứ không phải "tuần
#: theo lịch": câu hỏi vận hành luôn là "bảy ngày qua tiêu bao nhiêu", và một báo cáo
#: chạy sáng thứ Hai mà chỉ có dữ liệu của một ngày là vô dụng.
PERIODS: Final[dict[str, tuple[int, str]]] = {
    "ngay": (1, "HÔM NAY"),
    "tuan": (7, "7 NGÀY QUA"),
    "thang": (30, "30 NGÀY QUA"),
}

DEFAULT_PERIOD: Final = "ngay"

#: Chi tiết theo (loại phát × mệnh giá). `grant_type` là loại trong SỔ CÁI, không phải
#: loại trong kho — đó là thứ trả lời "tiền đi vào luồng nào".
_SQL_BY_TYPE = """
SELECT grant_type,
       value_vnd,
       count(*)::int                   AS so_ma,
       coalesce(sum(value_vnd), 0)::bigint AS tong_vnd
  FROM code_grants
 WHERE state = 'delivered'
   AND delivered_at >= :tu AND delivered_at < :den
 GROUP BY grant_type, value_vnd
 ORDER BY tong_vnd DESC, grant_type, value_vnd
"""

_SQL_TOTALS = """
SELECT
  (SELECT count(*)::int FROM code_grants
    WHERE state = 'delivered' AND delivered_at >= :tu AND delivered_at < :den) AS so_ma,
  (SELECT coalesce(sum(value_vnd), 0)::bigint FROM code_grants
    WHERE state = 'delivered' AND delivered_at >= :tu AND delivered_at < :den) AS tong_vnd,
  (SELECT count(DISTINCT user_id)::int FROM code_grants
    WHERE state = 'delivered' AND delivered_at >= :tu AND delivered_at < :den) AS so_nguoi,
  (SELECT count(*)::int FROM users
    WHERE joined_at >= :tu AND joined_at < :den)                               AS user_moi,
  -- Tử số phải cùng TỆP với mẫu số: người VÀO trong kỳ và đã xác minh. Đếm
  -- `verified_at` trong kỳ là một tệp khác hẳn (gồm cả người vào từ tháng trước),
  -- và tỉ lệ giữa hai tệp khác nhau in ra được những con số như 300%.
  (SELECT count(*)::int FROM users
    WHERE joined_at >= :tu AND joined_at < :den AND verified_at IS NOT NULL)   AS da_xac_minh,
  -- Số lượt xác minh XẢY RA trong kỳ, bất kể vào từ bao giờ. Đây là con số vận hành
  -- ("hôm nay có bao nhiêu người xác minh"), không phải tử số của tỉ lệ trên.
  (SELECT count(*)::int FROM users
    WHERE verified_at >= :tu AND verified_at < :den)                           AS xac_minh_trong_ky
"""

#: Chi của các đợt event nằm TRONG kỳ. Trần ngân sách là trần MỖI ĐỢT, nên báo cáo phải
#: tách được từng đợt chứ không chỉ đưa một con số gộp.
#:
#: Hai con số cho mỗi đợt, và chúng trả lời hai câu hỏi khác nhau:
#: - `*_ky` — chi TRONG KỲ, cùng ranh giới với con số "💰 Đã chi" ở đầu báo cáo. Không
#:   cùng ranh giới thì hai khối trên cùng một màn hình cộng ra hai tổng khác nhau và
#:   không có gì trên đó giải thích tại sao.
#: - `*_doi` — chi TRỌN ĐỜI của đợt, thứ phải đem so với trần ngân sách mỗi đợt. Một đợt
#:   mở cuối kỳ trước, tiêu tiếp sang kỳ này, có phần trong kỳ nhỏ mà tổng đã sát trần.
_SQL_EVENTS = """
SELECT e.event_id,
       e.created_at,
       count(g.grant_id) FILTER (
         WHERE g.delivered_at >= :tu AND g.delivered_at < :den)::int          AS so_ma_ky,
       coalesce(sum(g.value_vnd) FILTER (
         WHERE g.delivered_at >= :tu AND g.delivered_at < :den), 0)::bigint   AS tong_vnd_ky,
       count(g.grant_id)::int                                                 AS so_ma_doi,
       coalesce(sum(g.value_vnd), 0)::bigint                                  AS tong_vnd_doi
  FROM events e
  LEFT JOIN event_participations p ON p.event_id = e.event_id
  LEFT JOIN code_grants g ON g.grant_id = p.code_grant_id AND g.state = 'delivered'
 WHERE e.created_at >= :tu AND e.created_at < :den
 GROUP BY e.event_id, e.created_at
 ORDER BY e.event_id
"""


@dataclass(frozen=True, slots=True)
class TongKy:
    so_ma: int
    tong_vnd: int
    so_nguoi: int
    user_moi: int
    #: Người VÀO trong kỳ và đã xác minh — tử số của tỉ lệ chuyển đổi.
    da_xac_minh: int
    #: Lượt xác minh XẢY RA trong kỳ, bất kể vào từ bao giờ. **Không** phải tử số của tỉ lệ
    #: trên: hai tệp khác nhau chia cho nhau in ra được những con số như 300%.
    xac_minh_trong_ky: int


@dataclass(frozen=True, slots=True)
class DongChi:
    grant_type: str
    value_vnd: int
    so_ma: int
    tong_vnd: int


@dataclass(frozen=True, slots=True)
class DongEvent:
    event_id: int
    created_at: datetime
    #: Chi TRONG KỲ — cùng ranh giới với con số tổng ở đầu báo cáo.
    so_ma_ky: int
    tong_vnd_ky: int
    #: Chi TRỌN ĐỜI của đợt — con số phải đem so với trần ngân sách mỗi đợt. Một đợt mở
    #: cuối kỳ trước, tiêu tiếp sang kỳ này, có phần trong kỳ nhỏ mà tổng đã sát trần.
    so_ma_doi: int
    tong_vnd_doi: int


@dataclass(frozen=True, slots=True)
class Report:
    period: str
    label: str
    tu: datetime
    den: datetime
    tong: TongKy
    dong: list[DongChi]
    events: list[DongEvent]


def parse_args(args: list[str]) -> tuple[str, bool] | None:
    """`(kỳ, có xuất csv)`, hoặc `None` nếu có token không hiểu.

    Không đoán bừa: một token lạ thường là admin gõ nhầm tên kỳ, và im lặng chạy kỳ mặc
    định sẽ trả về một báo cáo trông hợp lệ cho một khoảng thời gian khác với ý định.
    """
    period = DEFAULT_PERIOD
    xuat_csv = False
    for token in args:
        low = token.strip().lower()
        if low in PERIODS:
            period = low
        elif low == "csv":
            xuat_csv = True
        elif low:
            return None
    return period, xuat_csv


def period_bounds(period: str, today: date) -> tuple[datetime, datetime]:
    """Khoảng UTC của kỳ, cắt theo ranh giới ngày nghiệp vụ giờ VN."""
    days, _ = PERIODS[period]
    tu, _ = vn_day_bounds(today - timedelta(days=days - 1))
    _, den = vn_day_bounds(today)
    return tu, den


async def collect(db: AsyncSession, *, period: str, today: date) -> Report:
    """Ba truy vấn của một kỳ, gói thành một `Report`."""
    tu, den = period_bounds(period, today)
    params = {"tu": tu, "den": den}
    t = (await db.execute(text(_SQL_TOTALS), params)).one()
    dong = (await db.execute(text(_SQL_BY_TYPE), params)).all()
    events = (await db.execute(text(_SQL_EVENTS), params)).all()
    return Report(
        period=period,
        label=PERIODS[period][1],
        tu=tu,
        den=den,
        tong=TongKy(
            so_ma=t.so_ma,
            tong_vnd=t.tong_vnd,
            so_nguoi=t.so_nguoi,
            user_moi=t.user_moi,
            da_xac_minh=t.da_xac_minh,
            xac_minh_trong_ky=t.xac_minh_trong_ky,
        ),
        dong=[
            DongChi(
                grant_type=r.grant_type,
                value_vnd=r.value_vnd,
                so_ma=r.so_ma,
                tong_vnd=r.tong_vnd,
            )
            for r in dong
        ],
        events=[
            DongEvent(
                event_id=r.event_id,
                created_at=r.created_at,
                so_ma_ky=r.so_ma_ky,
                tong_vnd_ky=r.tong_vnd_ky,
                so_ma_doi=r.so_ma_doi,
                tong_vnd_doi=r.tong_vnd_doi,
            )
            for r in events
        ],
    )


def to_csv(report: Report) -> bytes:
    """Đúng những dòng vừa hiện trên màn hình, không phải một truy vấn thứ hai.

    UTF-8 **có BOM**: Excel bản Việt mở CSV UTF-8 không BOM thành chữ vỡ, và người nhận
    báo cáo này mở bằng Excel chứ không mở bằng trình soạn thảo.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ky", "tu", "den", "nhom", "loai", "menh_gia_vnd", "so_ma", "tong_vnd"])
    khoang = [report.period, report.tu.isoformat(), report.den.isoformat()]
    for r in report.dong:
        writer.writerow([*khoang, "luong", r.grant_type, r.value_vnd, r.so_ma, r.tong_vnd])
    for r in report.events:
        writer.writerow(
            [*khoang, "event_trong_ky", f"event_{r.event_id}", "", r.so_ma_ky, r.tong_vnd_ky]
        )
        writer.writerow(
            [*khoang, "event_tron_doi", f"event_{r.event_id}", "", r.so_ma_doi, r.tong_vnd_doi]
        )
    return buf.getvalue().encode("utf-8-sig")


__all__ = [
    "DEFAULT_PERIOD",
    "PERIODS",
    "DongChi",
    "DongEvent",
    "Report",
    "TongKy",
    "collect",
    "parse_args",
    "period_bounds",
    "to_csv",
]
