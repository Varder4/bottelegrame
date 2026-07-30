"""Thêm admin vào bảng `admin_users`.

Đây là **đường duy nhất** để có người admin đầu tiên. Migration `0003_seed_admin` cố ý
không seed `admin_users`: một `user_id` hard-code vào lịch sử schema sẽ sống mãi trong
repo, và ai đọc được repo là biết chính xác phải chiếm tài khoản nào. Sau người đầu tiên,
mọi thay đổi khác đi bằng lệnh `/admin_add` và `/admin_del`.

Chạy::

    PYTHONPATH=src .venv/Scripts/python.exe scripts/add_admin.py 6720704691 owner

Người được thêm phải đã từng `/start` với bot: `admin_users.user_id` có khoá ngoại sang
`users` (`ondelete=RESTRICT`), nên không có hàng ở `users` là không thêm được.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from televip.core.config import get_settings
from televip.db.engine import dispose_engine, init_engine, transaction
from televip.services import admin

#: `admin_users.added_by` và `audit_log.actor_id` cần một con số. Không có admin nào đứng
#: sau người ĐẦU TIÊN, nên 0 nghĩa là "do vận hành thêm bằng script" — 0 không phải id
#: Telegram hợp lệ, nên nó không thể bị nhầm với một admin thật khi đọc lại sổ.
BOOTSTRAP_ACTOR_ID = 0


def _say(message: str) -> None:
    print(message)  # noqa: T201 — script CLI, in ra màn hình là toàn bộ giao diện của nó


async def _run(user_id: int, role: str, added_by: int) -> int:
    init_engine(get_settings())
    try:
        async with transaction() as db:
            if not await admin.user_exists(db, user_id):
                _say(
                    f"❌ user {user_id} chưa có trong bảng `users`.\n"
                    "   Người này phải bấm /start với bot một lần trước đã — "
                    "`admin_users` có khoá ngoại sang `users`."
                )
                return 2

            change = await admin.grant_role(db, user_id=user_id, role=role, added_by=added_by)

        if change.old_role is None:
            _say(f"✅ Đã thêm user {user_id} với vai trò '{role}'.")
        else:
            _say(f"✅ Đã đổi vai trò user {user_id}: '{change.old_role}' → '{role}'.")
        return 0
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thêm admin vào bảng admin_users.")
    parser.add_argument("user_id", type=int, help="user_id Telegram của người cần thêm")
    parser.add_argument("role", choices=admin.ROLES, help="vai trò cần cấp")
    parser.add_argument(
        "--added-by",
        type=int,
        default=BOOTSTRAP_ACTOR_ID,
        help=f"user_id của người ra lệnh, ghi vào audit_log (mặc định {BOOTSTRAP_ACTOR_ID})",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.user_id, args.role, args.added_by))


if __name__ == "__main__":
    sys.exit(main())
