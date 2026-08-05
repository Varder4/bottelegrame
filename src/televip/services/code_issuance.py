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
from typing import Any, Final

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


# ══════════════════════════════════════════════════════════════════════
# Nghiệp vụ thu hồi
# ══════════════════════════════════════════════════════════════════════
#
# Hai hàm nguyên thuỷ ở trên chỉ biết một câu `UPDATE`. Mọi hàng rào quyết định *có được
# thu hồi hay không* vốn nằm trong `handlers/admin/codes.py` — tức chỉ tồn tại trên đường
# vào Telegram. Panel web là đường vào thứ hai, và thu hồi là thao tác **huỷ nghĩa vụ**:
# một câu lệnh chạm được cả nghìn mã.
#
# Khuôn giống hệt nạp kho (`services/stock.py`): một hàm KIỂM trả về một kiểu, và một hàm
# GHI **chỉ nhận kiểu đó**. Không đường vào nào ghi thẳng được, kể cả một script chạy tay.
#
# ## Hai chỗ khác hành vi cũ, và cả hai chống GÕ NHẦM chứ không chống kẻ xấu
#
# Panel chỉ có người trong nhà dùng, nên ở đây không dựng lớp nào để chặn kẻ tấn công. Cái
# thật sự xảy ra là người vận hành bấm một cái nút đã cũ:
#
# 1. **Cái sắp xoá không được LỚN HƠN cái đã nhìn thấy.** Bản cũ chỉ *cảnh báo* khi lệch,
#    và cảnh báo đó in ra SAU khi đã xoá. Kịch bản đời thường: mở trang xác nhận lúc kho
#    có 1 mã, để đó đi nạp thêm 200 mã, quay lại bấm — xoá sạch 200 mã vừa nạp.
#
# 2. **Vé nhớ cả TIỀN, không chỉ số mã.** 10 mã 5.000đ và 10 mã 88.000đ có cùng số mã, nên
#    canh lệch bằng số mã thì 880.000đ biến mất mà không dòng nào kêu.
#
# ## Một luật về giao dịch mà nơi gọi PHẢI giữ
#
# `thu_hoi_hang_loat()` ném ngoại lệ **sau** câu `UPDATE` (nó đo trên chính tập
# `RETURNING`). Nên `try/except` của nơi gọi phải nằm **NGOÀI** `async with transaction()`.
# Bắt bên trong thì giao dịch vẫn commit: mã đã `revoked`, sổ không ghi, màn hình báo "từ
# chối" — đúng nghĩa sổ cái lệch với bảng `codes`.

MENH_GIA_TAT_CA: Final = "__tatca__"
"""Sentinel TƯỜNG MINH cho "mọi mệnh giá".

Chuỗi rỗng **không bao giờ** mang nghĩa này. `Form()` của FastAPI đưa `""` chứ không đưa
`None`, nên `menh_gia or None` sẽ biến một ô bị trình duyệt khôi phục về rỗng thành phạm
vi rộng nhất có thể — đạt được bằng cách KHÔNG LÀM GÌ. Sự khác nhau giữa "một mệnh giá" và
"TẤT CẢ" không được phép là sự khác nhau giữa có chữ và không có chữ.
"""

#: Vé đề nghị thu hồi hàng loạt. Giữ nguyên tiền tố và TTL mà bot đang chạy — vé dùng
#: CHUNG giữa hai đường vào, nên mở đề nghị trên web cũng làm chết cái nút cũ còn nằm
#: trong lịch sử chat Telegram.
DE_NGHI_PREFIX: Final = "dac:"
DE_NGHI_TTL_SECONDS: Final = 300

#: Khoá "mỗi người tối đa MỘT đề nghị sống" — chống chính mình mở ba tab rồi chỉ nhớ một.
DE_NGHI_CHU_PREFIX: Final = "dac:chu:"


