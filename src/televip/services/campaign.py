"""Chiến dịch mời bạn — mở, gia hạn, dừng.

Toàn bộ nội dung file này vốn nằm trong `apps/worker/handlers/admin/campaign.py`. Nó đứng
yên được suốt thời gian chỉ có một đường vào. Panel web là đường vào thứ hai, và ở miền
này việc chép lại đắt hơn hầu hết miền khác: **cái được chuyển sang đây không phải ba câu
SQL, mà là một THỨ TỰ.**

## Thứ tự bắt buộc, và vì sao chép hụt nó là mất tiền

Mở một chiến dịch gồm ba bước, và chúng phải chạy đúng thứ tự này trong **một** giao dịch:

    1. KHOÁ (`pg_advisory_xact_lock`)
    2. DỪNG MỌI chiến dịch đang bật
    3. MỞ cái mới

Bỏ bước 1: dưới `READ COMMITTED`, giao dịch của admin B chụp ảnh bảng **trước** khi hàng
mới của A tồn tại, nên nó không tắt được hàng đó, rồi B chèn tiếp hàng của mình. Kết quả là
**hai chiến dịch cùng bật**.

Và hai hàng cùng bật thì không ai thấy: `referral.campaign_window()` chỉ đọc
`ORDER BY campaign_id DESC LIMIT 1`, nên hàng cũ trở thành **bật nhưng vô hình** — nó vẫn
phát thưởng sau khi người vận hành đã đọc "đã dừng". Mỗi người mời đủ ăn tối đa
`max_claims × reward_value_vnd`; trên tệp 19.151 người đó là một đường chi không ai nhìn ra.

Bỏ bước 2, hoặc đảo 2 và 3: cùng hậu quả.

## Gia hạn cộng dồn từ hạn CŨ

`ends_at + N ngày`, không phải `now() + N ngày`. Gia hạn 7 ngày cho một chiến dịch còn 3
ngày phải ra 10 ngày; tính từ `now()` là âm thầm **cắt ngắn** 3 ngày đang có.

## Tham số là ẢNH CHỤP, không phải luật

`interval_people`, `reward_value_vnd`, `max_claims` chép từ `settings` vào hàng chiến dịch
tại thời điểm mở. Đó là ảnh chụp để đối soát về sau — luật đang chạy vẫn đọc từ `settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.logging import get_logger
from televip.services import referral

log = get_logger(__name__)

#: Trần số ngày một lần mở hoặc gia hạn. Không phải luật nghiệp vụ mà là hàng rào gõ nhầm:
#: `/chiendich start 3650` là mười năm phát thưởng liên tục.
#:
#: Giữ đúng con số đang chạy trong handler Telegram. Đổi nó ở đây là đổi hành vi của một
#: lệnh đang dùng, và đó là một quyết định riêng chứ không phải hệ quả của việc dời chỗ.
MAX_DAYS: Final = 365

#: Không gian tên khoá tư vấn — `0x4344` = "CD". Khác `0x4556` ("EV", trần ngân sách event)
#: và `0x4243` ("BC", bơm đợt bắn tin), để ba thứ không xếp hàng sau nhau.
LOCK_NS: Final = 0x4344

_SQL_LOCK = "SELECT pg_advisory_xact_lock(:ns, 0)"

_SQL_RUNNING = """
SELECT campaign_id, code, name, starts_at, ends_at,
       interval_people, reward_value_vnd, max_claims
  FROM campaigns
 WHERE is_active
   AND (starts_at IS NULL OR starts_at <= now())
   AND ends_at IS NOT NULL
   AND ends_at > now()
 ORDER BY campaign_id DESC
 LIMIT 1
