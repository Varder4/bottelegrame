"""Token bucket dùng chung cho toàn bộ lời gọi Bot API — nguyên tử bằng Lua trên Redis.

Ba điều phải hiểu trước khi sửa file này:

1. **CHỈ CÓ MỘT XÔ cho cả bot**, tên `rl:global`. Trần 30 tin/giây của Telegram tính theo
   **bot token**, không theo tiến trình và không theo lane. Cài thành mỗi lane một xô
   (`rl:global:interactive`, `rl:global:bulk`…) là biến trần thật thành 60-90 tin/giây và
   Telegram trả 429 — đây là chỗ dễ cài sai nhất của cả thiết kế (02-broadcast.md §2.3.4).
2. **Lane chỉ là một tham số `reserve` truyền vào script**, không phải một khoá riêng. Lane
   chỉ được lấy token khi mức xô còn **cao hơn** `reserve` của nó, nên `interactive` (reserve 0)
   luôn thắng còn `bulk` (reserve 12) tự nhường khi user đang bấm nút.
3. **Sàn 8 tin/giây của `bulk` lấy TỪ TRONG ngân sách 30, không cộng thêm ngoài 30.** Hiểu sai
   chỗ này ra 22 + 8 = 38 tin/giây, vượt trần cứng và sinh 429 dây chuyền.

Xô phải nằm ngoài tiến trình vì hệ chạy nhiều tiến trình cùng token. Đây cũng là lý do
`AIORateLimiter` của python-telegram-bot bị bỏ: nó chỉ giới hạn trong một tiến trình.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Final

from televip.cache.client import get_redis
from televip.core.clock import now_utc

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.asyncio.client import Script

# ── Khoá và hằng số ─────────────────────────────────────────────────

#: Xô chung duy nhất của cả bot. KHÔNG bao giờ đặt TTL lên khoá này: nó giữ luôn `g` (biến
#: AIMD) và `bulk_cap`; hết hạn giữa lúc đang bị Telegram phạt là âm thầm reset ngân sách về 30.
KEY_GLOBAL: Final = "rl:global"
#: Tồn tại = đang bị phạt 429. Chặn MỌI lane, kể cả interactive.
KEY_COOLDOWN: Final = "rl:cooldown"
#: Cờ "bulk đang có việc" — lane khác đọc để biết phải chừa token cho sàn của bulk.
KEY_BULK_WANT: Final = "rl:bulk:want"
#: Đếm tin bulk đã phát trong giây hiện tại; hậu tố là `floor(now_ms / 1000)`.
KEY_BULK_SEC_PREFIX: Final = "tb:bulk:sec:"

#: Trần cứng của Telegram, đồng thời là capacity của xô và ceiling của AIMD.
G_MAX: Final = 30
#: Sàn bảo đảm cho lane bulk. Cũng là sàn của AIMD khi bị phạt 429.
BULK_FLOOR: Final = 8

#: Lane = một giá trị `reserve`. `janitor` có mặt vì script được thiết kế cho cả ba lane
#: (02-broadcast.md §2.3.3); khối dọn dẹp dùng nó mà không phải sửa file này.
LANE_RESERVE: Final[dict[str, int]] = {
    "interactive": 0,
    "bulk": 12,
    "janitor": 22,
}

#: Trần per-chat: Telegram cho ~1 tin/giây tới cùng một chat riêng, tha cho burst ngắn.
CHAT_RATE: Final = 1.0
CHAT_CAPACITY: Final = 3.0

# ── Script Lua ──────────────────────────────────────────────────────

_LUA_GLOBAL: Final = """
-- KEYS[1] rl:global (hash tk·ts·g·bulk_cap) · KEYS[2] rl:cooldown
-- KEYS[3] tb:bulk:sec:<epoch>              · KEYS[4] rl:bulk:want
-- ARGV: now_ms · reserve · is_bulk · cost · bulk_floor · g_max
-- TRẢ VỀ: 0 = được gửi ngay; n > 0 = ngủ n mili-giây rồi gọi lại.
local now     = tonumber(ARGV[1])
local reserve = tonumber(ARGV[2])
local is_bulk = tonumber(ARGV[3]) == 1
local cost    = tonumber(ARGV[4])
local floor_q = tonumber(ARGV[5])
local gmax    = tonumber(ARGV[6])