class ThuHoiBiTuChoi(ValueError):
    """Không thu hồi được. `args[0]` là câu hiện thẳng cho người vận hành.

    Mọi lớp con mang FIELD có kiểu để tầng trình bày tự viết câu chữ — cùng khuôn với
    `stock.NapKhoBiTuChoi`. Hai tầng nhờ đó có đúng một hình dạng bắt lỗi.
    """


# — thu hồi một mã —


class NhieuHonMotMa(ThuHoiBiTuChoi):
    """Ô nhập chứa nhiều hơn một mã — nói ra thay vì im lặng xoá cái đầu tiên."""

    def __init__(self, *, so_token: int) -> None:
        self.so_token = so_token
        super().__init__(
            f"Ô này nhận đúng MỘT mã, bạn đưa vào {so_token}. "
            f"Muốn thu hồi nhiều mã thì dùng ô thu hồi hàng loạt bên dưới."
        )


class KhongTimThayMa(ThuHoiBiTuChoi):
    def __init__(self, *, code_value: str) -> None:
        self.code_value = code_value
        super().__init__(f"Không tìm thấy code: {code_value}")


class MaDaPhat(ThuHoiBiTuChoi):
    """Mã đã vào sổ cái. Thu hồi là một BÚT TOÁN NGƯỢC, không phải một lần xoá dòng."""

    def __init__(
        self,
        *,
        code_value: str,
        code_type: str,
        value_vnd: int,
        holder_id: int,
        holder_username: str | None,
        holder_name: str | None,
        grant_state: str,
        moc: datetime | None,
    ) -> None:
        self.code_value = code_value
        self.code_type = code_type
        self.value_vnd = value_vnd
        self.holder_id = holder_id
        self.holder_username = holder_username
        self.holder_name = holder_name
        self.grant_state = grant_state
        self.moc = moc
        super().__init__(f"Code {code_value} đã phát cho {holder_id}, không xoá được.")


class MaDaThuHoi(ThuHoiBiTuChoi):
    def __init__(self, *, code_value: str) -> None:
        self.code_value = code_value
        super().__init__(f"Code {code_value} đã bị thu hồi trước đó.")


class MatCuocDua(ThuHoiBiTuChoi):
    """`UPDATE` sửa 0 dòng: mã rời trạng thái `available` giữa lượt đọc và lượt ghi.

    Điều kiện `status = 'available'` trong chính câu `UPDATE` mới là thứ chặn, không phải
    câu `if` phía trên nó.
    """

    def __init__(self, *, code_value: str) -> None:
        self.code_value = code_value
        super().__init__(f"Code {code_value} vừa được một luồng khác giữ chỗ. Không thu hồi được.")


# — thu hồi hàng loạt —


class LoaiKhongHopLe(ThuHoiBiTuChoi):
    def __init__(self, *, code_type_tho: str) -> None:
        self.code_type_tho = code_type_tho
        super().__init__(f"Loại code không hợp lệ: {code_type_tho}")


class MenhGiaKhongDocDuoc(ThuHoiBiTuChoi):
    def __init__(self, *, menh_gia_tho: str) -> None:
        self.menh_gia_tho = menh_gia_tho
        super().__init__(f"Mệnh giá không hợp lệ: {menh_gia_tho}")


class PhamViRong(ThuHoiBiTuChoi):
    def __init__(self, *, code_type: str, value_vnd: int) -> None:
        self.code_type = code_type
        self.value_vnd = value_vnd
        super().__init__(f"Không có mã CHƯA PHÁT nào khớp phạm vi: {code_type}")


class VuotNguongThuHoi(ThuHoiBiTuChoi):
    """Phạm vi vượt ngưỡng duyệt hai người.

    Cố ý **không** kế thừa `stock.VuotNguongDuyetHaiNguoi`: cái đó là con của
    `NapKhoBiTuChoi`, và một route thu hồi bắt `NapKhoBiTuChoi` là câu lệnh vô nghĩa. Hai
    miền dùng chung **hằng số khoá cấu hình**, không dùng chung cây ngoại lệ.
    """

    def __init__(self, *, tong_vnd: int, threshold_vnd: int, so_ma: int) -> None:
        self.tong_vnd = tong_vnd
        self.threshold_vnd = threshold_vnd
        self.so_ma = so_ma
        super().__init__(
            f"Phạm vi này trị giá {tong_vnd} đ ({so_ma} mã), vượt ngưỡng cần duyệt hai "
            f"người là {threshold_vnd} đ"
        )


