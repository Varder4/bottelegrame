"""Tra tín hiệu định danh — phần ĐỌC dùng chung cho bot và panel web.

Toàn bộ nội dung file này vốn nằm trong `apps/worker/handlers/admin/identity.py`. Nó đứng
yên được suốt thời gian chỉ có một màn hình đọc. Panel web là màn hình thứ hai, và ở miền
này việc chép lại đắt hơn mọi miền khác: **hai trong ba thứ được chuyển sang đây là hàng
rào quyền riêng tư.**

- `rut_gon_ip()` — chép lệch một ký tự là rò IP thật của người dùng ra một trang web.
- Hàm đọc **trả về IP ĐÃ rút gọn**. Đây là thay đổi có chủ ý so với bản trong handler: ở
  đó câu SQL trả IP đầy đủ rồi template mới rút gọn. Cách đó phụ thuộc vào việc mọi
  template tương lai đều nhớ gọi hàm che — và một template quên gọi thì không có gì báo.
  Ở đây IP đầy đủ **không rời service**.
- `parse_ip()` — chuẩn hoá trước khi so. Quên bước này thì panel báo "không có tài khoản
  nào" trên một IPv6 đang gắn với 40 tài khoản. Lỗi im lặng: không log, không stack trace.

Luật của miền, giữ nguyên từ bản gốc và không được nới ở web:

1. **Không đường nào đi từ tín hiệu ngược về tên của người khác.** Tra theo IP trả
   `user_id` chứ không trả username — muốn biết ai thì mở hồ sơ, và lượt đó để lại dấu.
2. **Chỉ ĐỌC và chỉ ĐO.** Không chặn ai, không chấm điểm ai. `risk_assessments.score`
   chưa có ai ghi; in ra `0` là bịa một con số, tệ hơn hẳn nói thẳng "chưa chấm điểm".
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Số dòng tối đa mỗi khối. Một IP của nhà mạng di động có thể gắn với hàng trăm tài
#: khoản; lấy hết vừa vượt trần Telegram vừa không ai đọc.
LIMIT: Final = 20

#: Từ bao nhiêu tài khoản dùng chung thì đánh dấu. **KHÔNG phải ngưỡng chặn** — không luật
#: nào đọc con số này. Nó chỉ quyết định chỗ nào hiện thêm một dấu cảnh báo.
DANG_CHU_Y: Final = 3


def parse_ip(raw: str) -> str | None:
    """Chuỗi có phải một địa chỉ IP không. Trả về **dạng chuẩn hoá**, hoặc `None`.

    Chuẩn hoá là bắt buộc chứ không phải làm đẹp: `identity_signals.signal_value` lưu chuỗi
    do `ipaddress` sinh ra, nên gõ `2405:4802:0:0::1` mà so thẳng sẽ không khớp dòng đang
    lưu dưới dạng `2405:4802::1`.
    """
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


def rut_gon_ip(value: str) -> str:
    """Che phần cuối địa chỉ. Đủ để đối chiếu hai lượt tra, không đủ để dán đi nơi khác."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(addr, ipaddress.IPv4Address):
        phan = value.split(".")
        return ".".join([*phan[:3], "x"])
    phan = value.split(":")
    return ":".join([*phan[:4], "…"])


def nhan_tin_hieu(signal_type: str, signal_value: str) -> str:
    """Nhãn hiển thị của một tín hiệu. Chỉ loại `ip` mới bị rút gọn."""
    return rut_gon_ip(signal_value) if signal_type == "ip" else signal_value


# ── Truy vấn ────────────────────────────────────────────────────────

#: Tín hiệu của MỘT người, kèm số tài khoản dùng chung lấy sẵn từ `signal_owners`.
_SQL_BY_USER = """
SELECT s.signal_type,
       s.signal_value,
       s.hits,
       s.first_seen,
       s.last_seen,
       coalesce(o.user_count, 1) AS so_tai_khoan
  FROM identity_signals s
  LEFT JOIN signal_owners o
         ON o.signal_type = s.signal_type AND o.signal_value = s.signal_value
 WHERE s.user_id = :uid
 ORDER BY so_tai_khoan DESC, s.last_seen DESC
 LIMIT :lim
"""

#: Các tài khoản từng mang một giá trị tín hiệu. **Không** trả username — xem luật 1.
_SQL_BY_VALUE = """
SELECT s.user_id, s.hits, s.first_seen, s.last_seen,
       (u.verified_at IS NOT NULL) AS da_xac_minh,
       (b.user_id IS NOT NULL)     AS dang_khoa
  FROM identity_signals s
  LEFT JOIN users u ON u.user_id = s.user_id
  LEFT JOIN user_bans b ON b.user_id = s.user_id AND b.unbanned_at IS NULL
 WHERE s.signal_type = :loai AND s.signal_value = :gia_tri
 ORDER BY s.last_seen DESC
 LIMIT :lim
"""

