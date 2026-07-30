"""Nhớ kết quả của một thao tác để lần gọi lại trả về **đúng kết quả cũ**.

Đây là lớp thứ nhất của idempotency hai lớp. Lớp thứ hai — bảng `idempotency_records` trong
PostgreSQL — mới là nguồn sự thật; lớp Redis này chỉ cắt phần lớn lưu lượng lặp trước khi nó
chạm tới database. **Không được dùng một mình cho việc phát code:** Redis mất dữ liệu khi
restart mà không bật persistence, và mất khoá ở đây nghĩa là phát mã thứ hai.

Người dùng bấm hai lần thật nhanh là chuyện bình thường, không phải tấn công: mạng chậm, nút
chưa kịp đổi trạng thái. Hệ cũ trả lời hai lần bấm bằng hai mã khác nhau — 226 người ôm 544
code thừa. Ở đây cùng `key` luôn cho cùng câu trả lời trong 24 giờ.
"""

from __future__ import annotations

from typing import Any, Final

import orjson

from televip.cache.client import get_redis

KEY_PREFIX: Final = "idem:"

#: 24 giờ — đủ dài để bao trọn mọi lần bấm lại của cùng một phiên, đủ ngắn để không giữ mãi
#: kết quả của những thao tác mà bản thân nghiệp vụ đã cho phép làm lại vào hôm sau.
DEFAULT_TTL: Final = 86_400.0


def idem_key(key: str) -> str:
    return f"{KEY_PREFIX}{key}"


async def remember(key: str, value: Any, ttl: float = DEFAULT_TTL) -> None:
    """Ghi nhớ kết quả của `key`.

    Ghi đè có chủ đích chứ không dùng `NX`: người gọi chỉ lưu **sau khi** đã có kết quả thật,
    nên bản ghi sau luôn mới hơn hoặc bằng bản trước.
    """
    await get_redis().set(idem_key(key), orjson.dumps(value), px=max(1, int(ttl * 1000)))


async def recall(key: str) -> Any | None:
    """Đọc lại kết quả đã nhớ, hoặc `None` nếu chưa từng nhớ / đã hết hạn.

    Lưu ý khi dùng: giá trị `None` được lưu và trường hợp "không có gì" trả về giống hệt nhau.
    Đừng nhớ `None` — hãy nhớ một dict mô tả kết quả.
    """
    raw = await get_redis().get(idem_key(key))
    if raw is None:
        return None
    return orjson.loads(raw)