class PhamViDaLonLen(ThuHoiBiTuChoi):
    """Cái sắp bị xoá LỚN HƠN cái con người đã nhìn thấy — về số mã hoặc về tiền.

    Kho vơi đi giữa hai bước là bình thường (có người vừa nhận mã) và được dung thứ: nói ra
    con số thật là đủ. Kho LỚN LÊN thì không: người vận hành duyệt một phạm vi, và cái được
    thi hành phải không rộng hơn cái đã duyệt.
    """

    def __init__(
        self, *, so_ma_da_hien: int, so_ma_thuc: int, tong_vnd_da_hien: int, tong_vnd_thuc: int
    ) -> None:
        self.so_ma_da_hien = so_ma_da_hien
        self.so_ma_thuc = so_ma_thuc
        self.tong_vnd_da_hien = tong_vnd_da_hien
        self.tong_vnd_thuc = tong_vnd_thuc
        super().__init__(
            f"Phạm vi đã lớn lên trong lúc chờ: lúc xem thử {so_ma_da_hien} mã / "
            f"{tong_vnd_da_hien} đ, bây giờ {so_ma_thuc} mã / {tong_vnd_thuc} đ"
        )


class DeNghiHetHan(ThuHoiBiTuChoi):
    """Vé hết hạn, đã bấm, hoặc bị một đề nghị mới của cùng người ghi đè."""

    def __init__(self) -> None:
        super().__init__("Đề nghị thu hồi này đã đóng — không mã nào bị đụng tới.")


class DeNghiCuaNguoiKhac(ThuHoiBiTuChoi):
    """Vé có thật nhưng thuộc người khác. Kiểm TRƯỚC khi tiêu, để không đốt vé của họ."""

    def __init__(self) -> None:
        super().__init__("Đây là đề nghị thu hồi của người khác — chỉ người tạo mới dùng được.")


@dataclass(frozen=True, slots=True)
class MaChoThuHoi:
    """Một mã đã qua đủ hàng rào ĐỌC. Chỉ `kiem_ma_thu_hoi()` dựng được."""

    code_id: int
    #: Lấy từ DÒNG DỮ LIỆU, không từ tham số. Chuỗi đi vào sổ kiểm toán phải đến từ
    #: database, không đến từ ô nhập — nếu không thì sổ ghi lại cái người ta GÕ, và một
    #: ký tự thừa ở đó biến bằng chứng thành thứ không tra cứu được.
    code_value: str
    code_type: str
    value_vnd: int
    status: str


@dataclass(frozen=True, slots=True)
class PhamViThuHoi:
    """Phạm vi đã qua đủ hàng rào ở bước ĐỀ NGHỊ, kèm con số đã đếm tại thời điểm kiểm."""

    code_type: str
    #: `0` = không lọc mệnh giá. Dùng số thay `None` để cùng một tham số đi qua được cả SQL
    #: lẫn payload của vé mà không cần hai đường mã hoá.
    value_vnd: int
    so_ma: int
    tong_vnd: int
    threshold_vnd: int


@dataclass(frozen=True, slots=True)
class DeNghiThuHoi:
    """Nội dung một vé ĐÃ TIÊU. Chỉ `nhan_de_nghi_thu_hoi()` dựng được, và hàm đó chỉ dựng
    được sau một `GETDEL` thành công.

    Mang HAI con số "đã hiện" — số mã VÀ tổng tiền — vì có hai loại số khác nhau:

    * số **quyết định** (có được xoá không, xoá bao nhiêu) → phải đếm lại;
    * số **người ta đã nhìn** → phải là chính nó, đếm lại là mất nó.

    Gộp hai loại làm một là cách 880.000đ biến mất mà không dòng nào kêu.
    """

    actor_id: int
    code_type: str
    value_vnd: int
    so_ma_da_hien: int
    tong_vnd_da_hien: int


