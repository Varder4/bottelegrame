"""Cấp code — trái tim chống thất thoát tiền.

**Mọi luồng phát code phải đi qua đúng hàm `reserve()` trong file này.** Không có đường
thứ hai. Ở hệ cũ có bốn nơi tự chọn code rồi tự đánh dấu đã dùng, và cả bốn đều sai theo
cùng một kiểu.

## Lỗi của hệ cũ, và cách chỗ này chặn nó

Hệ cũ làm hai bước ở **hai kết nối khác nhau**::

    row = SELECT code_id FROM codes WHERE is_used=0 LIMIT 1   -- kết nối A, rồi đóng
    UPDATE codes SET is_used=1 WHERE code_id=?                -- kết nối B, KHÔNG có
                                                              -- điều kiện is_used=0

Giữa hai bước đó, một tiến trình khác chọn trúng cùng mã. Đo được trên dữ liệu thật:
**226 người giữ 544 code tân thủ thừa, khoảng 5,44 triệu đồng.**

Ở đây cả việc chọn lẫn việc giữ chỗ nằm trong **một câu lệnh duy nhất**, và câu đó dùng
``FOR UPDATE SKIP LOCKED``: hai tiến trình chạy đồng thời không bao giờ nhìn thấy cùng
một dòng — người đến sau bỏ qua dòng đang bị khoá và lấy dòng kế tiếp.

## Hai pha, và vì sao phải hai pha

Hệ cũ đánh dấu code đã dùng **trước khi** gửi tin. Gửi lỗi là mã bốc hơi: không ai nhận
được nhưng kho vẫn trừ. Ở tỉ lệ gửi lỗi 2-5% bình thường, mỗi đợt lớn đốt hàng nghìn mã.

    pha 1  reserve()          available → reserved, giữ chỗ 15 phút
           (gửi tin qua outbox)
    pha 2  mark_delivered()   reserved → issued, ghi sổ cái, cộng bộ đếm hiển thị

Gửi thất bại thì grant nằm lại ở ``reserved`` để thử lại; quá hạn giữ chỗ thì job
``reap_reservations()`` trả mã về kho. **Không có nhánh nào làm mất mã.**
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.clock import now_utc
from televip.core.errors import AlreadyClaimed, OutOfStock
from televip.core.logging import get_logger
from televip.db.models.codes import Code, CodeGrant, CodeLedgerEntry
from televip.db.models.identity import User

log = get_logger(__name__)

#: Thời gian giữ chỗ một mã trước khi job dọn trả nó về kho.
#: Đủ dài để vượt qua một đợt Telegram trả 429 kéo dài, đủ ngắn để kho không bị treo lâu.
RESERVATION_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class Grant:
    """Kết quả một lần cấp code."""

    grant_id: int
    code_id: int
    code_value: str
    value_vnd: int
    #: True khi đây là lần bấm lại và ta trả về đúng grant đã có, không tạo grant mới.
    was_existing: bool


async def reserve(
    db: AsyncSession,
    *,
    user_id: int,
    grant_type: str,
    grant_key: str,
    code_type: str,
    value_vnd: int,
    reason: dict | None = None,
) -> Grant:
    """Giữ chỗ một mã cho một người. Gọi trong `transaction()`.

    Idempotent theo ``grant_key``: bấm nút mười lần vẫn ra đúng một grant và đúng một mã.

    Ném:
        AlreadyClaimed: đã có grant cho ``grant_key`` này nhưng chưa gắn được mã.
        OutOfStock: kho hết mã loại ``(code_type, value_vnd)``.
    """
    now = now_utc()

    # ── Bước 1: giành quyền sở hữu grant_key ────────────────────────
    # ON CONFLICT DO NOTHING + RETURNING: người thắng nhận grant_id, người thua nhận rỗng.
    # Đây là chỗ hai request song song của CÙNG một người bị tách ra, trước khi kịp
    # chạm vào kho. Hệ cũ kiểm bằng `SELECT COUNT(*)` rồi `await` mạng tới 15 giây mới
    # cấp — cửa sổ đó đủ rộng để bấm nút 5 lần và nhận 5 mã.
    ins = (
        pg_insert(CodeGrant)
        .values(
            grant_key=grant_key,
            user_id=user_id,
            grant_type=grant_type,
            value_vnd=value_vnd,
            state="reserved",
            idempotency_key=grant_key,
            reason=reason or {},
        )
        .on_conflict_do_nothing(index_elements=["grant_key"])
        .returning(CodeGrant.grant_id)
    )
    grant_id = (await db.execute(ins)).scalar_one_or_none()

    if grant_id is None:
        # Đã tồn tại. Trả lại đúng mã cũ — bấm lại phải cho ra cùng kết quả, không phải lỗi.
        existing = (
            await db.execute(
                select(CodeGrant.grant_id, CodeGrant.code_id, CodeGrant.value_vnd, Code.code_value)
                .join(Code, Code.code_id == CodeGrant.code_id, isouter=True)
                .where(CodeGrant.grant_key == grant_key)
            )
        ).one()
        if existing.code_id is None:
            # Grant có nhưng chưa gắn mã (lần trước hết kho hoặc chết giữa chừng).
            raise AlreadyClaimed(grant_type, existing_code=None)
        return Grant(
            grant_id=existing.grant_id,
            code_id=existing.code_id,
            code_value=existing.code_value,
            value_vnd=existing.value_vnd,
            was_existing=True,
        )

    # ── Bước 2: chọn VÀ giữ chỗ một mã, trong MỘT câu lệnh ──────────
    # `SKIP LOCKED` là thứ làm câu này an toàn dưới tải: hai worker chạy cùng lúc không
    # bao giờ khoá nhau, và tuyệt đối không bao giờ nhận cùng một `code_id`.
    picked = (
        await db.execute(
            text("""
                UPDATE codes
                   SET status = 'reserved',
                       reserved_for = :user_id,
                       reserved_until = :until
                 WHERE code_id = (
                       SELECT code_id
                         FROM codes
                        WHERE code_type = :code_type
                          AND value_vnd = :value_vnd
                          AND status = 'available'
                        ORDER BY code_id
                          FOR UPDATE SKIP LOCKED
                        LIMIT 1
                 )
             RETURNING code_id, code_value
            """),
            {
                "user_id": user_id,
                "until": now + RESERVATION_TTL,
                "code_type": code_type,
                "value_vnd": value_vnd,
            },
        )
    ).one_or_none()

    if picked is None:
        # Hết kho. Ném lỗi để transaction cuộn lại — dòng grant vừa tạo cũng biến mất,
        # nên lần bấm sau người dùng thử lại được ngay khi admin nạp thêm mã.
        log.warning("het_code", code_type=code_type, value_vnd=value_vnd, user_id=user_id)
        raise OutOfStock(code_type, value_vnd)

    # ── Bước 3: gắn mã vào grant ────────────────────────────────────
    await db.execute(
        update(CodeGrant).where(CodeGrant.grant_id == grant_id).values(code_id=picked.code_id)
    )

    return Grant(
        grant_id=grant_id,
        code_id=picked.code_id,
        code_value=picked.code_value,
        value_vnd=value_vnd,
        was_existing=False,
    )


def _entry_hash(prev_hash: bytes | None, payload: dict) -> bytes:
    """Mắt xích hash: mỗi bút toán ký lên bút toán trước.

    Sửa lén một dòng ở giữa sổ cái làm đứt chuỗi, và job đối soát đêm phát hiện ngay.
    `sort_keys` để cùng dữ liệu luôn cho cùng hash bất kể thứ tự khoá trong dict.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256((prev_hash or b"") + body).digest()


