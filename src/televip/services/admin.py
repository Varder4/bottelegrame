"""Phân quyền admin và nhật ký hành động.

`admin_users` × `admin_permissions` là **nguồn sự thật DUY NHẤT** của phân quyền
(`15-quyet-dinh-xay-moi.md` D14, `13-dac-ta-bot-moi.md` §13.4.1). Quyền tra **theo
`user_id` của người gửi**, không bao giờ theo chat chứa lệnh.

Hệ cũ viết `is_admin()` thành một phép so id chat mà không kiểm người gửi
(`handlers/admin.py:21`), nên ai lọt được vào nhóm là có `/add_giffcode` và `/broadcast`.
147.990.000đ đã đi qua cánh cửa đó. Vì vậy **file này không đọc một biến môi trường nào**:
id nhóm nhận cảnh báo là nơi gửi thông báo, nó không cấp quyền, và không được xuất hiện
trong bất kỳ nhánh `if` nào ở đây. Có test đọc chính source của file này để khẳng định
điều đó (`tests/test_admin_permissions.py`).

## Vì sao từ chối thì IM LẶNG

`admin_command` **không trả lời gì** cho người không có quyền. Một câu "bạn không có
quyền dùng lệnh này" là lời xác nhận rằng lệnh đó **có tồn tại** — đủ để người ngoài dò
ra toàn bộ bề mặt lệnh admin chỉ bằng cách gõ thử. Dấu vết của lượt từ chối không mất đi:
nó nằm ở `audit_log` với hành động `<lệnh>.denied`, chỗ vận hành đọc được còn người gõ
thì không.

## Cache vai trò — 30 giây

Mỗi lệnh admin đều phải hỏi "người này là ai", nên vai trò được giữ trong RAM 30 giây thay
vì hỏi database mỗi lượt.

Cái giá phải trả, nói thẳng ở đây để không ai bất ngờ: sau `revoke()`, một tiến trình
**khác** còn coi người đó là admin **tối đa 30 giây**. Tiến trình vừa thu hồi thì thấy
ngay, vì `revoke()` xoá cache của chính nó. Ba mươi giây thay vì 60 của `settings_service`
đúng vì đây là hàng rào quyền chứ không phải một con số nghiệp vụ. Chỗ cần chặn tức thì
tuyệt đối là `user_bans` — bảng đó đọc thẳng database, không đi qua đây.

## Bản đồ vai trò → lệnh KHÔNG nằm ở đây

Nó là dữ liệu trong bảng `admin_permissions`, seed một lần bởi migration `0003_seed_admin`.
Giữ một bản sao dict trong module này là dựng lại đúng cái mà D14 đã loại bỏ: hai nguồn
cho cùng một luật, và `/help_admin` in ra bản không phải bản đang chặn.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.clock import now_utc
from televip.core.errors import PermissionDenied
from televip.core.logging import get_logger
from televip.db.engine import session, transaction

log = get_logger(__name__)

#: Bốn vai trò của §13.4.1. Phải khớp CHECK `ck_admin_users_role` và
#: `ck_admin_permissions_role` — lệch nhau thì lỗi nổ ở tầng database chứ không ở đây.
ROLES: Final[tuple[str, ...]] = ("owner", "admin", "cskh", "ketoan")

#: Tuổi thọ một dòng cache vai trò. Xem docstring module về vì sao con số này là 30.
ROLE_CACHE_TTL_SECONDS: Final[float] = 30.0

#: Trần số dòng cache. Mỗi người lạ gõ thử một lệnh admin là một khoá mới, nên không có
#: trần thì dict này lớn theo số NGƯỜI DÙNG (19.151 ở hệ cũ) chứ không theo số admin.
_MAX_CACHE_ENTRIES: Final[int] = 4096

#: user_id → (hạn dùng theo `time.monotonic()`, vai trò). Vai trò `None` cũng được cache:
#: đó là câu trả lời của đại đa số lượt gọi, và nó fail-closed — cache sai chỉ gây từ chối.
_role_cache: dict[int, tuple[float, str | None]] = {}

_SQL_ROLE = """
SELECT role
  FROM admin_users
 WHERE user_id = :uid
   AND revoked_at IS NULL
"""

_SQL_CAN_RUN = """
SELECT EXISTS (
    SELECT 1
      FROM admin_users au
      JOIN admin_permissions ap ON ap.role = au.role
     WHERE au.user_id = :uid
       AND au.revoked_at IS NULL
       AND ap.command = :cmd
)
"""

_SQL_COMMANDS_FOR_ROLE = """
SELECT command
  FROM admin_permissions
 WHERE role = :role
 ORDER BY command
