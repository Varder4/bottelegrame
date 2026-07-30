"""Chống spam phía người dùng: mỗi cặp `(user_id, action)` có một thời gian chờ riêng.

**Vì sao Redis chứ không phải dict trong RAM.** Bot cũ giữ `user_last_action` trong một biến
Python (`main.py:82`). Ba hậu quả, cả ba đều đã xảy ra thật:

1. **Mọi lần restart là mất sạch cooldown.** `Restart=always` + `RestartSec=10` nghĩa là chỉ
   cần một exception là toàn bộ trạng thái chống spam về 0 — người đang bị chặn bấm lại được ngay.
2. **Chạy nhiều tiến trình thì mỗi tiến trình có một bản riêng**, nên cooldown 5 giây với 4
   tiến trình thực chất là 4 lượt bấm được chấp nhận trong 5 giây. Chống spam coi như không tồn tại.
3. **Dict đó không bao giờ được dọn** — nó phình theo số user cho tới hết bộ nhớ.

Khoá Redis tự hết hạn đúng bằng thời gian chờ nên không có gì phải dọn, và mọi tiến trình
nhìn thấy cùng một trạng thái.

Giá trị mặc định của từng hành động nằm trong bảng `settings` (13-dac-ta-bot-moi.md §13.6.6),
không hardcode ở đây — hàm này chỉ nhận số giây đã tra ra.
"""

from __future__ import annotations

from televip.cache.client import get_redis
from televip.core.errors import RateLimited

#: Tiền tố khoá chống spam. Ngắn vì nó xuất hiện nhiều triệu lần trong bộ nhớ Redis.
KEY_PREFIX = "cd:"


def cooldown_key(user_id: int, action: str) -> str:
    return f"{KEY_PREFIX}{user_id}:{action}"


async def check_cooldown(user_id: int, action: str, seconds: float) -> None:
    """Đặt cờ chờ cho `(user_id, action)`, hoặc ném `RateLimited` nếu còn đang chờ.

    Quyết định cho qua hay không nằm gọn trong **một lệnh** `SET NX PX`: kiểm tra và đặt khoá
    là cùng một thao tác nguyên tử. Tách thành `GET` rồi `SET` thì hai lượt bấm cách nhau
    một phần nghìn giây sẽ cùng đọc ra "chưa có khoá" và cùng được cho qua — đúng cái lỗ hổng
    làm 226 người ôm 544 code thừa ở hệ cũ.
    """
    if seconds <= 0:
        return

    redis = get_redis()
    key = cooldown_key(user_id, action)
    ttl_ms = max(1, int(seconds * 1000))

    if await redis.set(key, b"1", nx=True, px=ttl_ms):
        return

    # Đường từ chối mới cần biết còn phải chờ bao lâu để nói với người dùng — một lượt đọc
    # thêm ở đây không nằm trên đường thành công.
    remaining_ms = await redis.pttl(key)
    if remaining_ms <= 0:
        # Khoá vừa hết hạn ngay giữa hai lệnh. Thử đặt lại một lần thay vì báo "chờ 0 giây".
        if await redis.set(key, b"1", nx=True, px=ttl_ms):
            return
        remaining_ms = max(await redis.pttl(key), 0)

    raise RateLimited(action, remaining_ms / 1000.0)
