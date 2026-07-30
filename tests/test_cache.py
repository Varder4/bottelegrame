"""Test tầng Redis: chống spam, token bucket, idempotency.

Chạy trên **Redis thật** (container dev, database 15 để không đụng dữ liệu dev), không dùng
fakeredis: toàn bộ giá trị của tầng này nằm ở tính nguyên tử của `SET NX` và của script Lua.
Một bản giả chạy tuần tự trong tiến trình test sẽ xanh kể cả khi cài sai hoàn toàn.

Câu hỏi mỗi test trả lời:
- C1 — bấm hai lần trong thời gian chờ: lần hai bị chặn?
- C2 — chờ hết hạn: lại bấm được?
- C3 — 100 coroutine cùng xin token: có bao giờ cấp quá 30 token trong một giây không?
- C4 — xô bị rút xuống dưới reserve của bulk: bulk còn sàn riêng để sống không?
- C5 — một tiến trình ăn 429: mọi lane cùng ngừng?
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio

from televip.cache import antispam, idempotency, ratelimit
from televip.cache.client import close_redis, get_redis, init_redis
from televip.core.clock import now_utc
from televip.core.errors import RateLimited

TEST_REDIS_URL = os.environ.get("TELEVIP_TEST_REDIS_URL", "redis://127.0.0.1:6380/15")


@pytest_asyncio.fixture
async def redis():
    """Client mới, database sạch cho từng test.

    Cố ý KHÔNG dùng chung giữa các test: pytest-asyncio tạo event loop riêng cho mỗi test,
    còn connection pool thì gắn với loop đã tạo ra nó.

    Dùng một object giả thay `Settings` vì `init_redis` chỉ đọc `redis_url` — test không có
    lý do gì phải cầm bot token thật.
    """
    client = init_redis(SimpleNamespace(redis_url=TEST_REDIS_URL))  # type: ignore[arg-type]
    await client.flushdb()
    yield client
    await close_redis()


def _now_ms() -> int:
    return int(now_utc().timestamp() * 1000)


# ── C1, C2 — chống spam ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c1_lan_thu_hai_trong_thoi_gian_cho_bi_chan(redis):
    await antispam.check_cooldown(4001, "tan_thu", 5.0)

    with pytest.raises(RateLimited) as exc:
        await antispam.check_cooldown(4001, "tan_thu", 5.0)

    assert exc.value.action == "tan_thu"
    assert 0 < exc.value.retry_after_seconds <= 5.0


@pytest.mark.asyncio
async def test_c2_het_thoi_gian_cho_thi_qua(redis):
    await antispam.check_cooldown(4002, "checkin", 0.3)
    with pytest.raises(RateLimited):
        await antispam.check_cooldown(4002, "checkin", 0.3)

    await asyncio.sleep(0.4)
    await antispam.check_cooldown(4002, "checkin", 0.3)  # không được ném


@pytest.mark.asyncio
async def test_cooldown_rieng_theo_action_va_theo_user(redis):
    await antispam.check_cooldown(4003, "checkin", 5.0)

    # Hành động khác của cùng người, và cùng hành động của người khác: không liên quan.
    await antispam.check_cooldown(4003, "tan_thu", 5.0)
    await antispam.check_cooldown(4004, "checkin", 5.0)


# ── C3 — token bucket không bao giờ vượt trần ───────────────────────


@pytest.mark.asyncio
async def test_c3_100_coroutine_khong_bao_gio_duoc_qua_30_token_trong_mot_giay(redis):
    """Đây là con số Telegram phạt: quá 30 tin/giây cho cả bot là 429.

    Bắt đầu từ xô **rỗng** để phép đo có nghĩa: xô đầy sẵn 30 token thì giây đầu tiên hợp lệ
    có tới 60 lượt (30 tồn + 30 rót), đúng theo định nghĩa token bucket. Rút cạn trước rồi đo
    từ đúng thời điểm đó thì trần phải là 30 tròn.
    """
    start_ms = _now_ms()
    await redis.hset(ratelimit.KEY_GLOBAL, mapping={"tk": 0, "ts": start_ms, "g": 30})

    async def one() -> tuple[bool, float]:
        ok = await ratelimit.acquire("interactive", timeout=10)
        return ok, now_utc().timestamp()

    results = await asyncio.gather(*[one() for _ in range(100)])

    granted = [ts for ok, ts in results if ok]
    assert len(granted) == 100, "có lượt bị hết giờ — timeout quá ngắn hoặc bucket kẹt"

    start_s = start_ms / 1000.0
    in_first_second = [ts for ts in granted if ts <= start_s + 1.0]
    in_first_two = [ts for ts in granted if ts <= start_s + 2.0]

    assert len(in_first_second) <= ratelimit.G_MAX, (
        f"cấp {len(in_first_second)} token trong giây đầu, trần là {ratelimit.G_MAX}"
    )
    assert len(in_first_two) <= 2 * ratelimit.G_MAX

    # Và phải thật sự có phanh: 100 token ở 30/giây không thể xong dưới ~3,3 giây.
    assert max(granted) - start_s >= 3.0, "cấp quá nhanh — xô không giới hạn gì cả"


@pytest.mark.asyncio
async def test_bucket_rong_thi_cho_va_tra_ve_false_khi_het_gio(redis):
    await redis.hset(ratelimit.KEY_GLOBAL, mapping={"tk": 0, "ts": _now_ms(), "g": 30})

    assert await ratelimit.acquire("interactive", timeout=0) is False


@pytest.mark.asyncio
async def test_lane_khong_hop_le_bi_tu_choi_ngay(redis):
    with pytest.raises(ValueError, match="lane không hợp lệ"):
        await ratelimit.acquire("khong-co-lane")

    with pytest.raises(ValueError, match="tokens"):
        await ratelimit.acquire("interactive", tokens=999)


# ── C4 — sàn dành riêng cho bulk ────────────────────────────────────


@pytest.mark.asyncio
async def test_c4_bulk_van_duoc_gui_khi_xo_tut_duoi_reserve_nho_san(redis):
    """Xô còn 5 token, dưới `reserve = 12` của bulk. Vì bulk chưa phát đủ 8 tin trong giây
    này nên nó được bỏ qua reserve — nhưng vẫn phải trừ token của xô chung (8 tin đó lấy TỪ
    TRONG ngân sách 30, không cộng thêm ngoài 30)."""
    await redis.hset(
        ratelimit.KEY_GLOBAL,
        mapping={"tk": 5, "ts": _now_ms(), "g": 30, "bulk_cap": 28},
    )

    assert await ratelimit.acquire("bulk", timeout=0) is True
    assert float(await redis.hget(ratelimit.KEY_GLOBAL, "tk")) == pytest.approx(4.0, abs=0.2)


@pytest.mark.asyncio
async def test_c4b_bulk_nhuong_duong_khi_da_dung_het_san(redis):
    """Đã phát đủ 8 tin bulk trong giây này → `reserve = 12` có hiệu lực trở lại, và xô chỉ
    còn 5 token nên bulk phải nhường phần còn lại cho lane interactive."""
    epoch = _now_ms() // 1000
    for e in (epoch, epoch + 1):  # phòng trường hợp vắt qua ranh giới giây
        await redis.set(f"{ratelimit.KEY_BULK_SEC_PREFIX}{e}", ratelimit.BULK_FLOOR, px=3000)
    await redis.hset(
        ratelimit.KEY_GLOBAL,
        mapping={"tk": 5, "ts": _now_ms(), "g": 30, "bulk_cap": 28},
    )

    assert await ratelimit.acquire("bulk", timeout=0) is False
    # Cùng lúc đó interactive (reserve 0) vẫn đi được — đây là toàn bộ mục đích của reserve.
    assert await ratelimit.acquire("interactive", timeout=0) is True


# ── C5 — phạt 429 là toàn cục ───────────────────────────────────────


@pytest.mark.asyncio
async def test_c5_phat_429_chan_moi_lane_ke_ca_interactive(redis):
    new_g = await ratelimit.penalize(2.0)
    assert new_g == pytest.approx(18.0)  # 30 × 0,6

    assert await ratelimit.acquire("interactive", timeout=0) is False
    assert await ratelimit.acquire("bulk", timeout=0) is False

    await redis.delete(ratelimit.KEY_COOLDOWN)
    assert await ratelimit.acquire("interactive", timeout=0) is True


# ── Trần per-chat ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_chat_cho_burst_3_tin_roi_ep_ve_1_tin_moi_giay(redis):
    chat_id = 777001

    for _ in range(3):  # capacity 3 — đủ cho luồng /huongdan gửi 3 tin liên tiếp
        assert await ratelimit.per_chat_acquire(chat_id, timeout=0) is True

    assert await ratelimit.per_chat_acquire(chat_id, timeout=0) is False

    started = asyncio.get_running_loop().time()
    assert await ratelimit.per_chat_acquire(chat_id, timeout=3) is True
    assert asyncio.get_running_loop().time() - started >= 0.8

    # Chat khác có xô riêng, không bị ảnh hưởng.
    assert await ratelimit.per_chat_acquire(chat_id + 1, timeout=0) is True


@pytest.mark.asyncio
async def test_hoan_token_per_chat_cho_phep_gui_lai_ngay(redis):
    chat_id = 777002
    for _ in range(3):
        assert await ratelimit.per_chat_acquire(chat_id, timeout=0) is True
    assert await ratelimit.per_chat_acquire(chat_id, timeout=0) is False

    await ratelimit.refund_chat(chat_id)
    assert await ratelimit.per_chat_acquire(chat_id, timeout=0) is True


@pytest.mark.asyncio
async def test_xo_per_chat_co_ttl_con_xo_chung_thi_khong(redis):
    """`rl:global` giữ luôn `g` và `bulk_cap`; hết hạn là âm thầm reset ngân sách AIMD."""
    await ratelimit.per_chat_acquire(555001, timeout=0)
    await ratelimit.acquire("interactive", timeout=0)

    assert await redis.pttl(ratelimit.chat_key(555001)) > 0
    assert await redis.pttl(ratelimit.KEY_GLOBAL) == -1  # -1 = tồn tại, không có TTL


# ── Idempotency ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nho_ket_qua_va_lay_lai_dung_ket_qua_do(redis):
    await idempotency.remember("tanthu:9001", {"code": "ABC-123", "value_vnd": 10_000})

    assert await idempotency.recall("tanthu:9001") == {"code": "ABC-123", "value_vnd": 10_000}
    assert await idempotency.recall("tanthu:9002") is None


@pytest.mark.asyncio
async def test_ket_qua_da_nho_het_han_theo_ttl(redis):
    await idempotency.remember("event:1:9003", {"code": "X"}, ttl=0.2)
    assert await idempotency.recall("event:1:9003") == {"code": "X"}

    await asyncio.sleep(0.3)
    assert await idempotency.recall("event:1:9003") is None


@pytest.mark.asyncio
async def test_ttl_mac_dinh_la_24_gio(redis):
    await idempotency.remember("ref_tier:9004:1", ["a", 1])

    ttl_ms = await get_redis().pttl(idempotency.idem_key("ref_tier:9004:1"))
    assert 86_000_000 < ttl_ms <= 86_400_000