@dataclass(frozen=True, slots=True)
class KetQuaThuHoiHangLoat:
    #: Từ `RETURNING`, KHÔNG phải số đã hiện trên trang xác nhận.
    so_ma_da_xoa: int
    tong_vnd: int
    so_ma_da_hien: int
    tong_vnd_da_hien: int

    @property
    def lech(self) -> bool:
        return self.so_ma_da_xoa != self.so_ma_da_hien or self.tong_vnd != self.tong_vnd_da_hien


_SQL_FIND_CODE = """
SELECT c.code_id,
       c.code_value,
       c.status,
       c.code_type,
       c.value_vnd,
       g.grant_id,
       g.state     AS grant_state,
       g.user_id   AS holder_id,
       u.username  AS holder_username,
       u.full_name AS holder_name,
       coalesce(g.delivered_at, g.created_at) AS moc
  FROM codes c
  LEFT JOIN code_grants g ON g.code_id = c.code_id
  LEFT JOIN users u ON u.user_id = g.user_id
 WHERE c.code_value = :code
"""

_SQL_COUNT_AVAILABLE = """
SELECT count(*)::int                        AS so_ma,
       coalesce(sum(value_vnd), 0)::bigint  AS tong_vnd
  FROM codes
 WHERE code_type = :code_type
   AND status = 'available'
   AND (:value_vnd = 0 OR value_vnd = :value_vnd)
"""


async def kiem_ma_thu_hoi(db: AsyncSession, *, code_value: str) -> MaChoThuHoi:
    """Bốn hàng rào ĐỌC của việc thu hồi một mã, đúng thứ tự bot đang chạy.

    Ném:
        NhieuHonMotMa: ô nhập chứa nhiều hơn một mã.
        KhongTimThayMa · MaDaPhat · MaDaThuHoi.

    Tra bằng **đúng cái token đã đếm**, không bằng chuỗi thô: một ô `<textarea>` gửi lên
    `"ABC123\\r\\n"` — vẫn là một mã — nhưng `WHERE code_value = 'ABC123\\r\\n'` không khớp
    dòng nào, và người vận hành nhận "không tìm thấy" cho một mã CÓ THẬT. Hỏng theo hướng
    đóng, nhưng đó đúng là câu trả lời sai đẩy người ta quay ra gõ tay lệnh hàng loạt với
    phạm vi rộng hơn.

    Không `.lower()`: `uq_codes_value` unique trên chuỗi nguyên bản và mã phân biệt hoa
    thường. `.one_or_none()` chứ không `.first()`: nhiều dòng là dữ liệu hỏng và phải NỔ.
    """
    token = code_value.split()
    if len(token) != 1:
        raise NhieuHonMotMa(so_token=len(token))

    row = (await db.execute(text(_SQL_FIND_CODE), {"code": token[0]})).one_or_none()
    if row is None:
        raise KhongTimThayMa(code_value=token[0])

    if row.grant_id is not None:
        raise MaDaPhat(
            code_value=row.code_value,
            code_type=row.code_type,
            value_vnd=row.value_vnd,
            holder_id=row.holder_id,
            holder_username=row.holder_username,
            holder_name=row.holder_name,
            grant_state=row.grant_state,
            moc=row.moc,
        )

    if row.status == "revoked":
        raise MaDaThuHoi(code_value=row.code_value)

    return MaChoThuHoi(
        code_id=row.code_id,
        code_value=row.code_value,
        code_type=row.code_type,
        value_vnd=row.value_vnd,
        status=row.status,
    )


