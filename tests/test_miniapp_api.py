"""Test API Mini App.

Test quan trọng nhất của cả file là `test_body_khai_user_id_khac_thi_bi_bo_qua`: nó dựng
lại đúng cuộc tấn công đã ăn được trên bot cũ (`api/app.py:338-343`) — gửi `user_id` của
người khác trong JSON body. Nếu test đó chuyển sang xanh theo nghĩa "người trong body được
xác minh", lỗ hổng đã quay lại.
"""

from __future__ import annotations

import itertools
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.apps.web.app import create_app
from televip.apps.web.initdata import build_init_data
from televip.apps.web.miniapp import ATTEMPT_KEY_PREFIX, DEFAULT_MAX_ATTEMPTS_PER_MIN
from televip.cache.client import close_redis, init_redis
from televip.core.config import Settings
from televip.db.engine import dispose_engine, init_engine
from televip.services import settings_service
from televip.telegram import keyboards
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_user

BOT_TOKEN = "123456789:AAtest-token-chi-dung-trong-test-khong-that"

#: Chủ nhân của `initData` — người DUY NHẤT được phép xác minh trong mọi test dưới đây.
USER_A = {"id": 6989373720, "first_name": "Nguyễn", "last_name": "An", "username": "nguoi_that"}
#: Nạn nhân: kẻ tấn công khai id này trong body để mong bot đánh dấu hộ.
VICTIM_ID = 5555000111

#: Mỗi lượt gọi phải có một `initData` khác nhau, nếu không lớp chống phát lại (đúng như
#: thiết kế) sẽ từ chối lượt thứ hai. Lùi `auth_date` một giây mỗi lần là đủ khác mà vẫn
#: nằm trong hạn 300 giây.
_auth_date_offset = itertools.count()


def _fresh_init_data(user: dict | None = None) -> str:
    return build_init_data(BOT_TOKEN, user or USER_A, int(time.time()) - next(_auth_date_offset))


def _settings(initdata_mode: str = "on") -> Settings:
    return Settings(
        bot_token=BOT_TOKEN,
        admin_group_id=-1001,
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        env="dev",
        initdata_mode=initdata_mode,  # type: ignore[arg-type]
    )