"""

_SQL_USER_EXISTS = "SELECT 1 FROM users WHERE user_id = :uid"

_SQL_PEEK = "SELECT role, revoked_at FROM admin_users WHERE user_id = :uid FOR UPDATE"

_SQL_GRANT = """
INSERT INTO admin_users (user_id, role, added_by, added_at, revoked_at)
     VALUES (:uid, :role, :by, :now, NULL)
ON CONFLICT (user_id) DO UPDATE
        SET role       = EXCLUDED.role,
            added_by   = EXCLUDED.added_by,
            added_at   = EXCLUDED.added_at,
            revoked_at = NULL
"""

_SQL_REVOKE = """
UPDATE admin_users
   SET revoked_at = :now
 WHERE user_id = :uid
   AND revoked_at IS NULL
RETURNING role
"""

_SQL_AUDIT = """
INSERT INTO audit_log (actor_type, actor_id, action, entity_type, entity_id, before, after)
     VALUES (:actor_type, :actor_id, :action, :entity_type, :entity_id,
             CAST(:before AS jsonb), CAST(:after AS jsonb))
  RETURNING log_id
"""


@dataclass(frozen=True, slots=True)
class RoleChange:
    """Kết quả của `grant_role` / `revoke` — đủ để lệnh admin trả lời người gõ."""

    user_id: int
    old_role: str | None
    new_role: str | None
    #: True khi lệnh không đổi gì (gỡ quyền một người vốn không phải admin).
    unchanged: bool = False


def normalize_command(command: str) -> str:
    """`'stats'` và `'/stats'` là cùng một lệnh. Bảng lưu dạng có dấu gạch chéo (§13.4.1)."""
    stripped = command.strip()
    return stripped if stripped.startswith("/") else f"/{stripped}"


# ── Đọc quyền ───────────────────────────────────────────────────────


async def get_role(db: AsyncSession, user_id: int) -> str | None:
    """Vai trò đang có hiệu lực, hoặc `None` nếu không phải admin (hoặc đã bị thu hồi).

    `revoked_at IS NULL` là định nghĩa của "đang có hiệu lực": thu hồi quyền là ĐIỀN cột
    đó chứ không `DELETE` — mất dòng là mất bằng chứng ai từng có quyền gì.
    """
    cached = _role_cache.get(user_id)
    # `time.monotonic()` chứ không phải `now_utc()`: đây là phép đo KHOẢNG, và một lần NTP
    # chỉnh giờ lùi lại sẽ làm dòng cache treo quá hạn nếu so bằng đồng hồ treo tường.
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]

    role = (await db.execute(text(_SQL_ROLE), {"uid": user_id})).scalar_one_or_none()
    _remember(user_id, role)
    return role


async def can_run(db: AsyncSession, user_id: int, command: str) -> bool:
    """Người này có được chạy `command` không.

    Chặn sớm bằng cache vai trò: người không phải admin — tức gần như mọi lượt gọi — trả
    lời được mà **không** chạm database. Chỉ admin thật mới trả giá một lượt join.

    Thiếu hàng trong `admin_permissions` là **từ chối**: quyền chưa seed thì không ai dùng
    được lệnh, thay vì mọi người dùng được.
    """
    if await get_role(db, user_id) is None:
        return False
    row = await db.execute(text(_SQL_CAN_RUN), {"uid": user_id, "cmd": normalize_command(command)})
    return bool(row.scalar_one())


async def require_permission(db: AsyncSession, user_id: int, command: str) -> None:
    """Ném `PermissionDenied` nếu không đủ quyền.

    Bản dùng cho chỗ gọi **ngoài** handler Telegram (script, tầng web). Trong handler thì
    dùng `@admin_command`, nó bắt đúng lỗi này rồi im lặng.
    """
    if not await can_run(db, user_id, command):
        raise PermissionDenied(user_id, normalize_command(command))


async def commands_for_role(db: AsyncSession, role: str) -> list[str]:
    """Danh sách lệnh của một vai trò — nguồn dữ liệu DUY NHẤT của `/help_admin`.

    `/help_admin` phải đọc từ đây chứ không phải một danh sách cứng: hai chỗ sẽ trôi khác
    nhau ngay lần đầu ai đó đổi quyền bằng lệnh, và khi đó bản in ra là bản sai.
    """
    return list((await db.execute(text(_SQL_COMMANDS_FOR_ROLE), {"role": role})).scalars())


async def user_exists(db: AsyncSession, user_id: int) -> bool:
    """`admin_users.user_id` có FK sang `users`, nên phải kiểm trước khi INSERT.

    Người chưa từng `/start` không có hàng trong `users`; không kiểm ở đây thì lỗi hiện ra
    dưới dạng `ForeignKeyViolationError` — thứ không nói cho ai biết phải làm gì tiếp.
    """
    return (await db.execute(text(_SQL_USER_EXISTS), {"uid": user_id})).first() is not None


def invalidate_role(user_id: int | None = None) -> None:
    """Xoá cache vai trò của một người, hoặc của tất cả khi không truyền tham số."""
    if user_id is None:
        _role_cache.clear()
    else:
        _role_cache.pop(user_id, None)


# ── Ghi quyền ───────────────────────────────────────────────────────


async def grant_role(db: AsyncSession, *, user_id: int, role: str, added_by: int) -> RoleChange:
    """Cấp (hoặc đổi) vai trò, ghi `audit_log`. Gọi trong `transaction()`.

    Cấp lại cho người từng bị thu hồi thì xoá `revoked_at` và giữ nguyên dòng cũ: lịch sử
    nằm ở `audit_log`, không nằm ở việc xoá đi ghi lại.

    Ném:
        ValueError: vai trò không nằm trong `ROLES`.
    """
    if role not in ROLES:
        raise ValueError(f"vai trò không hợp lệ: {role!r} (hợp lệ: {', '.join(ROLES)})")

    # `FOR UPDATE` để hai owner cùng đổi vai trò một người phải xếp hàng: nếu không, cả hai
    # đọc cùng một `before` và sổ kiểm toán ghi lại một lịch sử không có thật.
    prev = (await db.execute(text(_SQL_PEEK), {"uid": user_id})).one_or_none()
    old_role = prev.role if prev is not None and prev.revoked_at is None else None

    await db.execute(
        text(_SQL_GRANT), {"uid": user_id, "role": role, "by": added_by, "now": now_utc()}
    )
    await write_audit(
        db,
        actor_id=added_by,
        action="admin.grant_role",
        entity_type="admin_user",
        entity_id=str(user_id),
        before=None if prev is None else {"role": prev.role, "active": old_role is not None},
        after={"role": role, "active": True},
    )

    invalidate_role(user_id)
    log.info("admin_cap_quyen", user_id=user_id, role=role, added_by=added_by)
    return RoleChange(user_id=user_id, old_role=old_role, new_role=role)


async def revoke(db: AsyncSession, *, user_id: int, revoked_by: int) -> RoleChange:
    """Thu hồi quyền bằng `revoked_at`, KHÔNG xoá dòng. Ghi `audit_log`.

    Gọi trong `transaction()`. Không có gì để thu hồi (không phải admin, hoặc đã bị thu
    hồi từ trước) thì trả `unchanged=True` và **không** sinh dòng nhật ký: không có gì đổi
    thì không có gì để ghi.
    """
    revoked_role = (
        await db.execute(text(_SQL_REVOKE), {"uid": user_id, "now": now_utc()})
    ).scalar_one_or_none()

    if revoked_role is None:
        return RoleChange(user_id=user_id, old_role=None, new_role=None, unchanged=True)

    await write_audit(
        db,
        actor_id=revoked_by,
        action="admin.revoke",
        entity_type="admin_user",
        entity_id=str(user_id),
        before={"role": revoked_role, "active": True},
        after={"role": revoked_role, "active": False},
    )

    invalidate_role(user_id)
    log.info("admin_thu_hoi_quyen", user_id=user_id, role=revoked_role, revoked_by=revoked_by)
    return RoleChange(user_id=user_id, old_role=revoked_role, new_role=None)


# ── Nhật ký ─────────────────────────────────────────────────────────


async def write_audit(
    db: AsyncSession,
    *,
    actor_id: int,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> int:
    """Một dòng `audit_log` với `actor_type = 'admin'`. Trả về `log_id`.

    Gọi trong **cùng giao dịch** với thao tác nó mô tả — đó là điều kiện để sổ không bao
    giờ nói dối: hoặc cả thao tác lẫn dòng sổ cùng có, hoặc cả hai cùng không. Hệ cũ ghi
    sổ ở một giao dịch riêng và `admin_logs` chỉ còn 283 dòng cho toàn bộ vòng đời.

    Sổ này append-only: không có hàm sửa và không có hàm xoá. Hệ cũ chỉ ghi
    `admin_id / action / details` với `details` là chuỗi tự do, nên có khiếu nại là không
    trả lời được "ai đổi cái gì thành cái gì" — `before`/`after` tồn tại vì lý do đó.
    """
    return await log_action(
        db,
        action=action,
        actor_id=actor_id,
        actor_type="admin",
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
    )


async def log_action(
    db: AsyncSession,
    *,
    action: str,
    actor_id: int | None,
    actor_type: str = "admin",
    entity_type: str | None = None,
    entity_id: str | None = None,
    before: Any = None,
    after: Any = None,
) -> int:
    """Như `write_audit` nhưng cho phép `actor_type='system'` — job nền, không có người gõ.

    `actor_id` khi đó là `None`. CHECK `ck_audit_log_actor_type` chỉ nhận hai giá trị
    `'admin'` và `'system'`.
    """
    row = await db.execute(
        text(_SQL_AUDIT),
        {
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before": None if before is None else json.dumps(before, ensure_ascii=False),
            "after": None if after is None else json.dumps(after, ensure_ascii=False),
        },
    )
    return int(row.scalar_one())


# ── Decorator cho handler ───────────────────────────────────────────

#: Chữ ký handler của python-telegram-bot.
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def admin_command(name: str, *, mutates: bool = True) -> Callable[[Handler], Handler]:
    """Bọc một handler lệnh admin: kiểm quyền, im lặng khi từ chối, ghi `audit_log`.

    ``name`` là chuỗi khớp `admin_permissions.command` (`'/broadcast'`). Không có bảng ánh
    xạ trung gian: tên trong code và tên trong database là một, nên thiếu quyền là thiếu
    một dòng dữ liệu chứ không phải một lỗi im lặng trong code.

    ``mutates`` mặc định **True** — quên khai báo thì hậu quả là một dòng nhật ký thừa,
    không phải một hành động đổi trạng thái không có dấu vết. Lệnh chỉ đọc (`/stats`,
    `/codes`, `/tonkho`) khai `mutates=False` để lượt xem không nhấn chìm sổ.

    Dòng nhật ký được ghi **trước** khi handler chạy. Ghi sau thì một handler nổ giữa
    chừng — sau khi đã đụng vào kho code — không để lại dấu vết nào; ghi trước thì tệ nhất
    là có một lượt gọi được ghi mà không hoàn tất, và đó vẫn là điều cần biết. Trạng thái
    trước/sau của từng đối tượng là việc của chính handler, qua `write_audit`.
    """
    command = normalize_command(name)

    def decorate(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            tg_user = update.effective_user
            if tg_user is None:
                # Tin nhắn kênh không có người gửi. Không có `user_id` thì không có quyền —
                # đây đúng là kẽ hở hệ cũ để hở khi kiểm nơi gửi thay vì kiểm người gửi.
                return

            # `context.args` là None với handler không phải CommandHandler.
            args = list(getattr(context, "args", None) or ())

            try:
                async with session() as db:
                    await require_permission(db, tg_user.id, command)
            except PermissionDenied as exc:
                async with transaction() as db:
                    await write_audit(
                        db,
                        actor_id=exc.user_id,
                        action=f"{command}.denied",
                        entity_type="command",
                        entity_id=command,
                        after={"args": args},
                    )
                log.warning("tu_choi_quyen", user_id=exc.user_id, command=command)
                return  # IM LẶNG — xem docstring module

            if mutates:
                async with transaction() as db:
                    await write_audit(
                        db,
                        actor_id=tg_user.id,
                        action=command,
                        entity_type="command",
                        entity_id=command,
                        after={"args": args},
                    )

            await handler(update, context)

        return wrapper

    return decorate


# ── Nội bộ ──────────────────────────────────────────────────────────


def _remember(user_id: int, role: str | None) -> None:
    if len(_role_cache) >= _MAX_CACHE_ENTRIES:
        now = time.monotonic()
        for uid in [uid for uid, (expires, _) in _role_cache.items() if expires <= now]:
            del _role_cache[uid]
        if len(_role_cache) >= _MAX_CACHE_ENTRIES:
            # Toàn bộ cache còn hạn mà vẫn chạm trần: thà mất cache (một truy vấn mỗi lệnh)
            # còn hơn để dict lớn không giới hạn trong tiến trình chạy hàng tháng.
            _role_cache.clear()
    _role_cache[user_id] = (time.monotonic() + ROLE_CACHE_TTL_SECONDS, role)


__all__ = [
    "ROLES",
    "ROLE_CACHE_TTL_SECONDS",
    "Handler",
    "RoleChange",
    "admin_command",
    "can_run",
    "commands_for_role",
    "get_role",
    "grant_role",
    "invalidate_role",
    "log_action",
    "normalize_command",
    "require_permission",
    "revoke",
    "user_exists",
    "write_audit",
]