async def thu_hoi_mot(
    db: AsyncSession, ma: MaChoThuHoi, *, actor_id: int, ip: str | None = None
) -> int:
    """Thu hồi một mã đã qua `kiem_ma_thu_hoi()`. Gọi trong `transaction()`. Trả `code_id`.

    Nhận `MaChoThuHoi` chứ không nhận `code_id: int` — chặn bằng KIỂU, cùng cách
    `stock.nap()` chỉ nhận `LoNapKho`.

    `revoke_one()` trả `None` → `MatCuocDua`, và sổ **không** được ghi: điều kiện
    `status = 'available'` trong chính câu `UPDATE` mới là thứ chặn, nên khi nó sửa 0 dòng
    thì không có việc gì đã xảy ra để mà ghi.
    """
    from televip.services import admin as admin_service

    if await revoke_one(db, code_id=ma.code_id) is None:
        raise MatCuocDua(code_value=ma.code_value)

    await admin_service.write_audit(
        db,
        actor_id=actor_id,
        action="del_code",
        entity_type="code",
        entity_id=str(ma.code_id),
        before={"code_value": ma.code_value, "status": ma.status},
        after={"code_value": ma.code_value, "status": "revoked", "ip": ip},
    )
    return ma.code_id


# ── Thu hồi hàng loạt: đề nghị → vé → thi hành ──────────────────────


async def kiem_pham_vi_thu_hoi(
    db: AsyncSession, *, code_type_tho: str, menh_gia_tho: str
) -> PhamViThuHoi:
    """Bốn hàng rào của bước ĐỀ NGHỊ, đúng thứ tự bot đang chạy.

    ``menh_gia_tho`` là `str`, **không** phải `str | None`: xem `MENH_GIA_TAT_CA`.

    Ném:
        LoaiKhongHopLe · MenhGiaKhongDocDuoc · PhamViRong · VuotNguongThuHoi.
    """
    from televip.db.models.codes import CODE_TYPES
    from televip.services import settings_service
    from televip.services.stock import DEFAULT_DUAL_APPROVAL_VND, DUAL_APPROVAL_KEY, doc_menh_gia

    code_type = code_type_tho.strip().lower()
    if code_type not in CODE_TYPES:
        raise LoaiKhongHopLe(code_type_tho=code_type_tho)

    if menh_gia_tho == MENH_GIA_TAT_CA:
        value_vnd = 0
    else:
        doc = doc_menh_gia(menh_gia_tho) if menh_gia_tho.strip() else None
        if doc is None:
            raise MenhGiaKhongDocDuoc(menh_gia_tho=menh_gia_tho)
        value_vnd = doc

    row = (
        await db.execute(
            text(_SQL_COUNT_AVAILABLE), {"code_type": code_type, "value_vnd": value_vnd}
        )
    ).one()
    if row.so_ma == 0:
        raise PhamViRong(code_type=code_type, value_vnd=value_vnd)

    threshold = await settings_service.get_int(DUAL_APPROVAL_KEY, DEFAULT_DUAL_APPROVAL_VND, db=db)
    if row.tong_vnd > threshold:
        raise VuotNguongThuHoi(tong_vnd=row.tong_vnd, threshold_vnd=threshold, so_ma=row.so_ma)

    return PhamViThuHoi(
        code_type=code_type,
        value_vnd=value_vnd,
        so_ma=row.so_ma,
        tong_vnd=row.tong_vnd,
        threshold_vnd=threshold,
    )


def _khoa_ve(ve: str) -> str:
    return f"{DE_NGHI_PREFIX}{ve}"


def _khoa_chu(actor_id: int) -> str:
    return f"{DE_NGHI_CHU_PREFIX}{actor_id}"


