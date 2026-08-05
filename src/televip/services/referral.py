"""Mời bạn bè — động cơ tăng trưởng, và chỗ hệ cũ vừa mất tiền vừa mất lòng người dùng.

Đặc tả: `13-dac-ta-bot-moi.md` §13.2.4 (luật tính và phát mốc), §13.2.14 (màn hình tiến
độ), §13.5 dòng A và F.

Bốn quyết định trong file này, mỗi cái chặn một lỗi đã đo được ở hệ cũ:

1. **Referral chỉ tính khi người được mời ĐÃ XÁC MINH.** Điều kiện đó nằm ngay trong câu
   `DELETE ... RETURNING` chứ không ở một câu `if` phía trên: nếu tính ngay lúc bấm link
   thì phát tán link cho hàng nghìn tài khoản rác là ăn thưởng, còn nếu kiểm bằng `if` rồi
   mới xoá thì hai worker vẫn cùng đọc được một ý định.
2. **Tiêu thụ ý định là MỘT câu lệnh.** Hệ cũ đọc `users.pending_referrer_id` rồi mới
   `UPDATE ... SET NULL` ở hai bước tách rời, nên hai request `/verify` song song cùng ghi
   một dòng referral cho một người.
3. **Mốc thưởng dùng `>=`, tuyệt đối không dùng `% interval == 0`.** Hệ cũ lấy modulo trên
   một số đếm đọc ngoài transaction: số nhảy 4→6 là người mời **không bao giờ chạm mốc 5
   và mất code vĩnh viễn**, trong khi màn hình "Check Chia sẻ" vẫn bảo họ đủ điều kiện.
   Ở đây `tier_dat_duoc = min(qualified // interval, max_claims)` rồi phát bù **mọi mốc
   chưa có bản ghi** — bấm muộn không mất mốc, mời 12 người nhận đủ 2 mốc.
4. **Số đã nhận đọc từ `referral_rewards`, không suy ra từ số người mời.** `claimed` và
   `qualified // interval` lệch nhau ngay khi có một referral bị từ chối vì rủi ro
   (§13.5 dòng A).

**Chiến dịch hết hạn thì NGỪNG phát thưởng** (§13.5 dòng F). Đây là chỗ đảo ngược một
yêu cầu cũ của `07-db.md` — xem `campaign_window()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.errors import AlreadyClaimed
from televip.core.ids import grant_key_referral_tier
from televip.core.logging import get_logger
from televip.services import code_issuance, settings_service
from televip.services.code_issuance import Grant

log = get_logger(__name__)

#: Loại code trong KHO và loại phát trong SỔ CÁI — hai khái niệm khác nhau, hai hằng riêng
#: (cùng quy ước với `handlers/tanthu.py`).
CODE_TYPE: Final = "moibanbe"
GRANT_TYPE: Final = "referral_milestone"

#: Mặc định của §13.6.3, chỉ dùng khi bảng `settings` thiếu khoá (database mới dựng, hoặc
#: test vừa TRUNCATE). Giá trị đang chạy luôn đọc từ `settings_service`.
DEFAULT_INTERVAL: Final = 5
DEFAULT_MAX_CLAIMS: Final = 10
DEFAULT_REWARD_VALUE_VND: Final = 10_000
DEFAULT_MAX_RISK_SCORE: Final = 70
DEFAULT_REQUIRE_VERIFIED: Final = True


@dataclass(frozen=True, slots=True)
class ReferralParams:
    """Ba con số của luật thưởng. Một nguồn duy nhất cho **mọi** màn hình (§13.5 dòng A)."""

    interval: int
    max_claims: int
    reward_value_vnd: int

    @property
    def max_people(self) -> int:
        return self.interval * self.max_claims


@dataclass(frozen=True, slots=True)
class CampaignWindow:
    """Cửa sổ thời gian của chiến dịch đang chạy.

    `remaining_seconds` là **tổng giây**, không phải `timedelta.days`/`.seconds` tách rời:
    với timedelta âm thì `.days` âm còn `.seconds` **vẫn dương**, nên bot cũ hiện
    "Còn 0 ngày N giờ" vĩnh viễn cho một chiến dịch đã hết hạn từ tám tháng trước
    (§13.5 dòng F).
    """

    is_running: bool
    remaining_seconds: int


@dataclass(frozen=True, slots=True)
class Progress:
    """Ba con số của §13.2.14, đọc trong **một** lượt để màn hình không tự mâu thuẫn."""

    qualified: int
    claimed: int
    total_vnd: int


async def params(*, db: AsyncSession | None = None) -> ReferralParams:
    """Luật thưởng đang chạy. Không con số nào hardcode ở tầng gọi."""
    return ReferralParams(
        interval=await settings_service.get_int("referral.interval", DEFAULT_INTERVAL, db=db),
        max_claims=await settings_service.get_int("referral.max_claims", DEFAULT_MAX_CLAIMS, db=db),
        reward_value_vnd=await settings_service.get_int(
            "referral.reward_value_vnd", DEFAULT_REWARD_VALUE_VND, db=db
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Chốt quan hệ giới thiệu
# ══════════════════════════════════════════════════════════════════════════════

#: Một câu duy nhất làm ba việc: tiêu ý định, kiểm điều kiện, ghi quan hệ.
#:
#: Tách ra thành ba câu là mở lại đúng cửa sổ đua mà bảng `referral_intents` sinh ra để
#: đóng. `DELETE ... RETURNING` trong CTE nghĩa là worker thua cuộc nhận về 0 dòng và
#: nhánh `INSERT` phía sau không có gì để chèn.
#:
#: Điều kiện xác minh nằm trong mệnh đề `WHERE` của `DELETE`: chưa xác minh thì ý định
#: **không bị tiêu**, nó nằm lại chờ lần xác minh thật.
_SQL_QUALIFY = """
WITH taken AS (
    DELETE FROM referral_intents i
     WHERE i.referee_id = :referee
       AND (CAST(:require_verified AS boolean) = false
            OR EXISTS (SELECT 1 FROM users u
                        WHERE u.user_id = i.referee_id
                          AND u.verified_at IS NOT NULL))
 RETURNING i.referrer_id
)
INSERT INTO referrals (referee_id, referrer_id, qualified_at, risk_score)
     SELECT :referee,
            t.referrer_id,
            CASE WHEN u.risk_score <= :max_risk THEN now() ELSE NULL END,
            u.risk_score
       FROM taken t
       JOIN users u ON u.user_id = :referee
      WHERE t.referrer_id <> :referee
        AND EXISTS (SELECT 1 FROM users r WHERE r.user_id = t.referrer_id)