@pytest_asyncio.fixture
async def infra(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Engine + Redis toàn cục cho tiến trình web, trỏ vào hạ tầng test.

    `httpx.ASGITransport` KHÔNG chạy lifespan của ứng dụng, nên hai thứ mà lifespan lo
    phải được dựng ở đây — nếu không mọi request sẽ ném "init_engine() chưa được gọi".
    Dựng và huỷ theo từng test vì connection pool gắn với event loop đã tạo ra nó.
    """
    settings = _settings()
    init_engine(settings)
    redis = init_redis(settings)
    await redis.flushdb()
    # Cache cấu hình sống 60 giây trong RAM tiến trình; test trước có thể đã nạp một bảng
    # `settings` khác hẳn vào đó.
    settings_service.invalidate()

    yield db

    await close_redis()
    await dispose_engine()


@asynccontextmanager
async def _client(initdata_mode: str = "on") -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings(initdata_mode))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mini.test") as client:
        yield client


@pytest_asyncio.fixture
async def api(infra: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async with _client() as client:
        yield client


# ── Tiện ích ────────────────────────────────────────────────────────


async def _solve_challenge(api: httpx.AsyncClient) -> tuple[str, int]:
    """Xin một phép tính và giải nó như một người thật."""
    response = await api.post("/api/challenge")
    assert response.status_code == 200
    body = response.json()
    left, right = body["question"].removesuffix("= ?").split("+")
    return body["challenge_id"], int(left) + int(right)


async def _verify(api: httpx.AsyncClient, **overrides) -> httpx.Response:
    challenge_id, answer = await _solve_challenge(api)
    payload = {
        "init_data": _fresh_init_data(),
        "answer": answer,
        "challenge_id": challenge_id,
    }
    payload.update(overrides)
    return await api.post("/api/verify", json=payload)


async def _verified_at(db: AsyncSession, user_id: int):
    return await db.scalar(
        text("SELECT verified_at FROM users WHERE user_id = :uid"), {"uid": user_id}
    )


async def _count_events(db: AsyncSession, user_id: int) -> int:
    return int(
        await db.scalar(
            text("SELECT count(*) FROM verification_events WHERE user_id = :uid"),
            {"uid": user_id},
        )
        or 0
    )


# ── Sức khoẻ ────────────────────────────────────────────────────────


async def test_health_bao_ok_khi_db_va_redis_song(api: httpx.AsyncClient):
    response = await api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}


# ── Phép tính chống robot ───────────────────────────────────────────


async def test_challenge_khong_bao_gio_gui_dap_an_xuong_client(api: httpx.AsyncClient):
    """Lỗi gốc của bot cũ: phép tính sinh trong JS nên đáp án nằm sẵn ở client.

    Response chỉ được chứa ĐÚNG hai thứ: một mã định danh không mang thông tin và một câu
    hỏi chỉ gồm hai số hạng. Thêm bất kỳ trường nào nữa là mở lại đường cũ.
    """
    body = (await api.post("/api/challenge")).json()

    assert set(body) == {"challenge_id", "question"}
    # `challenge_id` phải là UUID thuần — không phải đáp án mã hoá dưới dạng nào khác.
    uuid.UUID(body["challenge_id"])
    assert re.fullmatch(r"[1-9] \+ [1-9] = \?", body["question"]), body["question"]


async def test_challenge_chi_dung_duoc_mot_lan(api: httpx.AsyncClient, infra: AsyncSession):
    challenge_id, answer = await _solve_challenge(api)

    first = await api.post(
        "/api/verify",
        json={"init_data": _fresh_init_data(), "answer": answer, "challenge_id": challenge_id},
    )
    assert first.status_code == 200

    second = await api.post(
        "/api/verify",
        json={"init_data": _fresh_init_data(), "answer": answer, "challenge_id": challenge_id},
    )
    assert second.status_code == 400
    assert second.json()["error"] == "challenge_expired"


async def test_challenge_id_bia_ra_thi_bao_het_han(api: httpx.AsyncClient):
    response = await api.post(
        "/api/verify",
        json={
            "init_data": _fresh_init_data(),
            "answer": 7,
            "challenge_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "challenge_expired"


# ── Chữ ký ──────────────────────────────────────────────────────────


async def test_chu_ky_sai_thi_403_va_khong_ai_duoc_xac_minh(
    api: httpx.AsyncClient, infra: AsyncSession
):
    data = _fresh_init_data()
    tampered = data[:-1] + ("0" if data[-1] != "0" else "1")

    response = await _verify(api, init_data=tampered)

    assert response.status_code == 403
    assert response.json()["error"] == "invalid_signature"
    assert await _verified_at(infra, USER_A["id"]) is None


async def test_init_data_rong_thi_403(api: httpx.AsyncClient):
    response = await _verify(api, init_data="")
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_signature"


async def test_qua_han_thi_403_ma_expired(api: httpx.AsyncClient, infra: AsyncSession):
    old = build_init_data(BOT_TOKEN, USER_A, int(time.time()) - 4000)

    response = await _verify(api, init_data=old)

    assert response.status_code == 403
    assert response.json()["error"] == "expired"
    assert await _verified_at(infra, USER_A["id"]) is None


async def test_dung_lai_cung_init_data_thi_bao_replay(api: httpx.AsyncClient):
    data = _fresh_init_data()

    assert (await _verify(api, init_data=data)).status_code == 200

    replayed = await _verify(api, init_data=data)
    assert replayed.status_code == 403
    assert replayed.json()["error"] == "replay"


async def test_che_do_shadow_VAN_TU_CHOI_chu_ky_hong(infra: AsyncSession):
    """Không có chế độ nào cho phép chữ ký hỏng đi tiếp — kể cả `shadow`.

    Bản đầu của endpoint này cho `shadow` đi tiếp bằng cách rút `user_id` từ chuỗi
    `initData` **chưa kiểm chữ ký**. Nghe thì có vẻ vô hại vì "vẫn lấy từ initData",
    nhưng chuỗi chưa xác thực chính là dữ liệu client — gõ tay `user={"id":<nạn nhân>}`
    kèm hash bừa là đánh dấu được người khác đã xác minh. Đó đúng là lỗ hổng đã mở 7
    tháng ở bot cũ, chỉ đổi tên biến.

    Ở đây chữ ký **chính là** thứ tạo ra danh tính, nên "cho qua khi chữ ký sai" đồng
    nghĩa với "không có xác thực". `shadow` giờ chỉ đổi mức log, không đổi quyết định.
    """
    async with _client("shadow") as api:
        data = _fresh_init_data()
        tampered = data[:-1] + ("0" if data[-1] != "0" else "1")

        response = await _verify(api, init_data=tampered)

    assert response.status_code == 403
    assert response.json()["error"] == "invalid_signature"
    assert await _verified_at(infra, USER_A["id"]) is None, (
        "chữ ký hỏng mà vẫn đánh dấu verified — lỗ hổng của bot cũ đã quay lại"
    )
    assert (
        await infra.scalar(
            text("SELECT count(*) FROM verification_events WHERE user_id = :uid"),
            {"uid": USER_A["id"]},
        )
        == 0
    )


async def test_go_sai_phep_tinh_roi_go_dung_van_qua(api: httpx.AsyncClient, infra: AsyncSession):
    """Người thật gõ nhầm một lần thì không được bị khoá ra ngoài vĩnh viễn.

    `Telegram.WebApp.initData` là chuỗi CỐ ĐỊNH suốt phiên Mini App, nên mọi lần bấm
    "Kiểm tra" đều gửi lên đúng chuỗi đó. Bản đầu tiêu vé chống-phát-lại ngay ở bước
    kiểm chữ ký, nên lần thử thứ hai luôn ăn `replay` — gõ sai một lần là hết đường
    xác minh. Vé phải được tiêu theo KẾT QUẢ, không theo số lần gọi API.

    Bộ test cũ không bắt được lỗi này vì nó đúc `initData` mới cho từng lượt gọi,
    tức là không lượt nào đi đúng đường mà trình duyệt thật đi.
    """
    same_init_data = _fresh_init_data()

    challenge_id, answer = await _solve_challenge(api)
    wrong = await _verify(
        api, init_data=same_init_data, challenge_id=challenge_id, answer=answer + 1
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "wrong_answer"

    challenge_id, answer = await _solve_challenge(api)
    right = await _verify(api, init_data=same_init_data, challenge_id=challenge_id, answer=answer)
    assert right.status_code == 200, f"gõ đúng ở lần hai vẫn bị chặn: {right.json()}"
    assert await _verified_at(infra, USER_A["id"]) is not None


# ── Đáp án ──────────────────────────────────────────────────────────


async def test_dap_an_sai_thi_400_va_khong_danh_dau_verified(
    api: httpx.AsyncClient, infra: AsyncSession
):
    challenge_id, answer = await _solve_challenge(api)

    response = await api.post(
        "/api/verify",
        json={
            "init_data": _fresh_init_data(),
            "answer": answer + 1,
            "challenge_id": challenge_id,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "wrong_answer"
    assert await _verified_at(infra, USER_A["id"]) is None
    assert await _count_events(infra, USER_A["id"]) == 0


async def test_dap_an_dung_thi_verified_va_ghi_su_kien(api: httpx.AsyncClient, infra: AsyncSession):
    response = await _verify(api)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert await _verified_at(infra, USER_A["id"]) is not None
    assert await _count_events(infra, USER_A["id"]) == 1

    row = (
        await infra.execute(
            text("""
            SELECT event_type, verdict, initdata_valid, host(ip) AS ip, text(ip_24) AS ip24
              FROM verification_events WHERE user_id = :uid
            """),
            {"uid": USER_A["id"]},
        )
    ).one()
    assert (row.event_type, row.verdict, row.initdata_valid) == ("verify", "pass", True)
    assert row.ip == "127.0.0.1"
    assert row.ip24 == "127.0.0.0/24"


async def test_ghi_tin_hieu_ip_vao_identity_signals(api: httpx.AsyncClient, infra: AsyncSession):
    """Hệ cũ ghi đè `users.ip_address` mỗi lượt nên mất sạch lịch sử."""
    await _verify(api)
    await _verify(api)

    row = (
        await infra.execute(
            text("""
            SELECT signal_value, hits FROM identity_signals
             WHERE user_id = :uid AND signal_type = 'ip'
            """),
            {"uid": USER_A["id"]},
        )
    ).one()
    # Một hàng duy nhất, `hits` cộng dồn — không phải hai hàng, cũng không phải ghi đè.
    assert (row.signal_value, row.hits) == ("127.0.0.1", 2)


# ── Idempotent ──────────────────────────────────────────────────────


async def test_goi_lan_hai_tra_already_verified_va_khong_ghi_dong_thu_hai(
    api: httpx.AsyncClient, infra: AsyncSession
):
    assert (await _verify(api)).status_code == 200
    first_verified_at = await _verified_at(infra, USER_A["id"])

    second = await _verify(api)

    assert second.json()["error"] == "already_verified"
    assert await _count_events(infra, USER_A["id"]) == 1
    # Mốc thời gian cũ là bằng chứng điều tra, lượt gọi sau không được ghi đè nó.
    assert await _verified_at(infra, USER_A["id"]) == first_verified_at


# ── Lỗ hổng của bot cũ ──────────────────────────────────────────────


async def test_body_khai_user_id_khac_thi_bi_bo_qua(api: httpx.AsyncClient, infra: AsyncSession):
    """Cuộc tấn công đã ăn được trên bot cũ, dựng lại nguyên vẹn.

    Kẻ tấn công có `initData` hợp lệ của CHÍNH MÌNH và khai `user_id` của người khác trong
    body. Bot cũ đọc body nên đánh dấu hộ nạn nhân; bot mới phải bỏ qua hoàn toàn.
    """
    await make_user(infra, VICTIM_ID)

    response = await _verify(api, user_id=VICTIM_ID, telegram_id=VICTIM_ID)

    assert response.status_code == 200
    # Người trong `initData` được xác minh...
    assert await _verified_at(infra, USER_A["id"]) is not None
    # ...và nạn nhân thì không, kể cả một dòng nhật ký cũng không có.
    assert await _verified_at(infra, VICTIM_ID) is None
    assert await _count_events(infra, VICTIM_ID) == 0


# ── Chống vét cạn ───────────────────────────────────────────────────


async def test_qua_nhieu_lan_thu_thi_429(api: httpx.AsyncClient, infra: AsyncSession):
    """17 đáp án có thể có; không có trần số lần thử thì vét cạn là chắc chắn trúng."""
    for _ in range(DEFAULT_MAX_ATTEMPTS_PER_MIN):
        challenge_id, answer = await _solve_challenge(api)
        blocked = await api.post(
            "/api/verify",
            json={
                "init_data": _fresh_init_data(),
                "answer": answer + 1,
                "challenge_id": challenge_id,
            },
        )
        assert blocked.status_code == 400

    response = await _verify(api)

    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    assert await _verified_at(infra, USER_A["id"]) is None


async def test_bo_dem_so_lan_thu_co_han_dung(api: httpx.AsyncClient, infra: AsyncSession):
    """Bộ đếm phải tự hết hạn, nếu không một người bấm nhầm sẽ bị chặn vĩnh viễn."""
    from televip.cache.client import get_redis

    await _verify(api)

    ttl = await get_redis().ttl(f"{ATTEMPT_KEY_PREFIX}{USER_A['id']}")
    assert 0 < ttl <= 60


# ── Kiểu dữ liệu vào ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"answer": 5, "challenge_id": "x"},
        {"init_data": "abc", "challenge_id": "x"},
        {"init_data": "abc", "answer": "khong-phai-so", "challenge_id": "x"},
    ],
)
async def test_body_thieu_truong_thi_422(api: httpx.AsyncClient, body: dict):
    assert (await api.post("/api/verify", json=body)).status_code == 422


# ── Tin BƯỚC 2 tự nhảy sau khi xác minh ─────────────────────────────


async def _gieo_nhom(db: AsyncSession, *, so_nhom: int = 2) -> None:
    """Nhóm bắt buộc — không có nhóm nào thì không có gì để mời vào."""
    for i in range(so_nhom):
        await db.execute(
            text(
                "INSERT INTO required_chats (chat_id, title, invite_link, is_active, sort_order) "
                "VALUES (:c, :t, :l, true, :o) ON CONFLICT (chat_id) DO NOTHING"
            ),
            {"c": -100_000 - i, "t": f"Kênh {i}", "l": f"https://t.me/kenh{i}", "o": i},
        )
    await db.commit()


async def _tin_buoc_2(db: AsyncSession, user_id: int) -> list:
    return list(
        (
            await db.execute(
                text("SELECT chat_id, method, payload FROM outbox_messages WHERE dedupe_key = :k"),
                {"k": f"verify_step2:{user_id}"},
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_xac_minh_xong_thi_tin_BUOC_2_vao_hang_doi(
    api: httpx.AsyncClient, infra: AsyncSession
):
    """Người vừa xác minh không phải tự đi tìm nút — bot đẩy họ sang bước 2 ngay.

    Tiến trình web không gửi được gì, nên nó ghi ý định vào `outbox_messages`; worker bên
    bot phát. Bài này đo đúng cái web làm được: hàng ý định có mặt, đúng người, đúng nút.
    """
    await _gieo_nhom(infra)

    assert (await _verify(api)).status_code == 200

    rows = await _tin_buoc_2(infra, USER_A["id"])
    assert len(rows) == 1, "không có tin bước 2 nào được xếp"
    assert rows[0].chat_id == USER_A["id"]
    assert rows[0].method == "sendMessage"
    assert "BƯỚC 2" in rows[0].payload["text"]
    nut = rows[0].payload["reply_markup"]["inline_keyboard"][0][0]
    assert nut["callback_data"] == keyboards.CB_CHECK_GROUPS


@pytest.mark.asyncio
async def test_xac_minh_lan_hai_KHONG_xep_them_tin(api: httpx.AsyncClient, infra: AsyncSession):
    """Mở lại Mini App, bấm lại, mạng chậm — đều không được sinh tin thứ hai."""
    await _gieo_nhom(infra)
    assert (await _verify(api)).status_code == 200
    assert (await _verify(api)).status_code == 200

    assert len(await _tin_buoc_2(infra, USER_A["id"])) == 1


@pytest.mark.asyncio
async def test_chua_cau_hinh_nhom_thi_KHONG_xep_tin_rong(
    api: httpx.AsyncClient, infra: AsyncSession
):
    """Màn bước 2 không có nhóm nào là màn người dùng không làm gì được mà vẫn bị chặn."""
    await infra.execute(text("DELETE FROM required_chats"))
    await infra.commit()

    assert (await _verify(api)).status_code == 200
    assert await _tin_buoc_2(infra, USER_A["id"]) == []
    # …và việc xác minh vẫn phải thành công: thiếu cấu hình là lỗi của mình, không phải
    # lý do để chặn người dùng.
    assert await _verified_at(infra, USER_A["id"]) is not None
