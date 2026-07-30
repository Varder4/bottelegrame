"""Cấu hình nghiệp vụ đọc từ database — hiện thực của nguyên tắc N2.

**Không một con số nghiệp vụ nào được viết trong code.** Mệnh giá, tỉ lệ, mốc, giới hạn,
thời gian chờ đều là một dòng trong bảng `settings`, đổi bằng lệnh admin, có hiệu lực
trong vòng 60 giây, không restart tiến trình nào (`13-dac-ta-bot-moi.md` §13.6).

## Vì sao cache 60 giây, và vì sao KHÔNG có pub/sub

Bảng này có vài chục dòng và bị đọc ở gần như mọi lượt bấm nút. Đọc thẳng database mỗi
lần là hàng nghìn truy vấn/phút cho dữ liệu gần như không đổi. Nên mỗi tiến trình giữ
một bản sao trong RAM, hết hạn sau **60 giây**.

Hệ quả cố ý: sau khi admin gõ `/setcauhinh`, các worker khác còn đọc giá trị cũ **tối đa
60 giây**. Đó là **độ trễ chấp nhận được để đổi cấu hình lan ra mọi worker mà không cần
cơ chế pub/sub** — không cần `LISTEN/NOTIFY`, không cần kênh Redis, không cần một tiến
trình nền giữ kết nối chỉ để nghe. Đổi lại là một hằng số duy nhất và không có trạng thái
phân tán nào để hỏng. Worker vừa ghi thì thấy ngay, vì `set()` xoá cache của chính nó.

Cái giá này chỉ chấp nhận được vì **không khoá nào ở đây là hàng rào an toàn tức thời**:
chặn một người dùng gian lận là việc của `user_bans` (đọc thẳng database), không phải của
bảng cấu hình. Khoá tệ nhất có thể lệch 60 giây là một mệnh giá — tức vài chục nghìn đồng.

## Đường ghi

`set()` ghi `settings` và `settings_audit` trong **cùng một giao dịch**. Không có đường
nào đổi cấu hình mà không để lại dấu vết: hệ cũ đổi bằng `nano config.py` trên VPS, và
không ai trả lời được câu "ai đổi tỉ lệ event, lúc nào, từ giá trị nào".
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.db.models.delivery import Setting, SettingsAudit

log = get_logger(__name__)

#: Tuổi thọ bản sao trong RAM. Xem docstring module về vì sao con số này là 60.
CACHE_TTL_SECONDS: Final[float] = 60.0

#: Kiểu Python hợp lệ cho mỗi `settings.value_type` — dùng khi ghi, để một lần gõ nhầm
#: không biến `event.budget_cap_vnd` thành chuỗi rồi làm mọi lượt đọc sau đó ném lỗi.
_TYPE_FAMILIES: Final[dict[str, tuple[type, ...]]] = {
    "int": (int,),
    "money_vnd": (int,),
    "bp": (int,),
    "seconds": (int,),
    "bool": (bool,),
    "string": (str,),
    "json": (dict, list),
}

#: Các `value_type` mang giá trị số — chỉ chúng mới bị chặn bởi `min_value` / `max_value`.
_NUMERIC_TYPES: Final[frozenset[str]] = frozenset({"int", "money_vnd", "bp", "seconds"})


class _Missing:
    """Dấu hiệu 'không truyền default' — phân biệt với `default=None` hợp lệ."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - chỉ để đọc traceback
        return "<không có default>"


_MISSING: Final = _Missing()


# Chỉ cache **giá trị**. Ba cột còn lại (`value_type`, `min/max`, `sensitive`) cố ý KHÔNG
# nằm ở đây: chúng chỉ được dùng ở đường ghi, mà đường ghi đọc thẳng dòng dữ liệu trong
# cùng giao dịch. Quyết định "khoá này có cần hai người duyệt không" mà đọc từ một bản sao
# cũ tới 60 giây thì đúng chỗ nguy hiểm nhất — nên nó phải hỏi database, không hỏi cache.
_cache: dict[str, Any] = {}
# Mốc đo bằng `time.monotonic()` chứ không phải đồng hồ treo tường: một lần NTP chỉnh giờ
# lùi lại sẽ làm cache treo quá 60 giây nếu so bằng `now_utc()`. Đây là phép đo KHOẢNG,
# không phải mốc thời gian nghiệp vụ, nên không thuộc phạm vi của `core.clock`.
_loaded_at: float | None = None
# Nhiều coroutine cùng thấy cache hết hạn sẽ cùng lao vào truy vấn. Khoá này để đúng một
# cái chạy, những cái còn lại chờ rồi đọc kết quả của nó.
_lock: Final[asyncio.Lock] = asyncio.Lock()