ON CONFLICT (referee_id) DO NOTHING
  RETURNING referrer_id, qualified_at
"""


async def qualify(db: AsyncSession, *, referee_id: int) -> int | None:
    """Chốt quan hệ giới thiệu cho một người **vừa xác minh xong**. Gọi trong transaction.

    Trả về `referrer_id` khi quan hệ vừa được ghi **và** đạt chuẩn — tức là tầng gọi phải
    đi xét mốc thưởng cho người đó. Trả `None` ở mọi nhánh còn lại:

    * không có ý định giới thiệu nào;
    * người được mời chưa xác minh (`referral.require_verified`) — ý định **giữ nguyên**;
    * người được mời đã có người giới thiệu rồi (`referee_id` là PK) — không tính lần hai;
    * tự mời chính mình, hoặc người giới thiệu không còn tồn tại;
    * điểm rủi ro vượt `referral.max_risk_score` — dòng referral **vẫn được ghi** nhưng
      `qualified_at` để NULL, nên nó không bao giờ tính vào mốc.
    """
    require_verified = await settings_service.get_bool(
        "referral.require_verified", DEFAULT_REQUIRE_VERIFIED, db=db
    )
    max_risk = await settings_service.get_int(
        "referral.max_risk_score", DEFAULT_MAX_RISK_SCORE, db=db
    )

    row = (
        await db.execute(
            text(_SQL_QUALIFY),
            {
                "referee": referee_id,
                "require_verified": require_verified,
                "max_risk": max_risk,
            },
        )
    ).one_or_none()

    if row is None:
        return None

    if row.qualified_at is None:
        log.info("referral_bi_tu_choi_vi_rui_ro", referee_id=referee_id)
        return None

    log.info("referral_dat_chuan", referee_id=referee_id, referrer_id=row.referrer_id)
    return int(row.referrer_id)


# ══════════════════════════════════════════════════════════════════════════════
# Đọc số
# ══════════════════════════════════════════════════════════════════════════════

_SQL_COUNT_QUALIFIED = """
SELECT count(*) FROM referrals WHERE referrer_id = :uid AND qualified_at IS NOT NULL
"""

#: Ba con số của §13.2.14 trong **một** câu. `total_vnd` là tổng THẬT của các mốc đã phát,
#: lấy từ `code_grants.value_vnd` — `referral_rewards` không giữ mệnh giá, và nhân
#: `claimed × reward_value_vnd` chỉ đúng chừng nào mệnh giá chưa bao giờ đổi, mà mệnh giá
#: thì đổi được bằng `/setcauhinh`.
_SQL_PROGRESS = """
SELECT (SELECT count(*) FROM referrals
         WHERE referrer_id = :uid AND qualified_at IS NOT NULL)          AS qualified,
       (SELECT count(*) FROM referral_rewards WHERE user_id = :uid)      AS claimed,
       (SELECT COALESCE(sum(g.value_vnd), 0)
          FROM referral_rewards r
          JOIN code_grants g ON g.grant_id = r.code_grant_id
         WHERE r.user_id = :uid)                                         AS total_vnd
