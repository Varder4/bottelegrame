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
from datetime import datetime, timedelta
from typing import Any

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
    #: Trạng thái grant lúc trả về. `was_existing` một mình KHÔNG đủ để kết luận "người
    #: này đã cầm mã": một grant `reserved` là mã đã giữ chỗ mà lần gửi trước THẤT BẠI,
    #: nghĩa là người dùng chưa nhận được gì. Chỉ `delivered` mới là đã trao tới tay.
    state: str = "reserved"


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

    re_attached = False

    if grant_id is None:
        # ── KHOÁ DÒNG GRANT TRƯỚC KHI ĐỌC ──────────────────────────────────────────
        # `ON CONFLICT DO NOTHING` chỉ chặn được người đến sau trong lúc dòng kia CHƯA
        # commit. Với một grant đã commit từ trước, nó trả về rỗng mà **không giữ khoá
        # nào** — nên hai lời gọi song song cùng đọc `code_id IS NULL`, cùng giành được
        # một mã KHÁC NHAU qua `SKIP LOCKED`, và người sau chỉ việc ghi đè `code_id`.
        # Mã của người trước mồ côi, được job dọn trả về kho, rồi phát cho NGƯỜI KHÁC —
        # hai người cầm chung một mã quà. Đo được 30/30 lần trên database thật.
        #
        # Đường tới đây có thật: `handle_check_groups` (nút gọi `reserve()`) không có
        # cooldown, và `concurrent_updates(True)` biến một cú bấm đúp thành hai task
        # chạy song song.
        #
        # Khoá bằng một câu SELECT RIÊNG chỉ lấy khoá chính. Không gắn `FOR UPDATE` vào
        # câu nối bảng bên dưới: Postgres từ chối `FOR UPDATE` trên nhánh nullable của
        # outer join, còn `of=CodeGrant` thì trả `code_value = None` cho người thua (nó
        # khoá lại dòng grant nhưng dùng lại bản join đã đọc), và handler sẽ in "Mã: None".
        await db.execute(
            select(CodeGrant.grant_id).where(CodeGrant.grant_key == grant_key).with_for_update()
        )

        # Đã tồn tại. Trả lại đúng mã cũ — bấm lại phải cho ra cùng kết quả, không phải lỗi.
        existing = (
            await db.execute(
                select(
                    CodeGrant.grant_id,
                    CodeGrant.code_id,
                    CodeGrant.value_vnd,
                    CodeGrant.state,
                    Code.code_value,
                )
                .join(Code, Code.code_id == CodeGrant.code_id, isouter=True)
                .where(CodeGrant.grant_key == grant_key)
            )
        ).one()
        if existing.code_id is None:
            # ── Grant MỒ CÔI: có dòng grant nhưng không có mã nào gắn vào ──────────
            #
            # Hai đường dẫn tới đây, và cả hai đều xảy ra thật:
            #   1. Lần trước chạm đúng lúc kho rỗng, rồi giao dịch cuộn lại không sạch.
            #   2. Lần trước giữ chỗ được mã nhưng GỬI THẤT BẠI, nên không `mark_delivered`;
            #      `reap_reservations()` sau đó trả mã về kho **và NULL `code_grants.code_id`**.
            #      Đường (2) là đường thường gặp, vì job dọn kho chạy mỗi phút.
            #
            # Trước đây chỗ này ném thẳng `AlreadyClaimed`, và **không có đường nào trong
            # toàn bộ hệ thống gắn lại mã cho một grant mồ côi**. Hệ quả đo được: người
            # dùng bị khoá VĨNH VIỄN khỏi phần thưởng của chính mình — kho đầy 50 mã mà
            # bấm bao nhiêu lần cũng nhận đúng một câu "code đang hết", và không lệnh admin
            # nào gỡ được (`/resend_tanthu` chỉ đọc grant ĐÃ có mã). Lỗi này có ở cả bốn
            # luồng phát: tân thủ, mốc mời bạn, đập hộp, đổi điểm.
            #
            # Nên ở đây **gắn lại**, không ném. Không có rủi ro phát trùng: `grant_key` là
            # duy nhất nên vẫn đúng một grant, và `uq_grants_code` vẫn giữ đúng một mã.
            if existing.state != "reserved":
                # `revoked` / `delivered`-mà-mất-mã là trạng thái không được tự sửa: nó
                # nghĩa là có ai đó đã can thiệp, và đoán ý ở đây là ghi đè lên quyết định
                # của con người trên một đường tiêu tiền.
                raise AlreadyClaimed(grant_type, existing_code=None)
            grant_id = existing.grant_id
            re_attached = True

            # ── GHIM MỆNH GIÁ VỀ ĐÚNG CON SỐ ĐÃ VÀO SỔ ────────────────────────────
            # `value_vnd` do NƠI GỌI truyền vào, và nó có thể khác con số đã ghi trong
            # `code_grants` từ lượt trước. Không ghim thì bước 2 chọn mã theo con số MỚI
            # trong khi sổ cái, bút toán và bộ đếm người dùng vẫn ghi con số CŨ — người
            # dùng cầm một mã 50.000đ mà sổ ghi 10.000đ, hoặc ngược lại.
            #
            # Đường tới đây có thật và không cần admin làm gì: khoá đổi điểm là
            # `redeem:{user_id}:{ngày}`, KHÔNG mang bậc mệnh giá. Bấm bậc 10K → gửi hỏng
            # → job dọn → cùng ngày bấm bậc 50K: `redeem()` thấy đã trừ điểm rồi nên
            # không trừ thêm, còn `reserve()` thì phát mã 50K trên một grant ghi 10K.
            #
            # Ghim về `existing.value_vnd` chứ KHÔNG sửa `code_grants.value_vnd` theo yêu
            # cầu mới: người dùng đã trả điểm cho con số cũ, đổi giá sau lưng họ là một
            # giao dịch khác hẳn. Hết kho mệnh giá cũ thì ném `OutOfStock` — hỏng theo
            # hướng an toàn, đúng ý.
            value_vnd = existing.value_vnd

            log.warning(
                "gan_lai_ma_cho_grant_mo_coi",
                grant_key=grant_key,
                grant_id=grant_id,
                user_id=user_id,
                value_vnd=value_vnd,
            )
        else:
            return Grant(
                grant_id=existing.grant_id,
                code_id=existing.code_id,
                code_value=existing.code_value,
                value_vnd=existing.value_vnd,
                was_existing=True,
                state=existing.state,
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
        # Grant gắn lại là grant ĐÃ TỒN TẠI. Trả `False` ở đây làm chết hàng rào
        # `if grant.was_existing and grant.value_vnd != value_vnd` của `checkin.redeem()`
        # — đúng vào lượt duy nhất nó có việc để làm.
        was_existing=re_attached,
        state="reserved",
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


# ── Thu hồi ─────────────────────────────────────────────────────────
#
# Hai hàm dưới đây là NƠI DUY NHẤT được đổi `codes.status` sang `revoked`. Trước đây hai
# câu `UPDATE codes` này nằm thẳng trong `handlers/admin/codes.py` — tức là ở tầng trình
# bày, nơi mà `scripts/check_architecture.py` (luật 3) cấm. Bộ canh kiến trúc bắt được
# đúng hai chỗ đó.
#
# Việc dời xuống đây không phải dọn dẹp cho gọn: sắp có tầng trình bày THỨ HAI (panel web)
# gọi vào cùng nghiệp vụ này. Để nguyên thì hàng rào `status = 'available'` phải được viết
# lại lần thứ hai ở panel, và hai bản sao chép sớm muộn cũng lệch nhau — lần lệch đó sẽ là
# lần một câu UPDATE chạm vào mã đang thuộc về một người thật.

#: `status = 'available'` trong `WHERE` là hàng rào thật, không phải bộ lọc cho đẹp: mã đã
#: giữ chỗ hoặc đã phát nằm ngoài phạm vi câu này, nên không có đường nào để một lần gõ
#: nhầm chạm vào mã đang thuộc về một người thật.
_SQL_REVOKE_ONE = """
UPDATE codes
   SET status = 'revoked'
 WHERE code_id = :cid
   AND status = 'available'
RETURNING code_id
"""

_SQL_REVOKE_BULK = """
UPDATE codes
   SET status = 'revoked'
 WHERE code_type = :code_type
   AND status = 'available'
   AND (:value_vnd = 0 OR value_vnd = :value_vnd)
RETURNING code_id, value_vnd
"""


async def revoke_one(db: AsyncSession, *, code_id: int) -> int | None:
    """Thu hồi MỘT mã chưa phát. Trả `code_id` đã thu, hoặc `None` nếu mã không còn khả dụng.

    `None` nghĩa là mã vừa bị người khác giành mất, hoặc đã phát rồi — nơi gọi phải coi đó
    là "không thu được" chứ không phải lỗi.

    KHÔNG `DELETE`: hàng dữ liệu ở lại để đối soát vẫn ra số. Hệ cũ xoá thẳng hàng, kể cả
    mã đã trao cho người dùng, và sổ cái thủng.
    """
    return (await db.execute(text(_SQL_REVOKE_ONE), {"cid": code_id})).scalar_one_or_none()


async def revoke_bulk(db: AsyncSession, *, code_type: str, value_vnd: int = 0) -> list[Any]:
    """Thu hồi TOÀN BỘ mã chưa phát của một loại. `value_vnd = 0` nghĩa là không lọc mệnh giá.

    Trả về các dòng `(code_id, value_vnd)` đã thu — nơi gọi cộng lại để báo tổng giá trị.

    ⚠️ Hàm này KHÔNG kiểm ngưỡng duyệt hai người. Hàng rào đó phụ thuộc *ai* đang gọi và
    *kho lúc nào*, nên nó nằm ở nơi gọi (`handlers/admin/codes.py`, và sau này là panel).
    Đây là một chỗ hở đã biết: xem `docs/ke-hoach-v2/16-admin-web.md` mục 0.1 — nhóm hàng
    rào tiền còn nằm ở tầng trình bày, và kế hoạch panel web dời chúng vào trong.
    """
    rows = await db.execute(
        text(_SQL_REVOKE_BULK), {"code_type": code_type, "value_vnd": value_vnd}
    )
    return list(rows.all())


# ── Sổ phát của MỘT người ───────────────────────────────────────────

_SQL_GRANTS_OF_USER = """
SELECT g.grant_type, g.value_vnd, g.state, g.created_at, c.code_value
  FROM code_grants g
  LEFT JOIN codes c ON c.code_id = g.code_id
 WHERE g.user_id = :uid
 ORDER BY g.created_at DESC
 LIMIT :lim
"""


@dataclass(frozen=True, slots=True)
class GrantRow:
    """Một dòng trong sổ phát của một người.

    `code_value` là `None` khi grant đang ở trạng thái `reserved` mà chưa gắn được mã —
    trạng thái có thật, không phải lỗi dữ liệu.
    """

    grant_type: str
    value_vnd: int
    state: str
    created_at: datetime
    code_value: str | None


async def grants_of_user(db: AsyncSession, user_id: int, *, limit: int) -> list[GrantRow]:
    """`limit` lần phát gần nhất của một người, mới nhất trước.

    Đặt ở đây chứ không ở `services/users.py`: câu này đọc `code_grants` LEFT JOIN `codes`
    — hai bảng TIỀN do module này làm chủ. Để nó ở module người dùng thì lần sau ai sửa
    quy tắc phát mã sẽ không tìm thấy nó.
    """
    rows = (await db.execute(text(_SQL_GRANTS_OF_USER), {"uid": user_id, "lim": limit})).all()
    return [
        GrantRow(
            grant_type=r.grant_type,
            value_vnd=r.value_vnd,
            state=r.state,
            created_at=r.created_at,
            code_value=r.code_value,
        )
        for r in rows
    ]


__all__ = [
    "Grant",
    "GrantRow",
    "RESERVATION_TTL",
    "available_count",
    "grants_of_user",
    "mark_delivered",
    "reap_reservations",
    "reserve",
    "revoke_bulk",
    "revoke_one",
]