async def tao_de_nghi_thu_hoi(
    db: AsyncSession, *, actor_id: int, pham_vi: PhamViThuHoi, ip: str | None = None
) -> str:
    """Phát một vé dùng MỘT LẦN cho phạm vi này. Gọi trong `transaction()`. Trả chuỗi vé.

    ## Vì sao là vé, chứ không phải phạm vi nhét vào nút / vào trường ẩn

    Bản trước của lệnh Telegram nhét thẳng phạm vi vào `callback_data`, nên cái nút là một
    **mệnh lệnh vĩnh viễn** chứ không phải một quyết định: tin nhắn sống mãi trong lịch sử
    chat, và bấm lại nó vài ngày sau chạy `UPDATE` trên kho của HÔM NAY. Kịch bản đã dựng
    lại được: đề nghị lúc kho có 1 mã 5.000đ → hôm sau nạp 200 mã 88.000đ → cuộn lên bấm
    nút cũ thì thu hồi sạch 17.605.000đ.

    Bản web của đúng chuyện đó là một trường ẩn `loai=event` nằm lại trong tab cũ. Nên
    trang xác nhận **chỉ** mang vé, và phạm vi đọc từ vé.

    Vé mới của một người **giết** vé cũ của chính người đó: ba tab mở ba đề nghị là ba lần
    bấm mà người ta chỉ nhớ một.
    """
    import secrets

    from televip.cache.client import get_redis
    from televip.services import admin as admin_service

    # Vào sổ TRƯỚC khi chạm Redis: hỏng ở Redis thì giao dịch cuộn lại, không còn vé cũng
    # không còn dòng sổ. Cần dòng này vì khi kho bị xoá nhầm, câu hỏi đầu tiên là "lúc bấm
    # màn hình nói bao nhiêu mã" — và chỉ dòng này trả lời được.
    await admin_service.write_audit(
        db,
        actor_id=actor_id,
        action="del_all_code.denghi",
        entity_type="codes",
        entity_id=f"{pham_vi.code_type}:{pham_vi.value_vnd or 'all'}",
        after={"so_ma": pham_vi.so_ma, "tong_vnd": pham_vi.tong_vnd, "ip": ip},
    )

    ve = secrets.token_urlsafe(8)
    redis = get_redis()
    cu = await redis.getset(_khoa_chu(actor_id), ve.encode())
    if cu is not None:
        await redis.delete(_khoa_ve(cu.decode() if isinstance(cu, bytes) else str(cu)))
    await redis.expire(_khoa_chu(actor_id), DE_NGHI_TTL_SECONDS)
    await redis.set(
        _khoa_ve(ve),
        json.dumps(
            {
                "actor_id": actor_id,
                "code_type": pham_vi.code_type,
                "value_vnd": pham_vi.value_vnd,
                "so_ma_da_hien": pham_vi.so_ma,
                "tong_vnd_da_hien": pham_vi.tong_vnd,
            }
        ).encode(),
        ex=DE_NGHI_TTL_SECONDS,
    )
    return ve


async def nhan_de_nghi_thu_hoi(*, ve: str, actor_id: int) -> DeNghiThuHoi:
    """Tiêu vé. Ném `DeNghiHetHan` (hết hạn / đã bấm) hoặc `DeNghiCuaNguoiKhac`.

    `GETDEL` là một lệnh nguyên tử, nên hai lượt bấm đồng thời chỉ một lượt đi qua được —
    đó là toàn bộ cơ chế chống bấm hai lần, kể cả F5 và nút Back.
    """
    from televip.cache.client import get_redis

    redis = get_redis()
    tho = await redis.getdel(_khoa_ve(ve))
    if tho is None:
        raise DeNghiHetHan
    try:
        du_lieu = dict(json.loads(tho))
    except (ValueError, TypeError) as exc:
        raise DeNghiHetHan from exc
    if int(du_lieu.get("actor_id", 0)) != actor_id:
        raise DeNghiCuaNguoiKhac

    await redis.delete(_khoa_chu(actor_id))
    return DeNghiThuHoi(
        actor_id=actor_id,
        code_type=str(du_lieu["code_type"]),
        value_vnd=int(du_lieu["value_vnd"]),
        so_ma_da_hien=int(du_lieu["so_ma_da_hien"]),
        tong_vnd_da_hien=int(du_lieu["tong_vnd_da_hien"]),
    )