"""


async def count_qualified(db: AsyncSession, user_id: int) -> int:
    """Số người `user_id` đã mời **đạt chuẩn**. Dòng chưa `qualified_at` không tính."""
    return int((await db.execute(text(_SQL_COUNT_QUALIFIED), {"uid": user_id})).scalar_one())


async def count_claimed(db: AsyncSession, user_id: int) -> int:
    """Số mốc đã nhận — đếm `referral_rewards`, **không** suy ra từ số người mời."""
    return int(
        (
            await db.execute(
                text("SELECT count(*) FROM referral_rewards WHERE user_id = :uid"),
                {"uid": user_id},
            )
        ).scalar_one()
    )


async def progress(db: AsyncSession, user_id: int) -> Progress:
    """Ba con số của màn hình `📈Check Chia sẻ`, đọc trong một lượt."""
    row = (await db.execute(text(_SQL_PROGRESS), {"uid": user_id})).one()
    return Progress(
        qualified=int(row.qualified),
        claimed=int(row.claimed),
        total_vnd=int(row.total_vnd),
    )


async def pending_tiers(db: AsyncSession, user_id: int) -> list[int]:
    """Các mốc ĐỦ ĐIỀU KIỆN mà CHƯA nhận, theo thứ tự tăng dần.

    `tier_dat_duoc = min(qualified // interval, max_claims)` rồi trả về **mọi** mốc từ 1
    tới đó chưa có bản ghi — đây là chỗ dùng `>=` thay cho `% interval == 0` của hệ cũ.
    Người mời 12 người rồi mới bấm phải nhận đủ 2 mốc, không mất mốc vì không bấm đúng
    lúc chạm số (§13.2.4 điểm 4).
    """
    cfg = await params(db=db)
    if cfg.interval <= 0 or cfg.max_claims <= 0:
        # `interval = 0` là chia cho 0 ngay giữa luồng phát tiền. `CHECK` của bảng
        # `campaigns` chặn được giá trị này, `settings` thì không — chặn ở đây.
        log.error(
            "cau_hinh_referral_khong_hop_le", interval=cfg.interval, max_claims=cfg.max_claims
        )
        return []

    qualified = await count_qualified(db, user_id)
    reached = min(qualified // cfg.interval, cfg.max_claims)
    if reached <= 0:
        return []

    claimed = set(
        (
            await db.execute(
                text("SELECT tier_no FROM referral_rewards WHERE user_id = :uid"),
                {"uid": user_id},
            )
        )
        .scalars()
        .all()
    )
    return [tier for tier in range(1, reached + 1) if tier not in claimed]


# ══════════════════════════════════════════════════════════════════════════════
# Phát mốc
# ══════════════════════════════════════════════════════════════════════════════


async def grant_tier(db: AsyncSession, *, user_id: int, tier_no: int) -> Grant:
    """Phát code cho một mốc. Gọi trong `transaction()`, **một transaction cho mỗi mốc**.

    Thứ tự cố ý: **chốt mốc trước, chạm kho sau**. Khoá chính `(user_id, tier_no)` của
    `referral_rewards` là hàng rào DB — hai worker cùng chạy thì chỉ một bên chèn được,
    bên kia nhận `AlreadyClaimed` và không bao giờ tới được câu lệnh lấy mã.

    Hết kho thì `reserve()` ném `OutOfStock` và **cả transaction cuộn lại** — dòng
    `referral_rewards` vừa chèn cũng biến mất, nên mốc chưa bị đánh dấu đã phát và lần
    chạy sau phát bù (§13.2.4, "Trường hợp lỗi"). Đó cũng là lý do mỗi mốc phải nằm trong
    transaction riêng: một mốc hết kho không được kéo theo mốc đã phát thành công.

    Ném:
        AlreadyClaimed: mốc này đã có bản ghi.
        OutOfStock: kho `moibanbe` rỗng.
    """
    booked = (
        await db.execute(
            text("""
            INSERT INTO referral_rewards (user_id, tier_no)
                 VALUES (:uid, :tier)
            ON CONFLICT (user_id, tier_no) DO NOTHING
              RETURNING tier_no
            """),
            {"uid": user_id, "tier": tier_no},
        )
    ).scalar_one_or_none()

    if booked is None:
        raise AlreadyClaimed(GRANT_TYPE, existing_code=None)

    cfg = await params(db=db)
    grant = await code_issuance.reserve(
        db,
        user_id=user_id,
        grant_type=GRANT_TYPE,
        grant_key=grant_key_referral_tier(user_id, tier_no),
        code_type=CODE_TYPE,
        value_vnd=cfg.reward_value_vnd,
        reason={"tier_no": tier_no},
    )

    await db.execute(
        text("""
        UPDATE referral_rewards
           SET code_grant_id = :gid
         WHERE user_id = :uid AND tier_no = :tier
        """),
        {"gid": grant.grant_id, "uid": user_id, "tier": tier_no},
    )

    log.info("phat_moc_moi_ban", user_id=user_id, tier_no=tier_no, grant_id=grant.grant_id)
    return grant


# ══════════════════════════════════════════════════════════════════════════════
# Chiến dịch
# ══════════════════════════════════════════════════════════════════════════════

#: Chiến dịch "đang chạy" là chiến dịch bật, đã tới ngày bắt đầu, và **có hạn kết thúc còn
#: hiệu lực**. Không có dòng nào thoả ⇒ không có chiến dịch nào đang chạy.
_SQL_CAMPAIGN = """
SELECT GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (ends_at - now()))))::bigint AS remaining
  FROM campaigns
 WHERE is_active
   AND (starts_at IS NULL OR starts_at <= now())
   AND ends_at IS NOT NULL
   AND ends_at > now()
 ORDER BY campaign_id DESC
 LIMIT 1
