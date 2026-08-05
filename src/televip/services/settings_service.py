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
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.errors import ConfigError
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.db.models.delivery import Setting, SettingsAudit
from televip.domain import texts

log = get_logger(__name__)

#: Tuổi thọ bản sao trong RAM. Xem docstring module về vì sao con số này là 60.
CACHE_TTL_SECONDS: Final[float] = 60.0


class SensitiveSettingLocked(ConfigError):
    """Khoá `sensitive` bị đổi mà không có chữ ký người duyệt thứ hai.

    Là lớp con của `ConfigError` để mọi nơi đang bắt `ConfigError` vẫn bắt được — thêm một
    ngoại lệ mới mà không ai bắt là đổi một lời từ chối gọn gàng thành một vệt 500.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"khoá cấu hình {key!r} cần người duyệt thứ hai (§13.4.1) — chưa đổi được")


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

    ``approved_by`` là người duyệt thứ hai. Với khoá ``sensitive`` nó là **bắt buộc**, và
    điều kiện đó được ép ở ĐÂY.

    Trước bản này, hàm này chỉ *ghi lại* ``approved_by`` vào sổ mà không bao giờ *đòi* nó;
    câu từ chối duy nhất nằm trong handler Telegram (`ops.py`). Đứng yên được suốt thời
    gian chỉ có một tầng gọi — nhưng panel web là tầng gọi thứ hai, và một hàng rào chỉ
    tồn tại ở một trong hai đường vào thì không phải hàng rào. Mười khoá đang được nó che
    gồm `event.prize_table` (xác suất trúng của tiền thật), `code.tanthu_value_vnd` (mệnh
    giá × 19.151 người) và chính `admin.dual_approval_threshold_vnd` — cái ngưỡng bật/tắt
    mọi luật duyệt hai người còn lại.

    Ném:
        SensitiveSettingLocked: khoá ``sensitive`` mà không có ``approved_by``.
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

    # Kiểm TRƯỚC `_validate`: một khoá bị khoá thì lý do từ chối phải là "cần duyệt hai
    # người", không phải "giá trị sai kiểu". Người vận hành gõ lại cho đúng kiểu rồi vẫn
    # bị chặn là một vòng lặp không dẫn tới đâu.
    if row.sensitive and approved_by is None:
        raise SensitiveSettingLocked(key)

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


# ══════════════════════════════════════════════════════════════════════
# Đọc và ghi cho màn hình quản trị
# ══════════════════════════════════════════════════════════════════════
#
# Bốn thứ dưới đây vốn nằm trong `apps/worker/handlers/admin/ops.py`. Chúng đứng yên được
# suốt thời gian chỉ có một màn hình đọc cấu hình. Panel web là màn hình thứ hai, và ba
# trong bốn thứ đó là **hàng rào**, không phải trang trí:
#
# - `is_secret()` quyết định khoá nào bị che. Chép lệch một mẫu là một khoá `*.token` hiện
#   nguyên văn trong HTML — rộng hơn Telegram, vì HTML nằm trong cache trình duyệt.
# - `parse_setting_value()` quyết định `1.5` đọc ra `15` hay `1.500.000` cho một khoá tiền.
# - `_validate()` (ở trên) chặn giá trị ngoài khoảng min/max.
#
# `render_value()` thì đúng là trang trí — nhưng nó đi cùng bộ với ba cái trên, và tách
# riêng là mời người sau viết một bản thứ hai.


#: Khoá có tên khớp một trong các mẫu này thì **giá trị bị che** ở mọi màn hình.
#:
#: Bảng `settings` hiện chưa có khoá nào như vậy (bí mật nằm ở biến môi trường, không nằm
#: trong DB) — mẫu ở đây là hàng rào cho ngày mai, để một khoá `*.token` thêm vào sau này
#: không lặng lẽ hiện nguyên văn.
SECRET_KEY_PATTERNS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "private",
    "pepper",
    "hmac",
)

MASKED: Final = "••• (đã che)"

#: Tên kiểu cho người đọc. Dùng ở thông báo từ chối, nên phải nói được **cách gõ đúng**,
#: không chỉ tên kiểu.
TYPE_HINTS: Final[dict[str, str]] = {
    "int": "số nguyên (ví dụ: 30)",
    "money_vnd": "số tiền VNĐ nguyên (ví dụ: 10000 hoặc 10.000)",
    "bp": "điểm cơ bản, 100 bp = 1% (ví dụ: 250)",
    "seconds": "số giây (ví dụ: 60)",
    "bool": "true hoặc false",
    "string": "chuỗi văn bản",
    "json": 'JSON hợp lệ (ví dụ: [10000, 20000] hoặc {"a": 1})',
}

#: `12.000.000` → 12000000. Chỉ khớp khi MỌI nhóm sau dấu chấm đúng ba chữ số, nên `1.5`
#: KHÔNG lọt qua và không có cách nào để một số thập phân bị đọc nhầm thành hàng triệu.
_VN_THOUSANDS: Final = re.compile(r"^-?\d{1,3}(\.\d{3})+$")

_TRUE_WORDS: Final[frozenset[str]] = frozenset({"true", "1", "on", "yes", "bat", "bật"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"false", "0", "off", "no", "tat", "tắt"})
_INT_TYPES: Final[frozenset[str]] = frozenset({"int", "money_vnd", "bp", "seconds"})

#: Trần độ dài khi in gọn một giá trị JSON.
_RENDER_MAX: Final = 160


class ValueParseError(ValueError):
    """Giá trị người dùng gõ không hợp với `value_type` của khoá.

    Mang theo ``expected`` để tầng hiển thị in ra **kiểu mong đợi**, không phải một câu
    "giá trị không hợp lệ" mà người vận hành phải tự đoán.
    """

    def __init__(self, expected: str) -> None:
        self.expected = expected
        super().__init__(expected)


def is_secret(key: str) -> bool:
    """Khoá này có bị che giá trị không."""
    lowered = key.lower()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


@dataclass(frozen=True, slots=True)
class SettingRow:
    """Một khoá cấu hình, đã sẵn sàng để hiện.

    ``value`` là **None** khi khoá bị che — giá trị thật không rời service. Che ở tầng ĐỌC
    chứ không ở template: một template mới quên gọi hàm che sẽ in nguyên văn, và không có
    gì báo lỗi.
    """

    key: str
    value: Any
    value_type: str
    label_vi: str
    sensitive: bool
    bi_che: bool
    min_value: Any
    max_value: Any
    updated_by: int | None
    updated_at: Any


_SQL_SETTINGS_LIST = """
SELECT key, value, value_type, label_vi, sensitive, min_value, max_value,
       updated_by, updated_at
  FROM settings
 WHERE CAST(:prefix AS text) = '' OR key LIKE CAST(:pattern AS text)
 ORDER BY key
