"""Đặt tên đăng nhập + mật khẩu cho một admin — chạy tay, không qua bot, không qua web.

    PYTHONPATH=src .venv/Scripts/python.exe scripts/set_admin_password.py --list
    PYTHONPATH=src .venv/Scripts/python.exe scripts/set_admin_password.py 6989373720 chubot

Mật khẩu **không nhận qua tham số dòng lệnh**: tham số nằm lại trong lịch sử shell và
trong danh sách tiến trình, nơi mọi người dùng khác trên máy đọc được. Script hỏi bằng
`getpass`, gõ không hiện lên màn hình.

## Vì sao script này tồn tại

Hai việc, và cả hai đều không làm bằng đường nào khác được:

1. **Tạo tài khoản đăng nhập đầu tiên.** Panel chưa có ai đăng nhập được thì không có
   cách nào tự đặt mật khẩu cho mình qua chính panel.
2. **Cửa thoát hiểm khi quên mật khẩu.** Không có luồng "quên mật khẩu" gửi email — hệ
   này không có email. Ai vào được SSH thì đặt lại được, và ai vào được SSH thì đằng nào
   cũng cầm `.env` với mật khẩu database, nên đây không phải một cửa hậu mới.

## Nó KHÔNG cấp quyền admin

`set_password()` từ chối `user_id` không phải admin đang hoạt động. Cấp quyền vẫn là
`/admin_add` trong bot, và `admin_users` vẫn là nguồn quyền duy nhất. Nếu script này tự
tạo được admin thì nó đã trở thành nguồn quyền thứ hai — đúng thứ cả thiết kế tránh.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import text

from televip.core.config import get_settings
from televip.db.engine import dispose_engine, init_engine, session, transaction
from televip.services import admin_auth

_SQL_LIST = """
SELECT a.user_id, a.role, a.login_name, u.username, u.full_name,
       (a.password_hash IS NOT NULL) AS co_mat_khau,
       a.password_changed_at
  FROM admin_users a
  JOIN users u ON u.user_id = a.user_id
 WHERE a.revoked_at IS NULL
 ORDER BY a.added_at
"""


async def _liet_ke() -> int:
    async with session() as db:
        rows = (await db.execute(text(_SQL_LIST))).all()
    if not rows:
        print("Chưa có admin nào đang hoạt động. Dùng /admin_add trong bot để cấp quyền trước.")
        return 1
    print(f"{len(rows)} admin đang hoạt động:\n")
    for r in rows:
        ten = r.login_name or "—"
        dau = "✅" if r.co_mat_khau else "⬜"
        tg = r.username and f"@{r.username}" or (r.full_name or "")
        print(f"  {dau} {r.user_id:<14} {r.role:<7} đăng nhập: {ten:<16} {tg}")
    print("\n✅ = đã đặt mật khẩu, vào được panel · ⬜ = chưa")
    return 0


async def _dat(user_id: int, login_name: str) -> int:
    mk = getpass.getpass("Mật khẩu mới: ")
    lai = getpass.getpass("Gõ lại: ")
    if mk != lai:
        print("❌ Hai lần gõ không khớp.")
        return 1
    if len(mk) < admin_auth.MIN_PASSWORD_LEN:
        print(f"❌ Mật khẩu phải dài ít nhất {admin_auth.MIN_PASSWORD_LEN} ký tự.")
        return 1

    async with transaction() as db:
        ok = await admin_auth.set_password(db, user_id=user_id, login_name=login_name, password=mk)
        if ok:
            # Đổi mật khẩu phải giết mọi phiên đang mở: nếu ai đó đang dùng tài khoản này
            # thì lý do đổi mật khẩu nhiều khả năng CHÍNH LÀ để đuổi họ ra.
            so_phien = await admin_auth.revoke_all_sessions(db, user_id=user_id)

    if not ok:
        print(
            f"❌ {user_id} không phải admin đang hoạt động.\n"
            "   Cấp quyền bằng /admin_add trong bot trước — script này không cấp quyền."
        )
        return 1

    print(f"\n✅ Đã đặt mật khẩu cho {user_id}, tên đăng nhập: {login_name.strip().lower()}")
    if so_phien:
        print(f"   Đã đóng {so_phien} phiên đang mở của tài khoản này.")
    return 0


async def _chay(args: argparse.Namespace) -> int:
    init_engine(get_settings())
    try:
        if args.list:
            return await _liet_ke()
        if args.user_id is None or args.login_name is None:
            print("Thiếu tham số. Dùng --list để xem danh sách, hoặc:")
            print("  scripts/set_admin_password.py <user_id> <ten_dang_nhap>")
            return 1
        return await _dat(args.user_id, args.login_name)
    finally:
        await dispose_engine()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("user_id", type=int, nargs="?", help="user_id Telegram của admin")
    p.add_argument("login_name", nargs="?", help="tên đăng nhập cho panel web")
    p.add_argument("--list", action="store_true", help="liệt kê admin và ai đã có mật khẩu")
    if len(sys.argv) == 1:
        p.print_help()
        return 1
    return asyncio.run(_chay(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
