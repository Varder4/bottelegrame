"""Xác thực chữ ký `initData` của Telegram Mini App.

**Đây là lỗ hổng nghiêm trọng nhất của bot cũ.** Ở `api/app.py:424-432` khối kiểm chữ ký
bị comment kèm ghi chú "TEMPORARY DISABLED FOR TESTING" và nằm im như vậy suốt 7 tháng.
Hệ quả: `user_id` được đọc thẳng từ JSON body do client gửi lên, nên một lệnh `curl` với
`user_id` bất kỳ là đủ để đánh dấu đã xác minh và rút code. Mọi lớp chặn IP/VPN phía sau
đều vô nghĩa vì kẻ gian không cần mở bot.

## Thuật toán (theo tài liệu Telegram)

1. Tách `initData` (chuỗi query string) thành các cặp khoá-giá trị, **có url-decode**.
2. Lấy `hash` ra khỏi tập đó.
3. Ghép phần còn lại thành `data_check_string`: `key=value` sắp xếp theo `key`, nối bằng `\\n`.
4. `secret_key = HMAC_SHA256(key="WebAppData", data=<bot_token>)`
5. So sánh `HMAC_SHA256(key=secret_key, data=data_check_string)` với `hash`.

Hàm cũ của bot cũ viết sai ở bước 1: nó tách chuỗi bằng `split('&')` rồi `split('=')` mà
**không url-decode**. Trường `user=` là JSON đã percent-encode, nên `data_check_string`
dựng ra khác với thứ Telegram đã ký — bật thẳng hàm đó lên sẽ chặn nhầm 100% người dùng
thật. Ở đây dùng `parse_qsl`, hàm này decode đúng.

## Hai thứ chữ ký hợp lệ vẫn KHÔNG đủ

- **Hết hạn**: `auth_date` cũ hơn TTL thì từ chối. Chữ ký không tự hết hạn, nên thiếu
  bước này thì một chuỗi `initData` chụp được hôm nay dùng được mãi mãi.
- **Phát lại**: cùng một chuỗi hợp lệ gửi hai lần. Chống bằng cách nhớ `hash` đã dùng
  trong Redis suốt thời gian còn hiệu lực.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from televip.core.clock import now_utc
from televip.core.errors import InitDataError, InitDataExpired, InitDataReplay
from televip.core.logging import get_logger

log = get_logger(__name__)

_WEBAPP_SECRET_SALT = b"WebAppData"


@dataclass(frozen=True, slots=True)
class InitData:
    """Nội dung `initData` đã được xác thực."""

    user_id: int
    username: str | None
    full_name: str | None
    auth_date: int
    #: Chữ ký, dùng làm khoá chống phát lại.
    hash_hex: str
    raw: dict[str, str]

    @property
    def language_code(self) -> str | None:
        return self._user_field("language_code")

    @property
    def is_premium(self) -> bool:
        return bool(self._user_field("is_premium"))

    def _user_field(self, key: str) -> Any:
        try:
            return json.loads(self.raw.get("user", "{}")).get(key)
        except (json.JSONDecodeError, AttributeError):
            return None


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(_WEBAPP_SECRET_SALT, bot_token.encode(), hashlib.sha256).digest()


def _data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{k}={fields[k]}" for k in sorted(fields))


def verify_signature(init_data: str, bot_token: str) -> dict[str, str]:
    """Kiểm chữ ký. Trả về các trường đã decode, hoặc ném `InitDataError`.

    Chỉ kiểm chữ ký — không kiểm hạn dùng, không kiểm phát lại. Dùng `parse_init_data()`
    cho đường đi đầy đủ.
    """
    if not init_data:
        raise InitDataError("initData rỗng")

    # `parse_qsl` url-decode giá trị — đây chính là bước bot cũ làm thiếu.
    # `keep_blank_values` để một trường rỗng vẫn tham gia vào chuỗi ký, đúng như Telegram làm.
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))

    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData không có trường hash")

    expected = hmac.new(
        _secret_key(bot_token),
        _data_check_string(fields).encode(),
        hashlib.sha256,
    ).hexdigest()

    # `compare_digest` chứ không phải `==`: so sánh chuỗi thường thoát ra ngay ở byte đầu
    # khác nhau, và thời gian chênh lệch đó đủ để dò ra chữ ký đúng từng byte một.
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("chữ ký initData không hợp lệ")

    fields["hash"] = received_hash
    return fields


async def parse_init_data(
    init_data: str,
    bot_token: str,
    *,
    ttl_seconds: int = 300,
    check_replay: bool = True,
) -> InitData:
    """Đường đi đầy đủ: chữ ký → hạn dùng → phát lại.

    Ném:
        InitDataError: chữ ký sai hoặc thiếu trường bắt buộc.
        InitDataExpired: `auth_date` quá cũ.
        InitDataReplay: chuỗi này đã được dùng rồi.
    """
    fields = verify_signature(init_data, bot_token)

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("initData thiếu auth_date hoặc sai định dạng") from exc

    age = now_utc().timestamp() - auth_date
    if age > ttl_seconds:
        raise InitDataExpired(age, ttl_seconds)
    if age < -60:
        # Đồng hồ client chạy trước server hơn một phút. Cho phép lệch nhỏ, nhưng lệch
        # nhiều là dấu hiệu có người tự dựng auth_date trong tương lai để không bao giờ hết hạn.
        raise InitDataError(f"auth_date ở tương lai ({-age:.0f}s)")

    try:
        user_raw = json.loads(fields["user"])
        user_id = int(user_raw["id"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise InitDataError("initData không có thông tin người dùng hợp lệ") from exc

    if check_replay:
        from televip.cache.idempotency import recall, remember

        replay_key = f"initdata:{fields['hash']}"
        if await recall(replay_key) is not None:
            log.warning("initdata_phat_lai", user_id=user_id)
            raise InitDataReplay(f"initData đã được dùng (user {user_id})")
        # TTL bằng đúng hạn dùng: hết hạn rồi thì chuỗi tự vô hiệu, không cần nhớ nữa.
        await remember(replay_key, True, ttl=float(ttl_seconds))

    full_name = " ".join(
        p for p in (user_raw.get("first_name"), user_raw.get("last_name")) if p
    ).strip()

    return InitData(
        user_id=user_id,
        username=user_raw.get("username"),
        full_name=full_name or None,
        auth_date=auth_date,
        hash_hex=fields["hash"],
        raw=fields,
    )


def build_init_data(bot_token: str, user: dict[str, Any], auth_date: int) -> str:
    """Dựng một chuỗi `initData` hợp lệ — CHỈ dùng trong test.

    Có mặt ở đây thay vì trong file test để test và code sản xuất dùng chung đúng một
    định nghĩa về "hợp lệ" nghĩa là gì.
    """
    fields = {"auth_date": str(auth_date), "user": json.dumps(user, separators=(",", ":"))}
    signature = hmac.new(
        _secret_key(bot_token), _data_check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    from urllib.parse import urlencode

    return urlencode({**fields, "hash": signature})
