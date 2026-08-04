"""Gọi thử TẤT CẢ lệnh admin trên một database nháp, trước khi ai đó test tay.

    PYTHONPATH=src .venv/Scripts/python.exe scripts/smoke_admin.py

Chạy trên `televip_load` (database nháp của bộ đo tải), **không đụng** `televip` (dev) —
nên chạy lúc nào cũng được, kể cả khi bot đang chạy và bạn sắp test tay.

## Nó bắt loại lỗi nào

Loại lỗi mà bộ kiểm thử KHÔNG bắt được: handler ném ngoại lệ trên đường thật, hoặc trả về
rỗng. Trong Telegram, cả hai hiện ra giống hệt nhau — **gõ lệnh xong không có gì xảy ra**
— và người gõ kết luận "bot hỏng". Bộ kiểm thử gọi hàm với dữ liệu dựng sẵn vừa khít; ở
đây ta gọi bằng đúng tham số mà một người thật sẽ gõ, gồm cả gõ thiếu và gõ sai.

Nó **không** thay bộ kiểm thử: nó không kiểm nội dung câu trả lời đúng hay sai, chỉ kiểm
"có trả lời và không nổ". Trả lời sai vẫn phải người đọc bắt.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import traceback
from types import SimpleNamespace
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BASE = os.environ.get(
    "TELEVIP_LOAD_BASE_DSN", "postgresql://televip:televip_dev_only@127.0.0.1:5433"
)
DB = "televip_load"
SQLA = f"postgresql+asyncpg://{BASE.split('://', 1)[1]}/{DB}"
REDIS_URL = os.environ.get("TELEVIP_LOAD_REDIS_URL", "redis://127.0.0.1:6380/14")

OWNER = 999_001
TARGET = 999_002


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.answers: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> int | None:
        self.messages.append(text)
        return len(self.messages)

    async def send_photo(self, chat_id: int, photo: Any, caption: Any = None, **kw: Any) -> int:
        self.messages.append(f"[ẢNH] {caption or ''}")
        return 1

    async def send_document(
        self, chat_id: int, document: bytes, *, filename: str, **kw: Any
    ) -> int:
        self.messages.append(f"[TỆP {filename}, {len(document)} byte]")
        return 1

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


def _update(user_id: int = OWNER, *, raw: str | None = None, anh: bool = False) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name="Chu bot")
    replied = None
    if anh:
        replied = SimpleNamespace(
            photo=[SimpleNamespace(file_id="THUMB"), SimpleNamespace(file_id="FILE_TEST")],
            caption="Bài chia sẻ test",
        )
    msg = SimpleNamespace(message_id=1, text=raw, chat=chat, reply_to_message=replied)
    return SimpleNamespace(
        effective_chat=chat, effective_user=user, effective_message=msg, callback_query=None
    )


def _context(sender: FakeSender, *args: str) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}, bot=SimpleNamespace()),
        bot=SimpleNamespace(username="testbot"),
        args=list(args),
    )


#: `(tên lệnh, [danh sách bộ tham số cần thử])`. Bộ rỗng = gõ trơ lệnh, phải ra hướng dẫn
#: chứ không được im lặng. Bộ sai = gõ nhầm, phải ra lời từ chối rõ ràng.
KICH_BAN: dict[str, list[tuple[str, ...]]] = {
    "add_giffcode": [
        (),
        ("tanthu",),
        ("saibet", "10k", "X1"),
        ("tanthu", "10k", "SMOKE-A", "SMOKE-B"),
    ],
    "del_code": [(), ("KHONGCO",), ("SMOKE-A",)],
    "del_all_code": [(), ("khongco",), ("eventchiase",)],
    "resend_tanthu": [(), ("@aokhongco",), (str(TARGET),)],
    "codes": [(), ("used",)],
    "tonkho": [()],
    "cauhinh": [(), ("event.",), ("khong.co.khoa.nay",)],
    "setcauhinh": [
        (),
        ("khong.co.khoa.nay", "1"),
        ("event.window_minutes", "sai"),
        ("event.window_minutes", "15"),
    ],
    "stats": [()],
    "users": [(), ("5",), ("abc",)],
    "user": [(), ("@aokhongco",), (str(TARGET),)],
    "ban": [(), (str(TARGET), "thử", "khoá")],
    "unban": [(), (str(TARGET),)],
    "admin_add": [(), ("abc", "owner"), (str(TARGET), "vaitrola"), (str(TARGET), "cskh")],
    "admin_del": [(), (str(TARGET),)],
    "help_admin": [()],
    "huongdan": [()],
    "noidung": [(), ("tanthu.",)],
    "xemnoidung": [(), ("khong.co",), ("tanthu.step1",)],
    "suanoidung": [(), ("tanthu.step1",)],
    "resetnoidung": [(), ("tanthu.step1",)],
    "broadcast": [(), ("Xin", "chào")],
    "broadcast_status": [(), ("999",)],
    "broadcast_pause": [(), ("999",)],
    "broadcast_resume": [(), ("999",)],
    "broadcast_cancel": [(), ("999",)],
    "send_event": [()],
    "update_share_event": [()],
    "show_share_event": [()],
    "done_event": [(), ("@aokhongco",), (str(TARGET),)],
    "chiendich": [(), ("start", "30", "Test"), ("extend", "7"), ("linhtinh",), ("end",)],
    "baocao": [(), ("tuan",), ("saibet",), ("ngay", "csv")],
    "checkip": [(), ("1.2.3.4",), ("@aokhongco",), (str(TARGET),)],
}

#: Lệnh cần reply một tấm ảnh mới chạy được đường chính.
CAN_ANH = {"update_share_event"}

#: Lệnh đọc phần thô của tin nhắn thay vì `context.args`.
CAN_RAW = {"broadcast", "send_event", "suanoidung"}


def _migrate() -> None:
    kq = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "TELEVIP_DATABASE_URL": SQLA},
        capture_output=True,
        text=True,
    )
    if kq.returncode != 0:
        raise SystemExit(f"alembic hỏng:\n{kq.stderr[-1500:]}")


async def _tao_db() -> None:
    conn = await asyncpg.connect(f"{BASE}/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB):
            await conn.execute(f'CREATE DATABASE "{DB}"')
    finally:
        await conn.close()


async def _dung_du_lieu(factory: Any) -> None:
    """Owner có MỌI quyền, một người dùng đích, kho code đủ loại, nhóm bắt buộc."""
    from televip.apps.worker import main as worker_main

    async with factory() as s:
        await s.execute(
            text("""
            INSERT INTO grant_types (code, label_vi, once_per_life) VALUES
                ('tanthu','a',true),('referral_milestone','b',false),('event_box','c',false),
                ('points_redeem','d',false),('share_event','e',true),('admin_manual','f',false)
            ON CONFLICT DO NOTHING
        """)
        )
        for uid, ten in ((OWNER, "chubot"), (TARGET, "nguoidich")):
            await s.execute(
                text(
                    "INSERT INTO users (user_id, username, verified_at) VALUES (:u,:n,now()) "
                    "ON CONFLICT (user_id) DO NOTHING"
                ),
                {"u": uid, "n": ten},
            )
        await s.execute(
            text(
                "INSERT INTO admin_users (user_id, role, added_by) VALUES (:u,'owner',:u) "
                "ON CONFLICT (user_id) DO UPDATE SET role='owner', revoked_at=NULL"
            ),
            {"u": OWNER},
        )
        for ten, _ in worker_main.admin_command_handlers():
            await s.execute(
                text(
                    "INSERT INTO admin_permissions (role, command) VALUES ('owner', :c) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"c": f"/{ten}"},
            )
        for loai, gia in (
            ("tanthu", 10_000),
            ("eventchiase", 10_000),
            ("diemdanh", 10_000),
            ("event", 5_000),
            ("event", 10_000),
            ("event", 20_000),
            ("event", 50_000),
            ("event", 88_000),
            ("moibanbe", 10_000),
        ):
            await s.execute(
                text("""
                INSERT INTO codes (code_value, code_type, value_vnd, status)
                -- Hai tham số cho cùng một con số: `:g_text` đi vào phép nối chuỗi, `:g_num`
                -- đi vào cột bigint. Dùng chung một tham số thì asyncpg không suy ra được
                -- kiểu và ném "invalid input for query argument".
                SELECT 'SMK-' || :l || '-' || :g_text || '-' || i,
                       :l, CAST(:g_num AS bigint), 'available'
                  FROM generate_series(1, 20) AS i
                ON CONFLICT DO NOTHING
                """),
                {"l": loai, "g_text": str(gia), "g_num": gia},
            )
        await s.execute(
            text("""
            INSERT INTO required_chats (chat_id, title, invite_link, sort_order, is_active)
                 VALUES (-100999, 'Nhom test', 'https://t.me/+abc', 1, true)
            ON CONFLICT DO NOTHING
            """)
        )
        await s.commit()


async def main() -> int:
    from types import SimpleNamespace as NS

    await _tao_db()
    _migrate()

    from televip.apps.worker import main as worker_main
    from televip.cache.client import close_redis, init_redis
    from televip.db import engine as db_engine

    db_engine.init_engine(NS(database_url=SQLA, db_pool_size=20))  # type: ignore[arg-type]
    init_redis(NS(redis_url=REDIS_URL))  # type: ignore[arg-type]

    engine = create_async_engine(SQLA, pool_size=20, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _dung_du_lieu(factory)

    handlers = dict(worker_main.admin_command_handlers())
    thieu_kich_ban = sorted(set(handlers) - set(KICH_BAN))
    thua_kich_ban = sorted(set(KICH_BAN) - set(handlers))

    hong: list[str] = []
    im_lang: list[str] = []
    tong_luot = 0

    print(f"Gọi thử {len(handlers)} lệnh admin trên {DB}\n")
    for ten in sorted(handlers):
        bo_tham_so = KICH_BAN.get(ten, [()])
        for args in bo_tham_so:
            tong_luot += 1
            sender = FakeSender()
            raw = f"/{ten} {' '.join(args)}".strip() if ten in CAN_RAW else None
            upd = _update(raw=raw, anh=ten in CAN_ANH)
            try:
                await handlers[ten](upd, _context(sender, *args))
            except Exception:
                hong.append(f"/{ten} {' '.join(args)}\n{traceback.format_exc(limit=4)}")
                print(f"  ❌ /{ten} {' '.join(args)}  — NỔ")
                continue
            if not sender.messages and not sender.answers:
                im_lang.append(f"/{ten} {' '.join(args)}")
                print(f"  ⚠️  /{ten} {' '.join(args)}  — KHÔNG TRẢ LỜI GÌ")
            else:
                print(f"  ✅ /{ten} {' '.join(args)}")

    await engine.dispose()
    await db_engine.dispose_engine()
    await close_redis()

    print(f"\n{'═' * 62}")
    print(f"{tong_luot} lượt gọi trên {len(handlers)} lệnh")
    if thieu_kich_ban:
        print(f"⚠️  lệnh chưa có kịch bản gọi thử: {thieu_kich_ban}")
    if thua_kich_ban:
        print(f"⚠️  kịch bản trỏ tới lệnh không tồn tại: {thua_kich_ban}")
    if im_lang:
        print(f"\n⚠️  {len(im_lang)} lượt KHÔNG trả lời gì (người gõ sẽ tưởng bot hỏng):")
        for d in im_lang:
            print(f"    {d}")
    if hong:
        print(f"\n❌ {len(hong)} lượt NỔ:\n")
        for d in hong:
            print(d)
        return 1
    if im_lang or thieu_kich_ban or thua_kich_ban:
        return 2
    print("\n✅ Mọi lệnh đều trả lời và không lệnh nào nổ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