"""

#: Tắt MỌI chiến dịch đang bật, không chỉ cái mới nhất. Để sót một hàng bật ở giữa bảng
#: nghĩa là người vận hành bấm "dừng", đọc được "đã dừng", mà van tiền vẫn mở.
_SQL_END_ALL = "UPDATE campaigns SET is_active = false WHERE is_active RETURNING campaign_id"

#: `code` có `UNIQUE`, nên nó phải duy nhất **theo cấu trúc**, không theo may mắn. Một dấu
#: thời gian tới giây thì hai lần bấm liên tiếp — chuyện người vận hành làm thật khi tưởng
#: lần đầu không ăn — nổ `UniqueViolation`, một lỗi không nhánh nào bắt.
_SQL_START = """
WITH so_moi AS (
  SELECT nextval(pg_get_serial_sequence('campaigns', 'campaign_id')) AS id
)
INSERT INTO campaigns
       (campaign_id, code, name, interval_people, reward_value_vnd, max_claims,
        starts_at, ends_at, is_active)
SELECT so_moi.id,
       'cd-' || to_char(now() AT TIME ZONE 'Asia/Ho_Chi_Minh', 'YYYYMMDD') || '-' || so_moi.id,
       :name, :interval_people, :reward_value_vnd, :max_claims,
       now(), now() + make_interval(days => :days), true
  FROM so_moi
RETURNING campaign_id, code, ends_at
"""

_SQL_EXTEND = """
UPDATE campaigns
   SET ends_at = ends_at + make_interval(days => :days)
 WHERE campaign_id = :cid
