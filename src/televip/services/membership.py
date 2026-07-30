"""Tư cách thành viên nhóm/kênh bắt buộc — đọc bảng trước, hỏi Telegram sau.

Nguồn sự thật: `docs/ke-hoach-v2/05-groups.md` phần A, `13-dac-ta-bot-moi.md` §13.2.3
bước 4.

## Vì sao file này tồn tại

Bot cũ gọi `getChatMember` cho **mọi** user × **mọi** nhóm ở **mỗi** lần bấm nút. Đo
trên `log.txt` thật: 142 lời gọi trong 23 giây, **90,4% toàn bộ lưu lượng API** đi hỏi
đúng một câu "user này có trong nhóm không". Tệ hơn, `group_checker.py:35-37` trả `False`
khi timeout — nghĩa là mạng chậm thì bot nói với người **đã vào đủ nhóm** rằng "Tiến độ:
0/2" và từ chối tiền của họ.

Ở đây câu hỏi đó được trả lời bằng một truy vấn chỉ số trên `group_memberships`. Bảng
được nuôi bằng update `chat_member` mà Telegram **đẩy tới miễn phí** (xem
`handle_chat_member_update`), nên đường thường trực tốn **0 lời gọi API**.

`getChatMember` chỉ còn là đường dự phòng cho đúng hai trường hợp: chưa có bản ghi nào,
hoặc bản ghi đã quá hạn tin cậy (`settings.membership.cache_ttl_seconds`). Không kết luận
được thì **ném `MembershipUnavailable`** chứ không âm thầm trả "chưa vào nhóm" — đó chính
là lỗi âm tính giả ở trên.

## Ba điều kiện tiên quyết của thiết kế (05-groups A3)

1. Bot phải là **admin** trong mọi chat bắt buộc, nếu không Telegram không gửi sự kiện.
2. `allowed_updates` phải liệt kê **tường minh** `chat_member` (đã có ở `worker/main.py`).
3. `my_chat_member` phải được xử lý để biết lúc bot bị gỡ quyền — việc đó thuộc scheduler
   sức khoẻ nhóm, chưa nằm trong khối này.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from sqlalchemy import Row, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.errors import TelevipError
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.db.models.delivery import RequiredChat
from televip.services import settings_service

log = get_logger(__name__)

#: Ba trạng thái được coi là "đang ở trong chat". `restricted` **không** nằm đây: người bị
#: hạn chế có thể vẫn là thành viên hoặc đã bị đẩy ra, phân biệt bằng cờ `is_member` của
#: chính đối tượng `ChatMember` — xem `member_flag()`.
JOINED_STATUSES: frozenset[str] = frozenset({"member", "administrator", "creator"})

#: Khoá cấu hình cho tuổi thọ một bản ghi membership. **Chưa được seed** ở migration 0002
#: (nhóm khoá này không có trong §13.6), nên mọi lượt đọc phải kèm default.
CACHE_TTL_KEY = "membership.cache_ttl_seconds"
DEFAULT_CACHE_TTL_SECONDS = 300


class MembershipUnavailable(TelevipError):
    """Không kết luận được tư cách thành viên của một chat bắt buộc.

    Ném khi bảng không có gì để tin **và** `getChatMember` cũng lỗi (bot bị gỡ quyền
    admin, `chat_id` chết, hoặc mạng hỏng). §13.2.3 bước 4 nói rõ: không được chặn người
    dùng trong tình huống này — tầng gọi phải trả lời "hệ thống đang bận" và cảnh báo
    admin, vì đây là lỗi cấu hình của mình chứ không phải lỗi của người thật.
    """

    retryable = True

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        super().__init__(f"Không xác định được tư cách thành viên ở chat {chat_id}")


class ChatMemberFetcher(Protocol):
    """Phần giao diện `telegram.Bot` mà file này dùng.

    Khai báo hẹp như vậy để test truyền được bản giả, và để thấy ngay rằng đây là lời gọi
    Bot API **duy nhất** nằm ngoài `telegram/sender.py` — một ngoại lệ có chủ ý, ghi lại
    ở đây thay vì giấu đi.
    """

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any: ...


# ── Đọc cấu hình nhóm ───────────────────────────────────────────────


async def required_chats(db: AsyncSession) -> list[Row[Any]]:
    """Danh sách nhóm/kênh bắt buộc đang bật, theo `sort_order`.

    Thứ tự quan trọng vì nó là thứ tự đánh số 1️⃣2️⃣3️⃣ trên màn hình bước 2; `chat_id` là
    khoá phụ để hai nhóm cùng `sort_order` không đổi chỗ giữa hai lần bấm.
    """
    result = await db.execute(
        select(
            RequiredChat.chat_id,
            RequiredChat.title,
            RequiredChat.invite_link,
            RequiredChat.health,
            RequiredChat.sort_order,
        )
        .where(RequiredChat.is_active)
        .order_by(RequiredChat.sort_order, RequiredChat.chat_id)
    )
    return list(result.all())


# ── Kiểm tư cách thành viên ─────────────────────────────────────────

#: Một câu duy nhất trả lời "người này thiếu nhóm nào". `LEFT JOIN` để chat chưa có bản
#: ghi vẫn xuất hiện trong kết quả (và rơi vào nhánh hỏi lại), thay vì biến mất im lặng.
_SQL_SNAPSHOT = """
SELECT r.chat_id,
       m.status                                   AS status,
       COALESCE(m.is_member, false)               AS is_member,
       (m.updated_at IS NOT NULL
        AND m.updated_at > now() - make_interval(secs => :ttl)
        AND (m.trust_until IS NULL OR m.trust_until > now())) AS fresh
  FROM required_chats r
  LEFT JOIN group_memberships m
         ON m.chat_id = r.chat_id
        AND m.user_id = :uid
 WHERE r.is_active
 ORDER BY r.sort_order, r.chat_id
