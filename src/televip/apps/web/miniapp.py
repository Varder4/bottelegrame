"""API của Mini App xác minh — `POST /api/challenge` và `POST /api/verify`.

**Đây là file đã từng làm thủng cả hệ thống cũ.** Ở `api/app.py:338-343` bản cũ đọc
`user_id` thẳng từ JSON body do client gửi lên, trong khi khối kiểm chữ ký `initData` bị
comment kèm ghi chú "TEMPORARY DISABLED FOR TESTING". Hệ quả: một lệnh `curl` với
`user_id` bất kỳ là đủ để đánh dấu người khác đã xác minh rồi rút code của họ.

Hai luật bất di bất dịch của file này:

1. **Danh tính CHỈ đến từ `initData` đã xác thực.** `VerifyIn` cố ý KHÔNG có trường
   `user_id`; body gửi kèm `user_id` sẽ bị bỏ qua hoàn toàn (`extra="ignore"`). Không có
   đường nào cho một giá trị do client khai lọt vào truy vấn ghi.
2. **Đáp án phép tính không bao giờ rời khỏi server.** Bot cũ sinh phép tính trong
   JavaScript nên đáp án nằm sẵn trong mã nguồn trang — lớp chống bot đó gây ma sát cho
   100% người thật và chặn được 0% bot. Ở đây server sinh, server giữ trong Redis, server
   chấm, và chấm xong là huỷ (dùng đúng một lần).
"""

from __future__ import annotations

import enum
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.apps.web.initdata import parse_init_data
from televip.cache.client import get_redis
from televip.core.config import InitDataMode, Settings
from televip.core.errors import InitDataError, InitDataExpired, InitDataReplay, RateLimited
from televip.core.logging import get_logger
from televip.db.engine import transaction
from televip.services import settings_service

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["miniapp"])

# ── Khoá Redis ──────────────────────────────────────────────────────

#: Đáp án phép tính, một khoá cho mỗi `challenge_id`. Tự hết hạn nên không cần dọn.
CHALLENGE_KEY_PREFIX: Final = "verify:challenge:"
#: Bộ đếm số lần thử `/api/verify` của một user trong cửa sổ hiện tại.
ATTEMPT_KEY_PREFIX: Final = "verify:attempts:"

#: Cửa sổ đếm số lần thử. Cố định 60 giây vì "N lần mỗi phút" là đơn vị người vận hành
#: nghĩ theo; chỉ có N mới chỉnh được qua bảng `settings`.
ATTEMPT_WINDOW_SECONDS: Final = 60

# ── Khoá cấu hình nghiệp vụ ─────────────────────────────────────────
# Nguyên tắc N2: không con số nghiệp vụ nào hardcode. Các default dưới đây chỉ là lưới
# an toàn cho môi trường chưa seed bảng `settings` — nguồn sự thật vẫn là database.

SETTING_CAPTCHA_TTL: Final = "verify.captcha_ttl_s"
DEFAULT_CAPTCHA_TTL_SECONDS: Final = 300

#: Khoá này CHƯA có trong `0002_seed_config`; xem báo cáo của khối 1. Tới khi có migration
#: seed nó thì giá trị dưới đây là giá trị đang chạy.
SETTING_MAX_ATTEMPTS: Final = "verify.max_attempts_per_min"
DEFAULT_MAX_ATTEMPTS_PER_MIN: Final = 5

#: Danh sách dải mạng của proxy/CDN được phép đặt `X-Forwarded-For`. Rỗng = không tin ai,
#: tức luôn dùng địa chỉ TCP thật. Đây là mặc định đúng cho dev và fail-closed cho production
#: khi vận hành quên khai báo: thà ghi nhầm IP của chính proxy còn hơn tin một header mà bất
#: kỳ ai cũng tự đặt được — tin header thô là cách vô hiệu hoá toàn bộ tầng chống gian lận.
SETTING_TRUSTED_PROXIES: Final = "web.trusted_proxies"

# ── Câu chữ trả về Mini App ─────────────────────────────────────────
# Không nằm ở `domain/texts.py` vì đó là câu chữ tin nhắn Telegram; đây là thông báo lỗi
# của một API JSON, người đọc là trang Mini App chứ không phải khung chat.

