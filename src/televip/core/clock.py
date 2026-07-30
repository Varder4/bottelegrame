"""Thời gian — một nơi duy nhất.

Bot cũ dùng `datetime.now()` naive theo giờ máy chủ. Nếu VPS chạy UTC thì "ngày" của
người dùng lệch 7 tiếng: điểm danh lúc 23h30 giờ VN bị tính sang hôm sau và đứt streak
vô cớ. Ở đây:

- Mọi mốc thời gian lưu vào DB là **UTC có timezone** (`TIMESTAMPTZ`).
- Mọi khái niệm "ngày" của nghiệp vụ (điểm danh, streak, bảng xếp hạng ngày) dùng
  `business_date()` theo giờ Việt Nam.

CẤM gọi `datetime.now()`, `date.today()`, `datetime.utcnow()` ở bất kỳ đâu ngoài file này —
có kiểm tra tự động ở `scripts/check_architecture.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

VN_TZ: tzinfo = ZoneInfo("Asia/Ho_Chi_Minh")
UTC: tzinfo = UTC


def now_utc() -> datetime:
    """Thời điểm hiện tại, UTC, có timezone. Đây là hàm DUY NHẤT đọc đồng hồ hệ thống."""
    return datetime.now(UTC)


def now_vn() -> datetime:
    """Thời điểm hiện tại theo giờ Việt Nam — chỉ dùng để hiển thị cho người dùng."""
    return now_utc().astimezone(VN_TZ)


def business_date(moment: datetime | None = None) -> date:
    """Ngày nghiệp vụ theo giờ Việt Nam.

    Dùng cho điểm danh, streak, bảng xếp hạng theo ngày. Một người bấm điểm danh lúc
    23h30 giờ VN và một người bấm lúc 00h30 hôm sau phải rơi vào hai ngày khác nhau,
    bất kể máy chủ đặt múi giờ nào.
    """
    moment = moment or now_utc()
    if moment.tzinfo is None:
        raise ValueError("business_date() cần datetime có timezone — không nhận naive datetime")
    return moment.astimezone(VN_TZ).date()


def vn_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Khoảng [đầu ngày, đầu ngày kế) của một ngày VN, trả về theo UTC.

    Dùng cho truy vấn kiểu ``WHERE created_at >= $1 AND created_at < $2`` — dạng này
    tận dụng được index, khác với ``WHERE date(created_at) = $1`` vốn phải quét toàn bảng.
    """
    start_vn = datetime.combine(day, datetime.min.time(), tzinfo=VN_TZ)
    return start_vn.astimezone(UTC), (start_vn + timedelta(days=1)).astimezone(UTC)


def is_consecutive_day(previous: date | None, current: date) -> bool:
    """Hai ngày nghiệp vụ có liền nhau không — dùng để quyết định streak tăng hay reset."""
    if previous is None:
        return False
    return (current - previous).days == 1