-- (a) Phạt 429 toàn cục: chặn TẤT CẢ, kể cả interactive. Nếu chỉ tiến trình gặp lỗi ngủ
--     thì các tiến trình còn lại vẫn bắn tiếp và Telegram phạt nặng hơn.
local cd = redis.call('PTTL', KEYS[2])
if cd > 0 then return cd end

-- (b) g và bulk_cap nằm trong chính hash này, không truyền qua ARGV, để mọi tiến trình
--     nhìn thấy cùng một ngân sách — kể cả tiến trình vừa khởi động lại.
local st   = redis.call('HMGET', KEYS[1], 'tk', 'ts', 'g', 'bulk_cap')
local g    = tonumber(st[3]) or gmax
if g > gmax then g = gmax end
if g < floor_q then g = floor_q end
local cap  = g
local bcap = tonumber(st[4]) or (gmax - 2)
if bcap < floor_q then bcap = floor_q end
local tk   = tonumber(st[1])
local ts   = tonumber(st[2])

-- (b') KHÔNG BAO GIỜ để đồng hồ của xô chạy lùi. now_ms do người gọi tính trước khi gửi
--      lệnh đi, nên nhiều lời gọi song song tới nơi KHÔNG theo thứ tự thời gian đã tính.
--      Nếu một lời gọi "đến muộn với mốc thời gian cũ" được phép ghi đè ts về quá khứ thì
--      lời gọi kế tiếp tính lại quãng thời gian ĐÃ được rót một lần nữa — đo được thật:
--      100 coroutine lấy ra 34 token trong giây đầu thay vì 30. Cũng chính là lá chắn cho
--      lệch đồng hồ giữa các tiến trình.
if ts ~= nil and now < ts then now = ts end

-- (c) Bulk tuyên bố nhu cầu; không có cờ này thì "sàn 8/s" chỉ là một cuộc đua may rủi.
if is_bulk then redis.call('SET', KEYS[4], '1', 'PX', 1500) end

-- (d) Trần động bulk_cap, đo bằng số tin bulk đã phát trong GIÂY này.
local nbulk = tonumber(redis.call('GET', KEYS[3])) or 0
local ms_next_sec = 1000 - (now % 1000)
if is_bulk and nbulk >= bcap then
  return ms_next_sec
end

-- (e) Rót lại token theo thời gian trôi.
if tk == nil or ts == nil then tk = cap; ts = now end
tk = math.min(cap, tk + (now - ts) * g / 1000.0)

-- (f) Reserve hiệu dụng, hai chiều đối xứng:
--     · bulk chưa đủ sàn → bỏ qua reserve (nhưng VẪN trừ token của xô chung)
--     · lane khác, khi bulk đang có việc và chưa đủ sàn → chừa đúng phần sàn còn thiếu
local eff  = reserve
local debt = floor_q - nbulk
if debt < 0 then debt = 0 end
if is_bulk then
  if nbulk < floor_q then eff = 0 end
elseif debt > 0 and redis.call('EXISTS', KEYS[4]) == 1 then
  if eff < debt then eff = debt end
end

-- (g) Quyết định.
if tk - cost >= eff then
  redis.call('HSET', KEYS[1], 'tk', tk - cost, 'ts', now)
  if is_bulk then
    redis.call('INCR', KEYS[3])
    redis.call('PEXPIRE', KEYS[3], 2000)
  end
  return 0
end
redis.call('HSET', KEYS[1], 'tk', tk, 'ts', now)
local wait = math.ceil((eff + cost - tk) * 1000.0 / g)
if is_bulk and wait > ms_next_sec then wait = ms_next_sec end
if wait < 1 then wait = 1 end
return wait
"""

#: Bản rút gọn cho các xô không dính AIMD (per-chat): chỉ bước (e) + (g), reserve = 0,
#: `rate` và `capacity` truyền qua ARGV vì chúng là hằng số của từng loại xô.
_LUA_SIMPLE: Final = """
-- KEYS[1] khoá xô (hash tk·ts) · ARGV: now_ms · rate/s · capacity · cost
local now  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cap  = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local st = redis.call('HMGET', KEYS[1], 'tk', 'ts')
local tk = tonumber(st[1])
local ts = tonumber(st[2])
-- Đồng hồ của xô không được chạy lùi — xem ghi chú (b') ở script rl:global.
if ts ~= nil and now < ts then now = ts end
if tk == nil or ts == nil then tk = cap; ts = now end
if tk > cap then tk = cap end   -- hoàn token dư (refund_chat) không được vượt capacity
tk = math.min(cap, tk + (now - ts) * rate / 1000.0)

-- Khác rl:global: xô per-chat BẮT BUỘC có TTL. Số chat là vô hạn, không dọn thì Redis
-- đầy dần bằng những cái xô của người dùng một đi không trở lại. TTL = thời gian rót đầy
-- lại từ rỗng, cộng biên — hết hạn lúc đó nghĩa là xô đã đầy, mất khoá không mất thông tin.
local ttl = math.ceil(cap * 1000.0 / rate) + 1000

if tk - cost >= 0 then
  redis.call('HSET', KEYS[1], 'tk', tk - cost, 'ts', now)
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 0
end
redis.call('HSET', KEYS[1], 'tk', tk, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
local wait = math.ceil((cost - tk) * 1000.0 / rate)
if wait < 1 then wait = 1 end
return wait
"""

_LUA_PENALIZE: Final = """
-- Giảm nhân của AIMD. Phải nguyên tử vì nhiều tiến trình có thể cùng ăn 429 một khoảnh khắc.
local g = tonumber(redis.call('HGET', KEYS[1], 'g')) or tonumber(ARGV[1])
g = math.max(tonumber(ARGV[2]), g * 0.6)
redis.call('HSET', KEYS[1], 'g', g)
return tostring(g)
"""

#: Script đã đăng ký, kèm client đã dùng để đăng ký. redis-py gửi EVALSHA và tự nạp lại khi
#: Redis báo NOSCRIPT; chỉ cần đảm bảo không dùng lại object của một client đã đóng.
_registered: dict[str, tuple[Redis, Script]] = {}


def _script(name: str, source: str) -> Script:
    redis = get_redis()
    cached = _registered.get(name)
    if cached is not None and cached[0] is redis:
        return cached[1]
    script = redis.register_script(source)
    _registered[name] = (redis, script)
    return script


def _now_ms() -> int:
    return int(now_utc().timestamp() * 1000)


# ── API ─────────────────────────────────────────────────────────────


async def acquire(lane: str, tokens: int = 1, timeout: float = 30) -> bool:  # noqa: ASYNC109
    """Xin `tokens` token của xô chung cho `lane`. Trả `True` khi được phép gửi.

    Trả `False` nghĩa là hết `timeout` mà vẫn chưa tới lượt — người gọi phải coi đó là
    "chưa gửi" và trả tin về hàng đợi, tuyệt đối không gửi liều.

    Chờ bằng đúng số mili-giây script trả về, không phải `sleep(1.0)` mù quáng: vòng lặp cũ
    ngủ cứng 1 giây sau mỗi lô 30 tin nên vừa chậm hơn hạn mức (12-23 tin/giây trung bình)
    vừa vẫn ăn 429 (100 tin/giây tức thời trong 0,3 giây đầu mỗi chu kỳ).
    """
    if lane not in LANE_RESERVE:
        raise ValueError(f"lane không hợp lệ: {lane!r} (có: {sorted(LANE_RESERVE)})")
    if not 1 <= tokens <= G_MAX:
        # Xin nhiều hơn capacity thì không bao giờ được cấp — chờ tới hết timeout rồi trả
        # False, một kiểu treo im lặng rất khó lần ra. Chặn ngay tại đây.
        raise ValueError(f"tokens phải trong khoảng 1..{G_MAX}, nhận {tokens}")

    script = _script("global", _LUA_GLOBAL)
    reserve = LANE_RESERVE[lane]
    is_bulk = 1 if lane == "bulk" else 0
    # Đồng hồ đơn điệu để đo hạn chờ: đồng hồ tường có thể bị NTP kéo lùi và biến timeout
    # 30 giây thành vô hạn.
    deadline = time.monotonic() + timeout

    while True:
        now = _now_ms()
        wait_ms = int(
            await script(
                keys=[
                    KEY_GLOBAL,
                    KEY_COOLDOWN,
                    f"{KEY_BULK_SEC_PREFIX}{now // 1000}",
                    KEY_BULK_WANT,
                ],
                args=[now, reserve, is_bulk, tokens, BULK_FLOOR, G_MAX],
            )
        )
        if wait_ms <= 0:
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(wait_ms / 1000.0, remaining))


async def per_chat_acquire(chat_id: int, tokens: int = 1, timeout: float = 30) -> bool:  # noqa: ASYNC109
    """Trần ~1 tin/giây cho riêng một chat (burst tối đa 3 tin).

    Xô chung 30/giây không bảo vệ được chỗ này: 30 tin gửi hết cho **một** người vẫn là vi
    phạm. Luồng `/start` gửi 2 tin liên tiếp và `/huongdan` gửi 3 tin, nên capacity 3 là mức
    vừa đủ để các luồng đó không tự chặn mình.
    """
    if tokens < 1:
        raise ValueError(f"tokens phải >= 1, nhận {tokens}")

    script = _script("simple", _LUA_SIMPLE)
    key = chat_key(chat_id)
    deadline = time.monotonic() + timeout

    while True:
        wait_ms = int(await script(keys=[key], args=[_now_ms(), CHAT_RATE, CHAT_CAPACITY, tokens]))
        if wait_ms <= 0:
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(wait_ms / 1000.0, remaining))


def chat_key(chat_id: int) -> str:
    return f"rl:chat:{chat_id}"


async def refund_chat(chat_id: int, tokens: int = 1) -> None:
    """Trả lại token per-chat khi cửa sau (`rl:global`) bắt chờ và tin chưa hề được gửi.

    Quên bước này là một lỗi im lặng rất khó tìm: mỗi lần bị xô chung hoãn 200 ms, tin đó vẫn
    ăn mất một token per-chat, nên luồng `/start` bị kẹt vô cớ đúng vào lúc broadcast đang
    chạy — tức đúng lúc người dùng để ý nhất. Hoàn dư không sao, script cắt phần thừa ở
    `math.min(cap, …)`.
    """
    await get_redis().hincrbyfloat(chat_key(chat_id), "tk", float(tokens))


async def penalize(retry_after: float) -> float:
    """Ghi hình phạt 429 và giảm nhân ngân sách. Trả về `G` mới.

    Hai việc, và cả hai đều phải **toàn cục**: khoá `rl:cooldown` chặn mọi lane trong
    `retry_after + 1` giây, còn `g` giảm còn `max(8, g × 0,6)`. Nếu chỉ tiến trình gặp 429
    tự ngủ thì các tiến trình còn lại vẫn bắn tiếp và Telegram nâng mức phạt.

    Chiều tăng trở lại (`g = min(30, g + 2)` sau mỗi 60 giây không có 429) do `tv-scheduler`
    giữ — không tiến trình nào ôm `G` trong RAM.
    """
    redis = get_redis()
    await redis.set(KEY_COOLDOWN, b"1", px=max(1, int((retry_after + 1.0) * 1000)))
    raw = await _script("penalize", _LUA_PENALIZE)(keys=[KEY_GLOBAL], args=[G_MAX, BULK_FLOOR])
    return float(raw)