_MSG: Final[dict[str, str]] = {
    "invalid_signature": "Phiên xác minh không hợp lệ. Vui lòng mở lại từ bot.",
    "expired": "Phiên xác minh đã hết hạn. Vui lòng mở lại Mini App.",
    "replay": "Phiên xác minh này đã được dùng. Vui lòng mở lại Mini App.",
    "wrong_answer": "Kết quả phép tính chưa đúng. Vui lòng thử lại.",
    "challenge_expired": "Phép tính đã hết hạn. Vui lòng lấy phép tính mới.",
    "already_verified": "Tài khoản của bạn đã được xác minh trước đó.",
}


# ── Kiểu dữ liệu vào/ra ─────────────────────────────────────────────


class VerifyIn(BaseModel):
    """Body của `POST /api/verify`.

    `extra="ignore"` là CÓ CHỦ Ý: client cũ (hoặc kẻ tấn công) gửi kèm `user_id` thì
    trường đó rơi vào hư không. Dùng `extra="forbid"` sẽ trả 422 và biến một body thừa
    trường thành lỗi khó hiểu; điều quan trọng không phải là từ chối body, mà là **không
    tồn tại đường nào** để `user_id` của client được đọc.
    """

    model_config = ConfigDict(extra="ignore")

    init_data: str
    answer: int
    challenge_id: str


class ChallengeOut(BaseModel):
    challenge_id: str
    question: str


@dataclass(frozen=True, slots=True)
class Identity:
    """Danh tính rút từ `initData`. Không bao giờ dựng từ dữ liệu do client khai."""

    user_id: int
    username: str | None
    full_name: str | None


class _ChallengeResult(enum.Enum):
    OK = enum.auto()
    WRONG = enum.auto()
    EXPIRED = enum.auto()


# ── Endpoint ────────────────────────────────────────────────────────


@router.post("/challenge", response_model=ChallengeOut)
async def create_challenge() -> ChallengeOut:
    """Sinh một phép cộng hai số 1-9 và giữ đáp án ở server.

    Cố ý KHÔNG nhận `initData`: phép tính là bài kiểm tra "có phải người đang ngồi trước
    máy không", chưa phải bước xác định danh tính. Ràng buộc danh tính nằm trọn ở
    `/api/verify`, nơi nó thật sự có giá trị.
    """
    ttl = await _captcha_ttl_seconds()

    # `secrets` chứ không phải `random`: `random` là Mersenne Twister, quan sát đủ số hạng
    # là đoán được số kế tiếp — với một lớp chống robot thì đó là tự vô hiệu hoá chính mình.
    left = secrets.randbelow(9) + 1
    right = secrets.randbelow(9) + 1
    challenge_id = str(uuid.uuid4())

    await get_redis().set(
        _challenge_key(challenge_id),
        str(left + right).encode(),
        px=max(1, ttl * 1000),
    )

    # Trả về CHỈ câu hỏi. Đáp án không xuất hiện trong response dưới bất kỳ dạng nào.
    return ChallengeOut(challenge_id=challenge_id, question=f"{left} + {right} = ?")