_SQL_OWNER = """
SELECT user_count, first_user_id, last_seen
  FROM signal_owners WHERE signal_type = :loai AND signal_value = :gia_tri
"""

#: Lượt xác minh gần nhất của một người — nguồn duy nhất hiện có cho ASN / quốc gia.
_SQL_EVENTS = """
SELECT event_type, host(ip) AS ip, asn, country, verdict, risk_score, created_at
  FROM verification_events
 WHERE user_id = :uid
 ORDER BY created_at DESC
 LIMIT 5
"""


@dataclass(frozen=True, slots=True)
class TinHieu:
    signal_type: str
    signal_value: str
    #: Nhãn đã che — dùng cái này để hiện, không dùng `signal_value`.
    nhan: str
    hits: int
    first_seen: datetime | None
    last_seen: datetime | None
    so_tai_khoan: int

    @property
    def dang_chu_y(self) -> bool:
        return self.so_tai_khoan >= DANG_CHU_Y


@dataclass(frozen=True, slots=True)
class TaiKhoanChung:
    user_id: int
    hits: int
    first_seen: datetime | None
    last_seen: datetime | None
    da_xac_minh: bool
    dang_khoa: bool


@dataclass(frozen=True, slots=True)
class LuotXacMinh:
    event_type: str
    #: IP **đã rút gọn**. Bản đầy đủ không rời service.
    ip: str | None
    asn: str | None
    country: str | None
    verdict: str
    risk_score: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChuTinHieu:
    user_count: int
    first_user_id: int | None
    last_seen: datetime | None


async def tin_hieu_cua_nguoi(
    db: AsyncSession, user_id: int, *, limit: int = LIMIT
) -> list[TinHieu]:
    rows = (await db.execute(text(_SQL_BY_USER), {"uid": user_id, "lim": limit})).all()
    return [
        TinHieu(
            signal_type=r.signal_type,
            signal_value=r.signal_value,
            nhan=nhan_tin_hieu(r.signal_type, r.signal_value),
            hits=r.hits,
            first_seen=r.first_seen,
            last_seen=r.last_seen,
            so_tai_khoan=r.so_tai_khoan,
        )
        for r in rows
    ]


async def luot_xac_minh(db: AsyncSession, user_id: int) -> list[LuotXacMinh]:
    rows = (await db.execute(text(_SQL_EVENTS), {"uid": user_id})).all()
    return [
        LuotXacMinh(
            event_type=r.event_type,
            # Rút gọn Ở ĐÂY, không ở template: một template mới quên gọi hàm che thì in
            # nguyên IP thật, và không có gì báo lỗi.
            ip=rut_gon_ip(r.ip) if r.ip else None,
            asn=r.asn,
            country=r.country,
            verdict=r.verdict,
            risk_score=r.risk_score,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def tai_khoan_theo_tin_hieu(
    db: AsyncSession, *, loai: str, gia_tri: str, limit: int = LIMIT
) -> list[TaiKhoanChung]:
    rows = (
        await db.execute(text(_SQL_BY_VALUE), {"loai": loai, "gia_tri": gia_tri, "lim": limit})
    ).all()
    return [
        TaiKhoanChung(
            user_id=r.user_id,
            hits=r.hits,
            first_seen=r.first_seen,
            last_seen=r.last_seen,
            da_xac_minh=r.da_xac_minh,
            dang_khoa=r.dang_khoa,
        )
        for r in rows
    ]


async def chu_tin_hieu(db: AsyncSession, *, loai: str, gia_tri: str) -> ChuTinHieu | None:
    row = (await db.execute(text(_SQL_OWNER), {"loai": loai, "gia_tri": gia_tri})).one_or_none()
    if row is None:
        return None
    return ChuTinHieu(
        user_count=row.user_count, first_user_id=row.first_user_id, last_seen=row.last_seen
    )


__all__ = [
    "DANG_CHU_Y",
    "LIMIT",
    "ChuTinHieu",
    "LuotXacMinh",
    "TaiKhoanChung",
    "TinHieu",
    "chu_tin_hieu",
    "luot_xac_minh",
    "nhan_tin_hieu",
    "parse_ip",
    "rut_gon_ip",
    "tai_khoan_theo_tin_hieu",
    "tin_hieu_cua_nguoi",
]
