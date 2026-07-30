"""Một client Redis dùng chung cho cả tiến trình.

Mọi thứ trong `televip.cache` (chống spam, token bucket, idempotency) đều là **trạng thái
liên tiến trình**: hệ chạy `tv-web` + `tv-worker` + `tv-scheduler` cùng lúc, nên mọi con số
phải nằm ngoài RAM của từng tiến trình. Đây là lý do tầng này tồn tại thay vì vài cái dict.

`decode_responses=False` — làm việc với **bytes**, chốt một lần cho toàn tầng:

- `idempotency` lưu JSON đã `orjson.dumps()` ra sẵn bytes; bật decode nghĩa là Redis giải mã
  UTF-8 thành `str` rồi `orjson.loads()` mã hoá ngược lại — hai lần chuyển đổi vô ích trên
  đường nóng, và làm hỏng bất kỳ giá trị nhị phân nào lỡ đi qua đây.
- Các giá trị còn lại (TTL, số token, kết quả script Lua) redis-py trả về `int` sẵn, cờ này
  không ảnh hưởng.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from redis.asyncio import BlockingConnectionPool, Redis

from televip.core.logging import get_logger

if TYPE_CHECKING:
    from televip.core.config import Settings

_log = get_logger(__name__)

_client: Redis | None = None


def init_redis(settings: Settings) -> Redis:
    """Gọi đúng một lần lúc khởi động tiến trình. Gọi lại trả về đúng client cũ."""
    global _client

    if _client is not None:
        return _client

    # BlockingConnectionPool chứ không phải pool mặc định: pool mặc định **ném lỗi** khi
    # chạm trần kết nối. Lúc broadcast, 48 coroutine sender cùng xin token là chuyện bình
    # thường, và "hết kết nối" phải là *chờ vài mili-giây*, chứ không phải một exception
    # ném lên giữa đường gửi làm rơi người nhận. Cùng nguyên tắc với `db/engine.py`
    # (`max_overflow=0`): vượt pool thì xếp hàng, không mở kết nối ngoài tầm kiểm soát.
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        decode_responses=False,
        max_connections=64,
        timeout=5,  # chờ tối đa 5 giây để có kết nối rồi mới chịu báo lỗi
        # Chặn trên cho mọi lời gọi: một Redis treo không được phép kéo theo cả bot treo.
        socket_connect_timeout=3.0,
        socket_timeout=5.0,
        socket_keepalive=True,
        # Kết nối rỗi bị firewall/NAT cắt âm thầm là chuyện thường trên VPS; ping định kỳ
        # để lỗi lộ ra ở đây chứ không lộ ra giữa một lượt xin token.
        health_check_interval=30,
    )
    _client = Redis(connection_pool=pool)
    _log.info("redis.init", url=settings.redis_url)
    return _client


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("init_redis() chưa được gọi")
    return _client


async def close_redis() -> None:
    """Đóng pool lúc tắt tiến trình (và giữa các test)."""
    global _client
    if _client is not None:
        # `close_connection_pool=True` là bắt buộc vì pool do chính ta dựng: mặc định client
        # chỉ đóng kết nối của riêng nó và để pool sống tiếp, giữ nguyên socket đang mở.
        await _client.aclose(close_connection_pool=True)
    _client = None