"""


async def check_membership(
    db: AsyncSession,
    bot: ChatMemberFetcher,
    user_id: int,
) -> tuple[list[int], int, int]:
    """Trả `(chat_id còn thiếu, số nhóm đã vào, tổng số nhóm)`.

    Gọi trong `transaction()`: nhánh dự phòng ghi lại kết quả `getChatMember` vào bảng,
    nên lần bấm sau của cùng người đó không tốn lời gọi nào nữa.

    Ném:
        MembershipUnavailable: một chat bắt buộc không tra được bằng cả hai đường.
    """
    ttl = float(await settings_service.get_int(CACHE_TTL_KEY, DEFAULT_CACHE_TTL_SECONDS, db=db))
    rows = (await db.execute(text(_SQL_SNAPSHOT), {"uid": user_id, "ttl": ttl})).all()

    total = len(rows)
    if total == 0:
        return [], 0, 0

    missing: list[int] = []
    joined = 0
    stale = [row for row in rows if not row.fresh]

    for row in rows:
        if not row.fresh:
            continue
        if row.is_member:
            joined += 1
        else:
            missing.append(row.chat_id)

    if not stale:
        return missing, joined, total

    # Hỏi song song: N nhóm là N lời gọi, không phải N lượt chờ nối đuôi nhau.
    answers = await asyncio.gather(
        *(bot.get_chat_member(chat_id=row.chat_id, user_id=user_id) for row in stale),
        return_exceptions=True,
    )

    for row, answer in zip(stale, answers, strict=True):
        if isinstance(answer, BaseException):
            if row.status is None:
                # Không có gì để tin và cũng không hỏi được. Đây là chỗ bot cũ trả `False`
                # và từ chối người đã vào đủ nhóm; ở đây ta nói thật là mình đang hỏng.
                log.error(
                    "khong_tra_duoc_tu_cach_thanh_vien",
                    chat_id=row.chat_id,
                    user_id=user_id,
                    detail=str(answer),
                )
                raise MembershipUnavailable(row.chat_id) from answer
            # Có bản ghi cũ: dùng tạm còn hơn từ chối oan. Ghi log để đo `fallback_rate`.
            log.warning(
                "dung_ban_ghi_membership_cu",
                chat_id=row.chat_id,
                user_id=user_id,
                status=row.status,
                detail=str(answer),
            )
            if row.is_member:
                joined += 1
            else:
                missing.append(row.chat_id)
            continue

        status = answer.status
        is_member = member_flag(answer)
        await record_membership_event(
            db,
            user_id=user_id,
            chat_id=row.chat_id,
            old_status=row.status,
            new_status=status,
            is_member=is_member,
            source="poll",
        )
        if is_member:
            joined += 1
        else:
            missing.append(row.chat_id)

    return missing, joined, total


def is_joined(status: str, *, restricted_is_member: bool = False) -> bool:
    """Trạng thái này có tính là "đã tham gia" không.

    `restricted` là trạng thái duy nhất mơ hồ: người bị hạn chế quyền nói vẫn đang ở trong
    nhóm, còn người bị hạn chế **và** đẩy ra thì không. Telegram phân biệt hai ca đó bằng
    cờ `is_member`, nên chuỗi trạng thái một mình không đủ để quyết định.
    """
    if status == "restricted":
        return restricted_is_member
    return status in JOINED_STATUSES


def member_flag(chat_member: Any) -> bool:
    """`is_member` suy từ một đối tượng `ChatMember` của thư viện."""
    return is_joined(
        chat_member.status, restricted_is_member=bool(getattr(chat_member, "is_member", False))
    )


# ── Ghi trạng thái ──────────────────────────────────────────────────

_SQL_UPSERT_MEMBERSHIP = """
INSERT INTO group_memberships (user_id, chat_id, status, is_member, source, updated_at)
     VALUES (:uid, :cid, :status, :is_member, :source, now())