@router.post("/verify")
async def verify(payload: VerifyIn, request: Request) -> JSONResponse:
    """Xác minh một người dùng. Danh tính lấy từ `initData`, không lấy từ body."""
    settings: Settings = request.app.state.settings

    # Bậc thang except đi từ hẹp tới rộng: `InitDataReplay` và `InitDataExpired` đều là
    # lớp con của `InitDataError`, đảo thứ tự là mất luôn hai mã lỗi riêng.
    try:
        identity, initdata_hash = await _resolve_identity(payload.init_data, settings)
    except InitDataReplay:
        return _fail(403, "replay")
    except InitDataExpired:
        return _fail(403, "expired")
    except InitDataError:
        return _fail(403, "invalid_signature")

    # Chặn vét cạn: mỗi challenge chỉ dùng được một lần, nhưng không có trần số lần thử thì
    # kẻ tấn công vẫn lấy challenge mới rồi đoán liên tục cho tới khi trúng 1/17.
    try:
        await _consume_attempt(identity.user_id, await _max_attempts_per_min())
    except RateLimited as exc:
        return _fail(
            429,
            "rate_limited",
            f"Bạn thử quá nhiều lần. Vui lòng chờ {exc.retry_after_seconds:.0f} giây.",
        )

    match await _consume_challenge(payload.challenge_id, payload.answer):
        case _ChallengeResult.EXPIRED:
            return _fail(400, "challenge_expired")
        case _ChallengeResult.WRONG:
            log.info("verify.sai_phep_tinh", user_id=identity.user_id)
            return _fail(400, "wrong_answer")
        case _:
            pass

    # ── Tiêu vé chống phát lại, ĐÚNG Ở ĐÂY ──────────────────────────
    # Phải nằm SAU khi chấm đáp án, không phải trước.
    #
    # `Telegram.WebApp.initData` là một chuỗi CỐ ĐỊNH suốt cả phiên Mini App — mọi lần
    # bấm "Kiểm tra" đều gửi lên đúng chuỗi đó. Nếu tiêu vé ngay lúc kiểm chữ ký thì
    # người gõ sai phép tính một lần sẽ bị chính cơ chế chống phát lại khoá ra ngoài
    # vĩnh viễn: lần thử thứ hai trả `replay`, và người dùng thật không bao giờ xác minh
    # được nữa. Chống phát lại là để chặn việc dùng lại một lượt xác minh THÀNH CÔNG,
    # nên nó phải được tiêu theo kết quả chứ không theo số lần gọi API.
    if not await _claim_initdata_once(initdata_hash, settings.initdata_ttl_seconds):
        return _fail(403, "replay")

    client_ip = _client_ip(request, await _trusted_proxies())

    # Một giao dịch cho cả ba thao tác ghi: không có trạng thái nửa vời kiểu "đã đánh dấu
    # verified nhưng không có dòng nhật ký nào giải thích vì sao".
    async with transaction() as db:
        await _ensure_user_row(db, identity)
        if client_ip is not None:
            await _record_ip_signal(db, identity.user_id, client_ip)
        just_verified = await _mark_verified(db, identity.user_id)
        if just_verified:
            await _log_verification_event(
                db,
                user_id=identity.user_id,
                ip=client_ip,
                # Tới được đây nghĩa là chữ ký đã hợp lệ — mọi nhánh sai đều đã trả 403.
                initdata_valid=True,
            )

    if not just_verified:
        # Bấm hai lần, mạng chậm, mở lại Mini App — đều rơi vào đây. Không phải lỗi của
        # người dùng nên không dùng mã HTTP lỗi; chỉ nói rằng không có gì thay đổi.
        return _fail(200, "already_verified")

    log.info("verify.thanh_cong", user_id=identity.user_id)
    return JSONResponse({"ok": True})


# ── Danh tính ───────────────────────────────────────────────────────


async def _resolve_identity(init_data: str, settings: Settings) -> tuple[Identity, str]:
    """Trả về (danh tính, hash của initData). Chữ ký sai là từ chối — **không có ngoại lệ nào**.

    ## Vì sao không còn chế độ "shadow" ở đây

    Bản trước của hàm này cho `shadow`/`off` đi tiếp bằng cách rút `user_id` từ chuỗi
    `initData` **chưa kiểm chữ ký**. Nghe thì có vẻ vô hại vì "vẫn lấy từ initData chứ
    không lấy từ body", nhưng chuỗi `initData` chưa xác thực **chính là dữ liệu do client
    gửi lên** — kẻ tấn công gõ tay `user={"id":<nạn nhân>}&hash=0000...` là được đánh dấu
    đã xác minh. Đó đúng là lỗ hổng đã mở 7 tháng ở bot cũ, chỉ khác cái tên biến.

    Chế độ shadow có nghĩa ở những nơi chữ ký chỉ là **thông tin thêm**. Ở đây chữ ký
    **chính là** thứ tạo ra danh tính, nên "cho qua khi chữ ký sai" tương đương với
    "không có xác thực". Bot này bắt đầu từ 0 người dùng nên cũng không có tệp cũ nào để
    phải nới lỏng cho — không có lý do gì tồn tại một công tắc mở lại lỗ hổng.

    `settings.initdata_mode` giờ chỉ còn ảnh hưởng tới **mức log**, không ảnh hưởng tới
    quyết định cho qua hay không.
    """
    try:
        parsed = await parse_init_data(
            init_data,
            settings.bot_token,
            ttl_seconds=settings.initdata_ttl_seconds,
            # Vé chống phát lại được tiêu SAU khi chấm đáp án, không phải ở đây —
            # xem ghi chú trong `verify()`.
            check_replay=False,
        )
    except InitDataError:
        if settings.initdata_mode is not InitDataMode.ON:
            # Vẫn từ chối, nhưng ghi lại để đo tỉ lệ hỏng chữ ký trên lưu lượng thật.
            log.warning("verify.initdata_hong", mode=str(settings.initdata_mode))
        raise

    return Identity(parsed.user_id, parsed.username, parsed.full_name), parsed.hash_hex