async def thu_hoi_hang_loat(
    db: AsyncSession, de_nghi: DeNghiThuHoi, *, ip: str | None = None
) -> KetQuaThuHoiHangLoat:
    """Thi hành một đề nghị ĐÃ TIÊU VÉ. Gọi trong `transaction()`.

    Chỉ nhận `DeNghiThuHoi` — kiểu mà chỉ một `GETDEL` thành công dựng ra được. Nên không
    có đường tắt nào từ "vừa xem thử xong" thẳng tới "đã xoá", và `so_ma_da_hien` không
    phải một tham số tuỳ ý: nó là con số duy nhất ghi lại **con người đã duyệt cái gì**.

    ## Hàng rào tiền đo trên chính tập `RETURNING`

    Đếm bằng một câu `count(*)` rồi `UPDATE` ở câu sau là **hai ảnh chụp** — engine không
    đặt `isolation_level` nên giao dịch chạy ở `READ COMMITTED`, và khe giữa hai câu không
    đóng được bằng cách đếm thêm lần nữa. Thứ duy nhất ràng buộc được một `UPDATE` là một
    vị từ nằm trong chính nó, hoặc một phép đo trên đúng cái nó vừa sửa.

    Nên thứ tự ở đây là: `UPDATE … RETURNING` → đo trên tập trả về → ném nếu vượt. Ngoại lệ
    ném **sau** khi đã ghi, nên nơi gọi **phải** đặt `try/except` NGOÀI `transaction()` để
    giao dịch cuộn lại. Bắt bên trong thì mã đã `revoked` mà màn hình báo "từ chối".

    Ném:
        VuotNguongThuHoi: trần bị hạ xuống trong lúc chờ, hoặc kho vượt trần.
        PhamViDaLonLen: cái sắp xoá lớn hơn cái đã duyệt.
    """
    from televip.services import admin as admin_service
    from televip.services import settings_service
    from televip.services.stock import DEFAULT_DUAL_APPROVAL_VND, DUAL_APPROVAL_KEY

    rows = await revoke_bulk(db, code_type=de_nghi.code_type, value_vnd=de_nghi.value_vnd)
    so_ma = len(rows)
    tong_vnd = sum(r.value_vnd for r in rows)

    # Đọc lại ngưỡng trong CHÍNH giao dịch này: nó có thể đã bị hạ xuống trong 5 phút chờ.
    threshold = await settings_service.get_int(DUAL_APPROVAL_KEY, DEFAULT_DUAL_APPROVAL_VND, db=db)
    if tong_vnd > threshold:
        raise VuotNguongThuHoi(tong_vnd=tong_vnd, threshold_vnd=threshold, so_ma=so_ma)

    # Kho VƠI đi thì dung thứ (có người vừa nhận mã) — chỉ cần nói ra con số thật. Kho LỚN
    # LÊN thì không: cái được thi hành phải không rộng hơn cái con người đã duyệt.
    if so_ma > de_nghi.so_ma_da_hien or tong_vnd > de_nghi.tong_vnd_da_hien:
        raise PhamViDaLonLen(
            so_ma_da_hien=de_nghi.so_ma_da_hien,
            so_ma_thuc=so_ma,
            tong_vnd_da_hien=de_nghi.tong_vnd_da_hien,
            tong_vnd_thuc=tong_vnd,
        )

    if rows:
        await admin_service.write_audit(
            db,
            actor_id=de_nghi.actor_id,
            action="del_all_code",
            entity_type="codes",
            entity_id=f"{de_nghi.code_type}:{de_nghi.value_vnd or 'all'}",
            before={
                "so_ma_da_hien": de_nghi.so_ma_da_hien,
                "tong_vnd_da_hien": de_nghi.tong_vnd_da_hien,
            },
            after={"so_ma_da_xoa": so_ma, "tong_vnd": tong_vnd, "ip": ip},
        )

    return KetQuaThuHoiHangLoat(
        so_ma_da_xoa=so_ma,
        tong_vnd=tong_vnd,
        so_ma_da_hien=de_nghi.so_ma_da_hien,
        tong_vnd_da_hien=de_nghi.tong_vnd_da_hien,
    )


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