"""


async def campaign_window(db: AsyncSession) -> CampaignWindow:
    """Cửa sổ của chiến dịch đang chạy; `is_running=False` khi không có chiến dịch nào.

    ⚠️ **Đây là chỗ đảo ngược một yêu cầu cũ.** `07-db.md` (nhóm 3) từng cấm `ends_at` /
    `is_active` xuất hiện trong file này, vì mục tiêu lúc đó là tương thích byte-by-byte
    với hệ cũ khi di trú. Mục tiêu ấy đã hết phạm vi (§13.7), và §13.5 dòng F ra lệnh
    ngược lại: **chiến dịch hết hạn thì ngừng phát thưởng thật sự**, không chỉ ngừng hiển
    thị. Admin gia hạn bằng `/chiendich extend`.

    Hệ quả vận hành phải biết: **không có dòng `campaigns` nào đang chạy ⇒ không phát mốc
    nào.** Fail-closed là chủ ý — đây là đường tiêu tiền, và "chưa ai cấu hình" không phải
    lý do để tự động chi.
    """
    remaining = (await db.execute(text(_SQL_CAMPAIGN))).scalar_one_or_none()
    if remaining is None:
        return CampaignWindow(is_running=False, remaining_seconds=0)
    return CampaignWindow(is_running=True, remaining_seconds=int(remaining))


__all__ = [
    "CODE_TYPE",
    "DongChienDich",
    "list_campaigns",
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_CLAIMS",
    "DEFAULT_MAX_RISK_SCORE",
    "DEFAULT_REQUIRE_VERIFIED",
    "DEFAULT_REWARD_VALUE_VND",
    "GRANT_TYPE",
    "CampaignWindow",
    "Progress",
    "ReferralParams",
    "campaign_window",
    "count_claimed",
    "count_qualified",
    "grant_tier",
    "params",
    "pending_tiers",
    "progress",
    "qualify",
]


#: Danh sách chiến dịch cho màn hình quản trị. Khác `_SQL_CAMPAIGN` ở trên: câu kia trả lời
#: "có chiến dịch nào đang chạy không" (một dòng, mới nhất); câu này trả lời "đã có những
#: chiến dịch nào" — và nó phải hiện cả những chiến dịch đã tắt hoặc hết hạn, vì đó là chỗ
#: người vận hành nhìn ra một chiến dịch tưởng đã dừng mà cờ `is_active` vẫn bật.
_SQL_CAMPAIGN_LIST = """
SELECT campaign_id, code, name, interval_people, reward_value_vnd, max_claims,
       is_active, starts_at, ends_at,
       (is_active
        AND (starts_at IS NULL OR starts_at <= now())
        AND ends_at IS NOT NULL
        AND ends_at > now())                                        AS dang_chay,
       GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (ends_at - now()))))::bigint AS con_lai_giay
  FROM campaigns
 ORDER BY campaign_id DESC
 LIMIT :lim
