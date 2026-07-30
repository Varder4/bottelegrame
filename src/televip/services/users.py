"""Ghi nhận người dùng và ý định giới thiệu.

Hai thao tác ở đây đều phải idempotent: Telegram có thể gửi lại cùng một update, và
người dùng bấm `/start` bao nhiêu lần tuỳ thích.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UpsertResult:
    user_id: int
    #: True khi đây là lần đầu người này chạm vào bot. Quyết định có ghi nhận
    #: người giới thiệu hay không — chỉ người MỚI mới được tính cho ai đó.
    is_new: bool
    is_verified: bool


async def upsert_user(
    db: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
) -> UpsertResult:
    """Tạo mới hoặc cập nhật hồ sơ. Gọi trong `transaction()`.

    `xmax = 0` là mẹo của PostgreSQL để phân biệt INSERT thật với UPDATE trong cùng một
    câu `ON CONFLICT`: hàng vừa được chèn có `xmax` bằng 0, hàng bị cập nhật thì không.
    Cách khác là chạy một câu SELECT trước, nhưng như vậy có khoảng trống giữa hai câu
    và hai request song song của cùng một người sẽ cùng thấy "chưa tồn tại".
    """
    row = (
        await db.execute(
            text("""
            INSERT INTO users (user_id, username, full_name, started_bot_at, last_active)
                 VALUES (:uid, :un, :fn, now(), now())
            ON CONFLICT (user_id) DO UPDATE
                    SET username    = EXCLUDED.username,
                        full_name   = EXCLUDED.full_name,
                        last_active = now()
              RETURNING (xmax = 0) AS is_new,
                        (verified_at IS NOT NULL) AS is_verified
            """),
            {"uid": user_id, "un": username, "fn": full_name},
        )
    ).one()

    return UpsertResult(user_id=user_id, is_new=row.is_new, is_verified=row.is_verified)


async def record_referral_intent(
    db: AsyncSession,
    *,
    referee_id: int,
    referrer_id: int,
) -> bool:
    """Ghi nhận "ai giới thiệu ai" từ deep link. Gọi trong `transaction()`.

    Đây mới chỉ là **ý định**, chưa phải referral được tính. Nó chỉ chuyển thành referral
    thật sau khi người được mời xác minh xong — nếu tính ngay lúc bấm link thì chỉ cần
    phát tán link cho hàng nghìn tài khoản rác là ăn được thưởng.

    Trả về True nếu vừa ghi mới. Bỏ qua im lặng khi tự mời chính mình hoặc người giới
    thiệu không tồn tại.
    """
    if referrer_id == referee_id:
        return False

    result = await db.execute(
        text("""
        INSERT INTO referral_intents (referee_id, referrer_id)
             SELECT :referee, :referrer
              WHERE EXISTS (SELECT 1 FROM users WHERE user_id = :referrer)
        ON CONFLICT (referee_id) DO NOTHING
          RETURNING referee_id
        """),
        {"referee": referee_id, "referrer": referrer_id},
    )
    created = result.scalar_one_or_none() is not None
    if created:
        log.info("ghi_nhan_nguoi_gioi_thieu", referee_id=referee_id, referrer_id=referrer_id)
    return created


def parse_referral_payload(payload: str | None) -> int | None:
    """Đọc `ref_<user_id>` từ deep link `t.me/bot?start=...`.

    Bot cũ dùng tiền tố `cref_575_` với một số nhóm nhúng ở giữa; bot mới chỉ có
    `ref_<id>`. Trả None nếu payload rỗng, sai định dạng, hoặc id không phải số dương.
    """
    if not payload or not payload.startswith("ref_"):
        return None
    raw = payload[4:]
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None