"""


def _dong(r: Any) -> SettingRow:
    che = is_secret(r.key)
    return SettingRow(
        key=r.key,
        value=None if che else r.value,
        value_type=r.value_type,
        label_vi=r.label_vi,
        sensitive=r.sensitive,
        bi_che=che,
        min_value=r.min_value,
        max_value=r.max_value,
        updated_by=r.updated_by,
        updated_at=r.updated_at,
    )


async def list_settings(db: AsyncSession, *, prefix: str = "") -> list[SettingRow]:
    """Khoá cấu hình, lọc theo tiền tố. Đọc **thẳng bảng**, không qua cache.

    Không đọc cache là có chủ ý: bản sao trong RAM có thể cũ tới 60 giây, và một người vừa
    bấm Lưu rồi F5 mà thấy giá trị cũ sẽ tưởng là lưu hụt.
    """
    rows = (
        await db.execute(text(_SQL_SETTINGS_LIST), {"prefix": prefix, "pattern": f"{prefix}%"})
    ).all()
    return [_dong(r) for r in rows]


async def get_setting(db: AsyncSession, key: str) -> SettingRow | None:
    """Một khoá. `None` khi không có — nơi gọi quyết định đó là 404 hay một câu từ chối."""
    rows = await list_settings(db, prefix=key)
    return next((r for r in rows if r.key == key), None)


def parse_setting_value(raw: str, value_type: str) -> Any:
    """Chuỗi người dùng gõ → giá trị Python đúng `value_type` của khoá.

    Ném:
        ValueParseError: gõ sai kiểu. `expected` là hướng dẫn gõ đúng.
    """
    hint = TYPE_HINTS.get(value_type, value_type)
    cleaned = raw.strip()

    if value_type in _INT_TYPES:
        candidate = cleaned.replace("_", "")
        if _VN_THOUSANDS.match(candidate):
            candidate = candidate.replace(".", "")
        if not re.fullmatch(r"-?\d+", candidate):
            raise ValueParseError(hint)
        return int(candidate)

    if value_type == "bool":
        lowered = cleaned.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValueParseError(hint)

    if value_type == "json":
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueParseError(hint) from exc
        if not isinstance(parsed, dict | list):
            # `_TYPE_FAMILIES` chỉ nhận dict/list cho `json`. Chặn ở đây để thông báo nói
            # được kiểu mong đợi thay vì ném `ConfigError` chung chung.
            raise ValueParseError(hint)
        return parsed

    if value_type == "string":
        return cleaned

    raise ValueParseError(hint)


def render_value(value: Any, value_type: str, *, full: bool = False) -> str:
    """Giá trị → chuỗi cho người đọc.

    Khoá tiền in kèm dấu chấm nghìn và chữ `đ`: một `10000` trần trụi đứng cạnh một `10`
    trên cùng màn hình không nói được cái nào là tiền, và đó đúng là kiểu nhầm lẫn mà quy
    ước hiển thị sinh ra để tránh.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value_type == "money_vnd" and isinstance(value, int):
        return f"{texts.format_vnd(value)}đ"
    if isinstance(value, str):
        return value or "(rỗng)"
    if isinstance(value, int | float):
        return str(value)

    blob = json.dumps(value, ensure_ascii=False, separators=(",", ": "))
    if full or len(blob) <= _RENDER_MAX:
        return blob
    return f"{blob[: _RENDER_MAX - 3]}…"


