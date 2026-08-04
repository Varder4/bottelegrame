"""Nạp kho code ĐỦ MỌI LOẠI để test tay toàn bộ luồng trên máy dev.

    PYTHONPATH=src .venv/Scripts/python.exe scripts/seed_test_stock.py

CHỈ chạy trên dev. Từ chối chạy nếu database không tên `televip` — nạp mã giả vào kho
thật là tạo ra nghĩa vụ tiền không có thật.

Khác `seed_dev.py` ở chỗ: file kia nạp đúng thứ cần cho luồng tân thủ, file này nạp cho
**mọi** loại × mệnh giá mà bảng cấu hình cho phép, để người test không bị chặn giữa chừng
bởi một kho rỗng và tưởng là lỗi code.

Mã sinh ra mang tiền tố `TEST-` để phân biệt với mã thật, và `/tonkho` vẫn đếm chúng như
mã bình thường — đó là chủ ý: kho test phải hành xử y hệt kho thật.
"""

from __future__ import annotations

import asyncio
import secrets
import string

from sqlalchemy import text

from televip.core.config import get_settings
from televip.db.engine import dispose_engine, init_engine, transaction

_ALPHABET = string.ascii_uppercase + string.digits

#: (loại kho, mệnh giá, số lượng). Mệnh giá phải khớp `settings.code.category_values`,
#: nếu không `/add_giffcode` từ chối và luồng phát cũng không tìm thấy mã.
KHO: list[tuple[str, int, int]] = [
    ("tanthu", 10_000, 30),
    ("moibanbe", 10_000, 20),
    ("diemdanh", 10_000, 20),
    ("diemdanh", 20_000, 10),
    ("diemdanh", 50_000, 10),
    ("eventchiase", 10_000, 20),
    # Đập hộp: đủ SÁU mệnh giá của bảng tỉ lệ. Thiếu một mức là `/send_event` từ chối
    # chạy (§13.5.1 điều kiện 1), và đó là hàng rào ta muốn thấy hoạt động chứ không
    # muốn vấp phải khi đang test việc khác.
    ("event", 5_000, 40),
    ("event", 10_000, 30),
    ("event", 20_000, 20),
    ("event", 50_000, 10),
    ("event", 88_000, 10),
]


def _ma() -> str:
    return "TEST-" + "".join(secrets.choice(_ALPHABET) for _ in range(10))


async def main() -> int:
    settings = get_settings()
    ten_db = settings.database_url.rsplit("/", 1)[-1].split("?")[0]
    if ten_db != "televip":
        print(f"❌ Từ chối: database là {ten_db!r}, chỉ nạp vào 'televip' (dev).")
        return 1

    init_engine(settings)
    try:
        async with transaction() as db:
            tong = 0
            for loai, gia, so_luong in KHO:
                await db.execute(
                    text("""
                    INSERT INTO codes (code_value, code_type, value_vnd, status)
                         VALUES (:cv, :ct, :gia, 'available')
                    ON CONFLICT (code_value) DO NOTHING
                    """).bindparams(),
                    [{"cv": _ma(), "ct": loai, "gia": gia} for _ in range(so_luong)],
                )
                tong += so_luong
                print(f"  {loai:12s} {gia:>7,}đ  ×  {so_luong}")

        async with transaction() as db:
            rows = (
                await db.execute(
                    text("""
                    SELECT code_type, value_vnd, count(*) n
                      FROM codes WHERE status = 'available'
                     GROUP BY 1, 2 ORDER BY 1, 2
                    """)
                )
            ).all()
        print(f"\n✅ Đã nạp {tong} mã. TỒN KHO KHẢ DỤNG hiện tại:")
        for r in rows:
            print(f"    {r.code_type:12s} {r.value_vnd:>7,}đ  ×  {r.n}")
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