async def mark_delivered(
    db: AsyncSession,
    *,
    grant_id: int,
) -> None:
    """Pha 2: Telegram đã xác nhận gửi tới tay người dùng. Gọi trong `transaction()`.

    Đây là nơi DUY NHẤT được phép chuyển mã sang ``issued`` và ghi sổ cái tiền. Gọi hai
    lần cho cùng ``grant_id`` là vô hại: điều kiện ``state = 'reserved'`` làm lần thứ hai
    không đụng dòng nào.
    """
    row = (
        await db.execute(
            update(CodeGrant)
            .where(CodeGrant.grant_id == grant_id, CodeGrant.state == "reserved")
            .values(state="delivered", delivered_at=now_utc())
            .returning(
                CodeGrant.user_id,
                CodeGrant.code_id,
                CodeGrant.value_vnd,
                CodeGrant.grant_type,
            )
        )
    ).one_or_none()

    if row is None:
        return  # đã delivered rồi — không làm gì, không báo lỗi

    await db.execute(
        update(Code).where(Code.code_id == row.code_id).values(status="issued", reserved_until=None)
    )

    # Bộ đếm hiển thị: cộng TƯƠNG ĐỐI và trong CÙNG transaction với sổ cái. Đọc số cũ ra
    # rồi ghi số mới vào là cách hai request song song ghi đè lẫn nhau.
    await db.execute(
        update(User)
        .where(User.user_id == row.user_id)
        .values(
            total_codes_received=User.total_codes_received + 1,
            total_value_received=User.total_value_received + row.value_vnd,
        )
    )

    prev_hash = (
        await db.execute(
            select(CodeLedgerEntry.entry_hash).order_by(CodeLedgerEntry.entry_id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    payload = {
        "user_id": row.user_id,
        "grant_id": grant_id,
        "code_id": row.code_id,
        "value_vnd": row.value_vnd,
        "direction": 1,
        "reason": f"grant:{row.grant_type}",
    }
    db.add(
        CodeLedgerEntry(
            prev_hash=prev_hash,
            entry_hash=_entry_hash(prev_hash, payload),
            user_id=row.user_id,
            grant_id=grant_id,
            code_id=row.code_id,
            value_vnd=row.value_vnd,
            direction=1,
            reason=f"grant:{row.grant_type}",
        )
    )


async def reap_reservations(db: AsyncSession, *, limit: int = 500) -> int:
    """Trả về kho những mã đã giữ chỗ nhưng quá hạn mà chưa gửi được.

    Đây là lý do "gửi tin thất bại" không còn đốt mã. Chạy định kỳ vài phút một lần.
    Trả về số mã đã thu hồi.

    **Phải gỡ liên kết ở CẢ HAI phía.** Trả mã về kho mà để `code_grants.code_id` vẫn
    trỏ tới nó là một cái bẫy chết người: bảng có `uq_grants_code UNIQUE (code_id)`, còn
    `reserve()` chọn mã theo `ORDER BY code_id`, nên mã vừa thu hồi — vốn có id nhỏ nhất
    — được chọn lại ở **mọi** lượt sau, rồi bước gắn mã nổ `UniqueViolation`. Lỗi đó
    không phải `OutOfStock` cũng không phải `AlreadyClaimed` nên không handler nào bắt:
    một lần gửi lỗi duy nhất là chết vĩnh viễn cả đường phát của loại code đó.

    Gỡ xong, grant quay về đúng trạng thái "có grant nhưng chưa gắn mã" mà `reserve()`
    đã xử lý sẵn (ném `AlreadyClaimed`), và mọi handler đều có nhánh cho nó.
    """
    result = await db.execute(
        text("""
            WITH reclaimed AS (
                UPDATE codes
                   SET status = 'available',
                       reserved_for = NULL,
                       reserved_until = NULL
                 WHERE code_id IN (
                       SELECT code_id
                         FROM codes
                        WHERE status = 'reserved'
                          AND reserved_until < now()
                        ORDER BY reserved_until
                          FOR UPDATE SKIP LOCKED
                        LIMIT :limit
                 )
             RETURNING code_id
            ), unlinked AS (
                UPDATE code_grants
                   SET code_id = NULL
                 WHERE code_id IN (SELECT code_id FROM reclaimed)
                   AND state = 'reserved'
             RETURNING grant_id
            )
            SELECT code_id FROM reclaimed
        """),
        {"limit": limit},
    )
    reclaimed = len(result.fetchall())
    if reclaimed:
        log.info("thu_hoi_ma_giu_cho_qua_han", so_luong=reclaimed)
    return reclaimed


async def available_count(db: AsyncSession, *, code_type: str, value_vnd: int) -> int:
    """Tồn kho khả dụng của một (loại, mệnh giá)."""
    return (
        await db.execute(
            select(text("count(*)"))
            .select_from(Code)
            .where(
                Code.code_type == code_type,
                Code.value_vnd == value_vnd,
                Code.status == "available",
            )
        )
    ).scalar_one()


__all__ = [
    "Grant",
    "RESERVATION_TTL",
    "available_count",
    "mark_delivered",
    "reap_reservations",
    "reserve",
]