RETURNING ends_at
"""


class SoNgayKhongHopLe(ValueError):
    """Số ngày ngoài khoảng cho phép."""

    def __init__(self) -> None:
        super().__init__(f"số ngày phải là số nguyên từ 1 đến {MAX_DAYS}")


class KhongCoChienDichDangChay(ValueError):
    """Gia hạn khi không có chiến dịch nào đang chạy.

    Gia hạn một chiến dịch đã hết hạn là **mở lại van tiền** — thao tác khác hẳn về bản
    chất với "kéo dài cái đang mở", nên nó phải đi qua `mo()`.
    """

    def __init__(self) -> None:
        super().__init__("không có chiến dịch nào đang chạy để gia hạn")


def doc_so_ngay(raw: str) -> int:
    """Chuỗi → số ngày trong `[1, MAX_DAYS]`. Ném `SoNgayKhongHopLe`.

    `isdecimal` chứ không `isdigit`: `"²".isdigit()` là True nhưng `int("²")` ném.
    """
    token = raw.strip()
    if not token.isdecimal():
        raise SoNgayKhongHopLe
    days = int(token)
    if not 1 <= days <= MAX_DAYS:
        raise SoNgayKhongHopLe
    return days


@dataclass(frozen=True, slots=True)
class ChienDichMoi:
    campaign_id: int
    code: str
    ends_at: datetime
    days: int
    #: Những chiến dịch vừa bị dừng để nhường chỗ. Danh sách này phải được HIỆN RA: người
    #: vận hành cần biết mình vừa tắt cái gì.
    da_dung: list[int]
    interval_people: int
    reward_value_vnd: int
    max_claims: int


async def mo(
    db: AsyncSession, *, days: int, name: str, actor_id: int, ip: str | None = None
) -> ChienDichMoi:
    """Mở một chiến dịch mới. Ba bước, đúng thứ tự, trong giao dịch của nơi gọi.

    ``db`` phải là session GHI. Khoá tư vấn giữ tới hết giao dịch, nên nơi gọi **không
    được** commit giữa chừng.
    """
    from televip.services.admin import write_audit

    rule = await referral.params(db=db)

    # 1. Xếp hàng TRƯỚC khi đọc bảng — xem docstring module.
    await db.execute(text(_SQL_LOCK), {"ns": LOCK_NS})
    # 2. Dừng MỌI cái đang bật.
    da_dung = [r.campaign_id for r in (await db.execute(text(_SQL_END_ALL))).all()]
    # 3. Mở cái mới.
    created = (
        await db.execute(
            text(_SQL_START),
            {
                "name": name,
                "interval_people": rule.interval,
                "reward_value_vnd": rule.reward_value_vnd,
                "max_claims": rule.max_claims,
                "days": days,
            },
        )
    ).one()

    await write_audit(
        db,
        actor_id=actor_id,
        action="chiendich_start",
        entity_type="campaigns",
        entity_id=str(created.campaign_id),
        before={"da_dung": da_dung} if da_dung else None,
        after={"code": created.code, "name": name, "days": days, "ip": ip},
    )
    log.warning(
        "chien_dich_mo",
        actor_id=actor_id,
        campaign_id=created.campaign_id,
        days=days,
        code=created.code,
        da_dung=da_dung,
    )
    return ChienDichMoi(
        campaign_id=created.campaign_id,
        code=created.code,
        ends_at=created.ends_at,
        days=days,
        da_dung=da_dung,
        interval_people=rule.interval,
        reward_value_vnd=rule.reward_value_vnd,
        max_claims=rule.max_claims,
    )


@dataclass(frozen=True, slots=True)
class ChienDichGiaHan:
    campaign_id: int
    code: str
    name: str
    ends_at_cu: datetime
    ends_at_moi: datetime
    days: int


async def gia_han(
    db: AsyncSession, *, days: int, actor_id: int, ip: str | None = None
) -> ChienDichGiaHan:
    """Kéo dài chiến dịch ĐANG CHẠY. Cộng dồn từ hạn cũ.

    Ném:
        KhongCoChienDichDangChay: không có cái nào đang chạy.
    """
    from televip.services.admin import write_audit

    await db.execute(text(_SQL_LOCK), {"ns": LOCK_NS})
    row = (await db.execute(text(_SQL_RUNNING))).one_or_none()
    if row is None:
        raise KhongCoChienDichDangChay

    moi = (await db.execute(text(_SQL_EXTEND), {"cid": row.campaign_id, "days": days})).scalar_one()

    await write_audit(
        db,
        actor_id=actor_id,
        action="chiendich_extend",
        entity_type="campaigns",
        entity_id=str(row.campaign_id),
        before={"ends_at": row.ends_at.isoformat()},
        after={"ends_at": moi.isoformat(), "days": days, "ip": ip},
    )
    log.warning("chien_dich_gia_han", actor_id=actor_id, campaign_id=row.campaign_id, days=days)
    return ChienDichGiaHan(
        campaign_id=row.campaign_id,
        code=row.code,
        name=row.name,
        ends_at_cu=row.ends_at,
        ends_at_moi=moi,
        days=days,
    )


async def dung(db: AsyncSession, *, actor_id: int, ip: str | None = None) -> list[int]:
    """Dừng MỌI chiến dịch đang bật. Trả danh sách id đã dừng — rỗng nghĩa là không có cái nào.

    Dừng *mọi* cái chứ không chỉ cái mới nhất: một hàng bật còn sót lại là van tiền vẫn mở
    sau khi người vận hành đã đọc "đã dừng".
    """
    from televip.services.admin import write_audit

    await db.execute(text(_SQL_LOCK), {"ns": LOCK_NS})
    da_dung = [r.campaign_id for r in (await db.execute(text(_SQL_END_ALL))).all()]
    if not da_dung:
        return []

    await write_audit(
        db,
        actor_id=actor_id,
        action="chiendich_end",
        entity_type="campaigns",
        entity_id=",".join(str(x) for x in da_dung),
        after={"da_dung": da_dung, "ip": ip},
    )
    log.warning("chien_dich_dung", actor_id=actor_id, da_dung=da_dung)
    return da_dung


__all__ = [
    "LOCK_NS",
    "MAX_DAYS",
    "ChienDichGiaHan",
    "ChienDichMoi",
    "KhongCoChienDichDangChay",
    "SoNgayKhongHopLe",
    "doc_so_ngay",
    "dung",
    "gia_han",
    "mo",
]
