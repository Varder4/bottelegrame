"""Trần ngân sách event phải là trần CỨNG, kể cả khi cả nghìn người bấm cùng lúc.

Bộ soát ghi nhận `open_box()` đọc tổng đã phát rồi mới quyết định — hai việc tách rời
nhau. Dưới tải cao, N giao dịch cùng đọc một con số cũ, cùng kết luận "còn ngân sách",
rồi cùng phát: phần vượt trần là **N giải**, không phải một.

Đây là dạng lỗi không bao giờ hiện ra khi test tuần tự, và cũng không hiện ra trên máy
dev — nó chỉ hiện ra đúng lúc đông người nhất, tức là đúng lúc tốn tiền nhất.

Hai bài dưới đây chạy thật: nhiều session độc lập, mỗi session một giao dịch riêng, gọi
`open_box()` bằng `asyncio.gather`. Không có khoá thì bài thứ nhất trượt.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from televip.services import event_box, settings_service
from tests.conftest import add_codes, make_user

#: Một mệnh giá duy nhất, luôn trúng. Bỏ hết ngẫu nhiên ra khỏi bài kiểm — thứ đang được
#: đo là trần ngân sách, không phải bảng tỉ lệ.
PRIZE_VND = 10_000
CAP_VND = 30_000
CONCURRENCY = 12


async def _set(db, key: str, value, value_type: str) -> None:
    await db.execute(
        text("""
        INSERT INTO settings (key, value, value_type, label_vi)
             VALUES (:k, CAST(:v AS jsonb), :t, :k)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, max_value = NULL
        """),
        {"k": key, "v": json.dumps(value), "t": value_type},
    )


@pytest.fixture
async def event_san_sang(seeded, session_factory):
    """Một đợt đang mở, bảng tỉ lệ 100% trúng 10k, trần 30k, kho dư dả."""
    db = seeded
    await _set(db, event_box.BUDGET_CAP_KEY, CAP_VND, "money_vnd")
    await _set(db, event_box.WINDOW_MINUTES_KEY, 600, "int")
    await _set(
        db,
        event_box.PRIZE_TABLE_KEY,
        [{"value_vnd": PRIZE_VND, "weight_bp": 10_000}],
        "json",
    )
    for i in range(CONCURRENCY):
        await make_user(db, 7000 + i)
    await add_codes(db, code_type=event_box.CODE_TYPE, value_vnd=PRIZE_VND, count=100)

    event_id = await event_box.create_event(db, created_by=1, caption="test")
    await db.execute(
        text("UPDATE events SET is_active = true WHERE event_id = :e"), {"e": event_id}
    )
    await db.commit()
    settings_service.invalidate()
    yield event_id
    settings_service.invalidate()


async def _open(factory, *, event_id: int, user_id: int):
    async with factory() as s, s.begin():
        return await event_box.open_box(s, event_id=event_id, user_id=user_id)


@pytest.mark.asyncio
async def test_tran_ngan_sach_khong_bi_vuot_khi_dap_hop_dong_thoi(event_san_sang, session_factory):
    """12 người đập cùng lúc, trần 30k, giải 10k → tối đa 3 người trúng.

    Trước khi có khoá: cả 12 giao dịch đọc `spent = 0`, cả 12 trúng, đợt tiêu 120.000đ
    trên một trần 30.000đ — gấp bốn lần.
    """
    event_id = event_san_sang

    results = await asyncio.gather(
        *(_open(session_factory, event_id=event_id, user_id=7000 + i) for i in range(CONCURRENCY))
    )

    wins = [r for r in results if r.status == "win"]
    tong = sum(r.value_vnd or 0 for r in wins)

    # `>= cap mới dừng` là ngữ nghĩa cố ý: lượt chạm trần vẫn được phát, nên phần vượt
    # tối đa là ĐÚNG MỘT giải. Cái phải chết là kiểu vượt N giải.
    assert tong <= CAP_VND, f"đợt tiêu {tong:,}đ trên trần {CAP_VND:,}đ — trần không giữ"
    assert len(wins) == CAP_VND // PRIZE_VND, f"phải đúng 3 người trúng, đang là {len(wins)}"

    # Số còn lại phải bị từ chối ĐÚNG LÝ DO, không phải rơi vào nhánh khác.
    capped = [r for r in results if r.reason == "budget_cap"]
    assert len(capped) == CONCURRENCY - len(wins)


@pytest.mark.asyncio
async def test_so_sach_khop_voi_so_nguoi_trung(event_san_sang, session_factory):
    """Sổ cái phát hành phải nói đúng con số mà `open_box` trả về.

    Khoá tư vấn không được phép làm lệch việc ghi sổ: nếu một lượt bị chặn SAU khi đã
    giành mã thì kho hụt mà sổ không ghi.
    """
    event_id = event_san_sang

    results = await asyncio.gather(
        *(_open(session_factory, event_id=event_id, user_id=7000 + i) for i in range(CONCURRENCY))
    )
    wins = [r for r in results if r.status == "win"]

    async with session_factory() as s:
        spent = await event_box.spent_vnd(s, event_id=event_id)
        reserved = (
            await s.execute(text("SELECT count(*) FROM codes WHERE status = 'reserved'"))
        ).scalar_one()
        played = (
            await s.execute(
                text("SELECT count(*) FROM event_participations WHERE event_id = :e"),
                {"e": event_id},
            )
        ).scalar_one()

    assert spent == sum(r.value_vnd or 0 for r in wins)
    assert reserved == len(wins), "có mã bị giữ chỗ mà không ai được ghi là trúng"
    assert played == CONCURRENCY, "mỗi người phải được ghi đúng một lượt, kể cả lượt trượt"
