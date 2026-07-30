"""Nạp dữ liệu tối thiểu để thử luồng nhận code trên máy dev.

CHỈ dùng cho môi trường dev. Chạy:
    PYTHONPATH=src .venv/Scripts/python.exe scripts/seed_dev.py

Nạp:
- `required_chats`: nhóm và kênh thật của bot (bot đã là administrator ở cả hai)
- `codes`: một ít mã tân thủ để có cái mà phát
- `settings.webapp.verify_url`: URL Mini App (truyền qua --verify-url)
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string

from sqlalchemy import text

from televip.core.config import get_settings
from televip.db.engine import dispose_engine, init_engine, transaction

# Nhóm và kênh thật, đã kiểm bằng getChatMember: bot là administrator ở cả hai.
REQUIRED_CHATS = [
    {
        "chat_id": -5179022394,
        "title": "NHÓMTESST",
        "invite_link": "https://t.me/+SGLpXCWz00gwNzY1",
        "sort_order": 1,
    },
    {
        "chat_id": -1003737488431,
        "title": "CHANEL TEST",
        "invite_link": "https://t.me/chaneltest6868",
        "sort_order": 2,
    },
]

_ALPHABET = string.ascii_uppercase + string.digits


def _make_code() -> str:
    """Mã dạng `TVXXXXXXXXXX` — cùng hình dạng với mã thật để test sát thực tế."""
    return "TV" + "".join(secrets.choice(_ALPHABET) for _ in range(10))


async def main(count: int, verify_url: str | None) -> None:
    settings = get_settings()
    init_engine(settings)

    try:
        async with transaction() as db:
            for chat in REQUIRED_CHATS:
                await db.execute(
                    text("""
                    INSERT INTO required_chats
                        (chat_id, title, invite_link, is_active, sort_order, health)
                         VALUES (:chat_id, :title, :invite_link, true, :sort_order, 'healthy')
                    ON CONFLICT (chat_id) DO UPDATE
                            SET title = EXCLUDED.title,
                                invite_link = EXCLUDED.invite_link,
                                is_active = true,
                                sort_order = EXCLUDED.sort_order
                    """),
                    chat,
                )
            print(f"  required_chats : {len(REQUIRED_CHATS)} nhóm/kênh")

            value_vnd = int(
                (
                    await db.execute(
                        text("SELECT value FROM settings WHERE key = 'code.tanthu_value_vnd'")
                    )
                ).scalar_one()
            )

            codes = [_make_code() for _ in range(count)]
            await db.execute(
                text("""
                INSERT INTO codes (code_value, code_type, value_vnd, status)
                     SELECT unnest(CAST(:codes AS text[])), 'tanthu', :val, 'available'
                ON CONFLICT (code_value) DO NOTHING
                """),
                {"codes": codes, "val": value_vnd},
            )
            print(f"  codes          : {count} mã tân thủ mệnh giá {value_vnd:,}đ")

            if verify_url:
                await db.execute(
                    text("""
                    INSERT INTO settings (key, value, value_type, label_vi)
                         VALUES ('webapp.verify_url', CAST(:url AS jsonb), 'string',
                                 'URL Mini App xác minh')
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """),
                    {"url": f'"{verify_url}"'},
                )
                print(f"  verify_url     : {verify_url}")

        async with transaction() as db:
            rows = (
                await db.execute(
                    text("""
                    SELECT code_type, value_vnd, count(*) AS n
                      FROM codes WHERE status = 'available'
                     GROUP BY code_type, value_vnd ORDER BY 1, 2
                    """)
                )
            ).all()
        print("\n  TỒN KHO HIỆN TẠI:")
        for r in rows:
            print(f"    {r.code_type:14s} {r.value_vnd:>9,}đ  ×  {r.n}")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20, help="số mã tân thủ cần nạp")
    parser.add_argument("--verify-url", default=None, help="URL công khai của Mini App")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.verify_url))