# ── Đọc ─────────────────────────────────────────────────────────────


async def get(key: str, default: Any = None, *, db: AsyncSession | None = None) -> Any:
    """Giá trị thô của một khoá, hoặc ``default`` nếu khoá không tồn tại.

    ``db`` chỉ để dùng lại session đang mở (test, hoặc lời gọi bên trong một giao dịch);
    bỏ trống thì hàm tự mở session riêng.
    """
    await _ensure_fresh(db)
    # Tra bằng sentinel chứ không bằng `.get(key)`: JSONB chứa được `null`, nên `None` là
    # một giá trị hợp lệ và không được lẫn với "không có khoá".
    value = _cache.get(key, _MISSING)
    return default if isinstance(value, _Missing) else value


async def get_int(key: str, default: Any = _MISSING, *, db: AsyncSession | None = None) -> int:
    """Ném ``ConfigError`` nếu khoá thiếu (và không có default) hoặc không phải số nguyên."""
    value = await _lookup(key, default, db=db)
    # `bool` là lớp con của `int` trong Python: không loại nó ra thì `true` lọt qua thành 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong_type(key, value, "số nguyên")
    return value


async def get_float(key: str, default: Any = _MISSING, *, db: AsyncSession | None = None) -> float:
    value = await _lookup(key, default, db=db)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _wrong_type(key, value, "số")
    return float(value)


async def get_bool(key: str, default: Any = _MISSING, *, db: AsyncSession | None = None) -> bool:
    value = await _lookup(key, default, db=db)
    if not isinstance(value, bool):
        raise _wrong_type(key, value, "true/false")
    return value


async def get_str(key: str, default: Any = _MISSING, *, db: AsyncSession | None = None) -> str:
    value = await _lookup(key, default, db=db)
    if not isinstance(value, str):
        raise _wrong_type(key, value, "chuỗi")
    return value


async def get_dict(
    key: str, default: Any = _MISSING, *, db: AsyncSession | None = None
) -> dict[str, Any]:
    value = await _lookup(key, default, db=db)
    if not isinstance(value, dict):
        raise _wrong_type(key, value, "đối tượng JSON")
    return value


async def get_list(
    key: str, default: Any = _MISSING, *, db: AsyncSession | None = None
) -> list[Any]:
    value = await _lookup(key, default, db=db)
    if not isinstance(value, list):
        raise _wrong_type(key, value, "mảng JSON")
    return value


async def refresh(*, db: AsyncSession | None = None) -> None:
    """Nạp lại toàn bộ bảng ngay, bỏ qua TTL."""
    async with _lock:
        await _load(db)


def invalidate() -> None:
    """Đánh dấu cache hết hạn; lượt đọc kế tiếp sẽ nạp lại."""
    global _loaded_at
    _loaded_at = None


# ── Ghi ─────────────────────────────────────────────────────────────