"""


@dataclass(frozen=True, slots=True)
class DongChienDich:
    campaign_id: int
    code: str
    name: str
    interval_people: int
    reward_value_vnd: int
    max_claims: int
    is_active: bool
    starts_at: datetime | None
    ends_at: datetime | None
    dang_chay: bool
    con_lai_giay: int | None


async def list_campaigns(db: AsyncSession, *, limit: int = 20) -> list[DongChienDich]:
    """Chiến dịch gần nhất, mới nhất trước — kể cả đã tắt và đã hết hạn.

    Hiện cả những chiến dịch **bật mà vô hình** là chủ ý: `campaign_window()` chỉ đọc dòng
    mới nhất thoả điều kiện, nên một dòng cũ còn `is_active` vẫn nằm đó mà không màn hình
    nào nói ra. Đây là màn hình nói ra nó.
    """
    rows = (await db.execute(text(_SQL_CAMPAIGN_LIST), {"lim": limit})).all()
    return [
        DongChienDich(
            campaign_id=r.campaign_id,
            code=r.code,
            name=r.name,
            interval_people=r.interval_people,
            reward_value_vnd=r.reward_value_vnd,
            max_claims=r.max_claims,
            is_active=r.is_active,
            starts_at=r.starts_at,
            ends_at=r.ends_at,
            dang_chay=r.dang_chay,
            con_lai_giay=None if r.con_lai_giay is None else int(r.con_lai_giay),
        )
        for r in rows
    ]