# ── Đường ghi của quản trị ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SettingChange:
    key: str
    truoc: Any
    sau: Any


async def set_by_admin(
    db: AsyncSession,
    key: str,
    value: Any,
    *,
    actor_id: int,
    ip: str | None,
    approved_by: int | None = None,
) -> SettingChange:
    """Đổi một khoá **và** ghi `audit_log`, trong CÙNG giao dịch của nơi gọi.

    Tồn tại để không tầng trình bày nào phải tự nhớ ghép hai bước. Trước bản này, handler
    Telegram tự gọi `set()` rồi `write_audit()`; đường vào thứ hai mà quên bước thứ hai thì
    cấu hình vẫn đổi nhưng sổ kiểm toán im lặng — và câu hỏi "ai nâng `event.budget_cap_vnd`
    lên một tỉ, lúc nào" mất câu trả lời.

    ``db`` phải là session GHI (`transaction()`). Không tự commit: nơi gọi quyết định.
    """
    from televip.services.admin import write_audit

    truoc = await db.get(Setting, key)
    gia_tri_cu = None if truoc is None else truoc.value

    await set(key, value, updated_by=actor_id, approved_by=approved_by, db=db)
    await write_audit(
        db,
        actor_id=actor_id,
        action="setcauhinh",
        entity_type="settings",
        entity_id=key,
        before={"value": gia_tri_cu},
        after={"value": value, "ip": ip},
    )
    return SettingChange(key=key, truoc=gia_tri_cu, sau=value)