async def set(  # noqa: A001 - tên khoá theo đặc tả `/setcauhinh`, cố ý đối xứng với `get`
    key: str,
    value: Any,
    *,
    updated_by: int,
    approved_by: int | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Đổi một khoá cấu hình, ghi `settings` và `settings_audit` trong cùng giao dịch.

    ``approved_by`` là người duyệt thứ hai với khoá ``sensitive`` — việc **thu thập** chữ
    ký thứ hai là của tầng lệnh admin, ở đây chỉ ghi lại nó vào sổ.

    Ném:
        ConfigError: khoá không tồn tại, hoặc giá trị sai kiểu / ngoài khoảng min-max.
    """
    if db is not None:
        await _write(db, key, value, updated_by, approved_by)
    else:
        async with transaction() as own:
            await _write(own, key, value, updated_by, approved_by)

    invalidate()
    log.info("settings.changed", key=key, changed_by=updated_by, approved_by=approved_by)


async def _write(
    db: AsyncSession, key: str, value: Any, updated_by: int, approved_by: int | None
) -> None:
    # `with_for_update` để hai admin đổi cùng một khoá cùng lúc phải xếp hàng: nếu không,
    # cả hai đọc cùng `old_value` và sổ kiểm toán ghi một lịch sử không có thật.
    row = await db.get(Setting, key, with_for_update=True)
    if row is None:
        # Tập khoá là cố định, do migration seed. Tạo khoá mới ở runtime nghĩa là có nơi
        # đang đọc một khoá không ai seed — lỗi đó phải nổ ra chứ không được tự vá.
        raise ConfigError(f"khoá cấu hình {key!r} không tồn tại trong bảng settings")

    _validate(row, value)

    db.add(
        SettingsAudit(
            key=key,
            old_value=row.value,
            new_value=value,
            changed_by=updated_by,
            approved_by=approved_by,
        )
    )
    row.value = value
    row.updated_by = updated_by


def _validate(row: Setting, value: Any) -> None:
    allowed = _TYPE_FAMILIES.get(row.value_type)
    if allowed is None:  # pragma: no cover - CHECK ở database đã chặn
        raise ConfigError(f"settings[{row.key!r}] khai báo value_type lạ: {row.value_type!r}")

    # Kiểm `bool` trước: nó là lớp con của `int`, nên `isinstance(True, (int,))` là True
    # và một khoá tiền có thể nhận `true` mà không ai chặn.
    if isinstance(value, bool) != (row.value_type == "bool"):
        raise _wrong_type(row.key, value, row.value_type)
    if not isinstance(value, allowed):
        raise _wrong_type(row.key, value, row.value_type)

    if row.value_type in _NUMERIC_TYPES:
        if row.min_value is not None and value < row.min_value:
            raise ConfigError(
                f"settings[{row.key!r}] = {value} nhỏ hơn mức tối thiểu {row.min_value}"
            )
        if row.max_value is not None and value > row.max_value:
            raise ConfigError(f"settings[{row.key!r}] = {value} lớn hơn mức tối đa {row.max_value}")


# ── Nội bộ ──────────────────────────────────────────────────────────


async def _lookup(key: str, default: Any, *, db: AsyncSession | None) -> Any:
    await _ensure_fresh(db)
    value = _cache.get(key, _MISSING)
    if not isinstance(value, _Missing):
        return value
    if isinstance(default, _Missing):
        raise ConfigError(f"thiếu khoá cấu hình {key!r} trong bảng settings")
    # Default cũng đi qua bộ kiểm kiểu của hàm gọi — một default sai kiểu là lỗi tại chỗ gọi.
    return default


async def _ensure_fresh(db: AsyncSession | None) -> None:
    if not _is_stale():
        return
    async with _lock:
        # Kiểm lại sau khi giành được khoá: cái đứng trước có thể vừa nạp xong.
        if _is_stale():
            await _load(db)


def _is_stale() -> bool:
    return _loaded_at is None or (time.monotonic() - _loaded_at) >= CACHE_TTL_SECONDS


async def _load(db: AsyncSession | None) -> None:
    global _cache, _loaded_at

    stmt = select(Setting.key, Setting.value)
    if db is not None:
        rows = (await db.execute(stmt)).all()
    else:
        async with session() as own:
            rows = (await own.execute(stmt)).all()

    # Dựng dict mới rồi gán đè, thay vì `clear()` + điền: lượt đọc chen vào giữa không bao
    # giờ nhìn thấy một bảng cấu hình rỗng một nửa.
    _cache = {key: value for key, value in rows}
    _loaded_at = time.monotonic()
    log.debug("settings.loaded", count=len(_cache))


def _wrong_type(key: str, value: Any, expected: str) -> ConfigError:
    return ConfigError(
        f"settings[{key!r}] phải là {expected}, đang là {type(value).__name__} ({value!r})"
    )