async def _claim_initdata_once(hash_hex: str, ttl_seconds: int) -> bool:
    """Giành quyền dùng một chuỗi `initData`. Trả False nếu nó đã được dùng rồi.

    Nhận thẳng `hash` đã được `_resolve_identity()` kiểm chữ ký xong, thay vì kiểm lại —
    kiểm hai lần vừa thừa vừa buộc hàm này phải biết bot token, thứ nó không cần biết.

    Dùng `SET NX PX` — **một lệnh nguyên tử**. Cách viết "đọc rồi ghi" (`GET` thấy trống
    thì `SET`) có khe hở giữa hai lệnh: hai request mang cùng chuỗi gửi song song đều
    thấy trống và cùng lọt qua, tức là biện pháp chống phát lại không chặn được đúng thứ
    nó sinh ra để chặn.

    TTL bằng đúng hạn dùng của `initData`: hết hạn thì chuỗi tự vô hiệu, không cần nhớ nữa.
    """
    claimed = await get_redis().set(
        f"initdata:{hash_hex}", b"1", nx=True, px=max(1, int(ttl_seconds * 1000))
    )
    return bool(claimed)


# ── Phép tính chống robot ───────────────────────────────────────────


def _challenge_key(challenge_id: str) -> str:
    return f"{CHALLENGE_KEY_PREFIX}{challenge_id}"


async def _consume_challenge(challenge_id: str, answer: int) -> _ChallengeResult:
    """Chấm đáp án và **huỷ** challenge, dù đúng hay sai.

    `GETDEL` là một lệnh: đọc và xoá nguyên tử. Tách thành `GET` rồi `DEL` thì hai request
    song song cùng đọc ra đáp án và cùng được chấm — mỗi challenge lại thành nhiều lượt đoán.
    Huỷ cả khi sai là có chủ ý: một challenge = một lần đoán, nếu không thì 17 lần thử là
    chắc chắn trúng.
    """
    raw = await get_redis().getdel(_challenge_key(challenge_id))
    if raw is None:
        return _ChallengeResult.EXPIRED
    # Client Redis của dự án chạy `decode_responses=False`, nên đây là bytes: `int(b"12")`
    # ném TypeError chứ không phải trả về 12.
    return _ChallengeResult.OK if int(raw.decode()) == answer else _ChallengeResult.WRONG


async def _consume_attempt(user_id: int, limit: int) -> None:
    """Đếm một lượt thử; ném `RateLimited` khi vượt `limit` lần trong cửa sổ.

    `EXPIRE ... NX` đặt hạn đúng một lần cho cửa sổ đầu tiên. Đặt hạn ở mọi lượt sẽ đẩy
    mốc hết hạn về sau liên tục, và một người bấm đều tay sẽ giữ bộ đếm sống mãi mãi.
    """
    if limit <= 0:
        return

    redis = get_redis()
    key = f"{ATTEMPT_KEY_PREFIX}{user_id}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, ATTEMPT_WINDOW_SECONDS, nx=True)
        count = int((await pipe.execute())[0])

    if count > limit:
        remaining = await redis.ttl(key)
        raise RateLimited("verify", float(max(remaining, 0)))


# ── Địa chỉ IP ──────────────────────────────────────────────────────