ON CONFLICT (user_id, chat_id) DO UPDATE
        SET status     = EXCLUDED.status,
            is_member  = EXCLUDED.is_member,
            source     = EXCLUDED.source,
            updated_at = now()
"""

_SQL_APPEND_EVENT = """
INSERT INTO membership_events (user_id, chat_id, old_status, new_status)
     VALUES (:uid, :cid, :old_status, :new_status)
"""


async def record_membership_event(
    db: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    old_status: str | None,
    new_status: str,
    is_member: bool | None = None,
    source: str = "event",
) -> None:
    """Cập nhật ảnh chụp (`group_memberships`) và ghi nhật ký (`membership_events`).

    Hai bảng, hai vai trò khác hẳn nhau: bảng đầu là trạng thái HIỆN TẠI dùng để quyết
    định, bảng sau là lịch sử **chỉ ghi thêm** dùng để ĐO (05-groups A4b — tỉ lệ rời nhóm
    sau khi nhận code). Nhật ký chỉ nhận dòng khi trạng thái thật sự đổi, nếu không mỗi
    lượt hỏi lại `getChatMember` sẽ đẻ một dòng "không có gì xảy ra".

    ⚠️ Không có nhánh nào ở đây thu hồi code của người rời nhóm. Đó là quyết định đã chốt
    (05-groups A4b): thu hồi tự động là hành vi MỚI, và một luật sai sẽ trừ oan hàng nghìn
    người thật trong một lệnh.
    """
    flag = is_member if is_member is not None else is_joined(new_status)

    await db.execute(
        text(_SQL_UPSERT_MEMBERSHIP),
        {
            "uid": user_id,
            "cid": chat_id,
            "status": new_status,
            "is_member": flag,
            "source": source,
        },
    )

    if old_status != new_status:
        await db.execute(
            text(_SQL_APPEND_EVENT),
            {
                "uid": user_id,
                "cid": chat_id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )


# ── Handler update `chat_member` ────────────────────────────────────

_SQL_IS_REQUIRED = "SELECT 1 FROM required_chats WHERE chat_id = :cid AND is_active"


async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hứng update `chat_member` — thứ giữ bảng luôn tươi mà **không tốn lời gọi API nào**.

    Telegram đẩy loại update này mỗi khi có người vào/rời một chat mà bot làm admin. Khối
    lượng thật đo được là ~0,011 sự kiện/giây, tức bằng không so với ngân sách 30 tin/giây.

    Chỉ ghi cho chat nằm trong `required_chats`: `group_memberships.chat_id` có khoá ngoại
    trỏ về bảng đó, nên một sự kiện từ nhóm lạ sẽ làm câu INSERT nổ.
    """
    cmu = update.chat_member
    if cmu is None:
        return

    chat_id = cmu.chat.id
    new_member = cmu.new_chat_member
    old_member = cmu.old_chat_member
    user_id = new_member.user.id

    async with transaction() as db:
        if (await db.execute(text(_SQL_IS_REQUIRED), {"cid": chat_id})).first() is None:
            return

        await record_membership_event(
            db,
            user_id=user_id,
            chat_id=chat_id,
            old_status=old_member.status if old_member is not None else None,
            new_status=new_member.status,
            is_member=member_flag(new_member),
            source="event",
        )

    log.info(
        "chat_member",
        user_id=user_id,
        chat_id=chat_id,
        old_status=old_member.status if old_member is not None else None,
        new_status=new_member.status,
    )


__all__ = [
    "CACHE_TTL_KEY",
    "DEFAULT_CACHE_TTL_SECONDS",
    "JOINED_STATUSES",
    "ChatMemberFetcher",
    "MembershipUnavailable",
    "check_membership",
    "handle_chat_member_update",
    "is_joined",
    "member_flag",
    "record_membership_event",
    "required_chats",
]