def _client_ip(request: Request, trusted_proxies: list[str]) -> str | None:
    """IP thật của người dùng.

    Chỉ đọc `X-Forwarded-For` khi kết nối TCP đến TỪ một proxy đã khai báo. Header này ai
    cũng đặt được, nên tin nó vô điều kiện nghĩa là mọi bản ghi IP đều do kẻ gian tự chọn
    — và cả tầng chống gian lận phía sau chỉ còn đo dữ liệu giả.

    Duyệt danh sách từ PHẢI sang TRÁI và bỏ qua các proxy tin cậy: phần bên trái do client
    tự đặt được, còn phần bên phải do chính chuỗi proxy của mình ghi thêm.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return None

    networks = _parse_networks(trusted_proxies)
    if not networks or not _in_networks(peer, networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if _valid_ip(hop) and not _in_networks(hop, networks):
            return hop

    # Mọi chặng đều là proxy của mình (hoặc header rỗng/rác) → không có IP client nào đáng
    # tin hơn địa chỉ TCP.
    return peer


def _parse_networks(raw: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw:
        try:
            networks.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError:
            log.warning("web.dai_proxy_khong_hop_le", value=item)
    return networks


def _in_networks(ip: str, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


# ── Ghi database ────────────────────────────────────────────────────


async def _ensure_user_row(db: AsyncSession, identity: Identity) -> None:
    """Bảo đảm có hàng `users` để hai bảng nhật ký phía dưới có đích cho khoá ngoại.

    Cố ý KHÔNG đụng `started_bot_at`: cột đó nghĩa là "đã /start với bot", tức điều kiện
    VẬT LÝ để bot được phép nhắn tin. Mở Mini App qua link t.me không tạo ra quyền đó, và
    đặt nhầm cột này là đẩy người chưa /start vào danh sách broadcast rồi ăn 403 hàng loạt
    — đúng loại tín hiệu xấu nhất có thể gửi tới hệ chống spam của Telegram.
    """
    await db.execute(
        text("""
        INSERT INTO users (user_id, username, full_name, last_active)
             VALUES (:uid, :un, :fn, now())
        ON CONFLICT (user_id) DO UPDATE
                SET username    = EXCLUDED.username,
                    full_name   = EXCLUDED.full_name,
                    last_active = now()
        """),
        {"uid": identity.user_id, "un": identity.username, "fn": identity.full_name},
    )


async def _mark_verified(db: AsyncSession, user_id: int) -> bool:
    """Đánh dấu đã xác minh. Trả về True chỉ ở lần ĐẦU TIÊN.

    `WHERE verified_at IS NULL` là điều kiện làm câu lệnh idempotent: gọi lại không ghi đè
    mốc thời gian cũ (mốc đó là bằng chứng điều tra) và `RETURNING` rỗng là tín hiệu để
    tầng trên dừng mọi nhánh thưởng — nếu không, bấm hai lần là nhận thưởng hai lần.
    """
    row = await db.execute(
        text("""
        UPDATE users
           SET verified_at = now()
         WHERE user_id = :uid
           AND verified_at IS NULL
     RETURNING user_id
        """),
        {"uid": user_id},
    )
    return row.scalar_one_or_none() is not None


async def _record_ip_signal(db: AsyncSession, user_id: int, ip: str) -> None:
    """Cộng dồn tín hiệu IP. PK tổ hợp biến lần gặp lại thành UPSERT tăng `hits`.

    Hệ cũ ghi đè `users.ip_address` mỗi lượt verify nên mất sạch lịch sử; ở đây mỗi
    (user, loại, giá trị) là một hàng sống mãi cùng số lần bắt gặp.
    """
    await db.execute(
        text("""
        INSERT INTO identity_signals (user_id, signal_type, signal_value)
             VALUES (:uid, 'ip', :val)
        ON CONFLICT (user_id, signal_type, signal_value) DO UPDATE
                SET hits      = identity_signals.hits + 1,
                    last_seen = now()
        """),
        {"uid": user_id, "val": ip},
    )


async def _log_verification_event(
    db: AsyncSession,
    *,
    user_id: int,
    ip: str | None,
    initdata_valid: bool,
) -> None:
    """Một dòng nhật ký cho lượt xác minh vừa thành công.

    `asn`, `country`, `device_hash`, `ua_hash` để NULL và `risk_score` = 0: chấm điểm rủi
    ro và tra GeoIP thuộc khối khác. Cột `reasons` không truyền để `server_default`
    `'{}'::text[]` tự lo.

    Truyền thẳng đối tượng `ipaddress` xuống asyncpg — cột là `inet`/`cidr` thật, đưa
    chuỗi vào là lỗi kiểu ngay ở tầng driver.
    """
    addr = ipaddress.ip_address(ip) if ip else None
    network = (
        ipaddress.ip_network(f"{addr}/24", strict=False)
        if isinstance(addr, ipaddress.IPv4Address)
        else None
    )

    await db.execute(
        text("""
        INSERT INTO verification_events
                    (user_id, event_type, ip, ip_24, initdata_valid, verdict, risk_score)
             VALUES (:uid, 'verify', :ip, :ip24, :valid, 'pass', 0)
        """),
        {"uid": user_id, "ip": addr, "ip24": network, "valid": initdata_valid},
    )


# ── Cấu hình nghiệp vụ ──────────────────────────────────────────────


async def _captcha_ttl_seconds() -> int:
    return await settings_service.get_int(SETTING_CAPTCHA_TTL, DEFAULT_CAPTCHA_TTL_SECONDS)


async def _max_attempts_per_min() -> int:
    return await settings_service.get_int(SETTING_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS_PER_MIN)


async def _trusted_proxies() -> list[str]:
    raw: list[Any] = await settings_service.get_list(SETTING_TRUSTED_PROXIES, [])
    return [str(item) for item in raw]


# ── Trả lời ─────────────────────────────────────────────────────────


def _fail(status: int, code: str, message: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": code, "message": message or _MSG[code]},
        status_code=status,
    )
