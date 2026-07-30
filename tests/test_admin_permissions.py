"""Phân quyền admin và nhật ký — chạy trên database thật, qua chính decorator.

Bốn mệnh đề của D14 được kiểm ở đây, tất cả đều là chỗ hệ cũ đã mở toang:

- người **không** có trong `admin_users` chạy lệnh → bị từ chối, và **im lặng**;
- `cskh` chạy lệnh của `owner` → bị từ chối;
- `revoked_at` khác NULL → mất quyền ngay;
- mọi lệnh đổi trạng thái sinh một dòng `audit_log`, kể cả lượt **bị từ chối**.

Cộng thêm mệnh đề quan trọng nhất, kiểm bằng cách đọc source: decorator kiểm quyền
**không đọc id nhóm cảnh báo**. Hệ cũ có đúng một dòng `if chat_id == <id nhóm>` và
147.990.000đ đã đi qua nó; một test đọc chữ là cách duy nhất chặn dòng đó quay lại.

⚠️ File này KHÔNG dùng fixture `db`/`seeded` của `conftest`. Decorator đọc ghi qua engine
**toàn cục** (`db.engine.session()` / `transaction()`), nên dựng dữ liệu bằng một engine
thứ hai sẽ biến thứ tự commit giữa hai bên thành một cuộc đua. Ở đây chỉ có **một** engine.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

import televip
from televip.core.errors import PermissionDenied
from televip.db.engine import session as db_session
from televip.services import admin
from tests.conftest import TEST_DATABASE_URL, _truncate_all

_SRC = Path(televip.__file__).parent
_MIGRATION_0003 = _SRC / "db" / "migrations" / "versions" / "0003_seed_admin.py"

#: Bốn file được phép quyết định "ai chạy được lệnh gì" — không file nào trong số đó được
#: nhắc tới id nhóm cảnh báo. Xem docstring module.
_GUARDED_SOURCES = (
    _SRC / "services" / "admin.py",
    _SRC / "apps" / "worker" / "handlers" / "admin" / "__init__.py",
)


def load_migration() -> Any:
    """Nạp `0003_seed_admin.py` theo đường dẫn — `versions/` không phải package."""
    spec = importlib.util.spec_from_file_location("_m0003", _MIGRATION_0003)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M0003 = load_migration()


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    """Bắt lại đúng những gì bot định gửi. Với người bị từ chối phải luôn rỗng."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return 1


def make_update(user_id: int | None) -> Any:
    chat = SimpleNamespace(id=user_id or 0, type="private")
    user = None if user_id is None else SimpleNamespace(id=user_id, username=f"u{user_id}")
    return SimpleNamespace(effective_chat=chat, effective_user=user, effective_message=None)


def make_context(sender: FakeSender, args: list[str] | None = None) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        args=args,
    )


# ── Fixture: một engine duy nhất, trỏ vào database test ─────────────


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine

    # Engine phải sinh và huỷ trong CÙNG event loop với test — xem docstring fixture
    # `engine` ở conftest.
    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=10)  # type: ignore[arg-type]
    )
    # `_truncate_all` xoá cả `admin_permissions`, nên bản seed của migration 0003 không
    # sống sót qua test. Mỗi test tự nạp lại từ CHÍNH bản đồ của migration đó.
    admin.invalidate_role()

    async with db_session() as s:
        await _truncate_all(s)
        await s.execute(
            text("INSERT INTO admin_permissions (role, command) VALUES (:role, :command)"),
            M0003.seed_rows(),
        )
        await s.commit()

    try:
        yield
    finally:
        await db_engine.dispose_engine()
        admin.invalidate_role()


# ── Helper ──────────────────────────────────────────────────────────


async def run_sql(sql: str, params: dict[str, Any] | None = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def rows(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    async with db_session() as s:
        return list((await s.execute(text(sql), params or {})).all())


async def make_user(user_id: int) -> int:
    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:uid, :un) ON CONFLICT DO NOTHING",
        {"uid": user_id, "un": f"u{user_id}"},
    )
    return user_id


async def make_admin(user_id: int, role: str, *, revoked: bool = False) -> int:
    """Ghi thẳng SQL, KHÔNG qua `grant_role`: test quyền không được phụ thuộc đường ghi."""
    await make_user(user_id)
    await run_sql(
        """
        INSERT INTO admin_users (user_id, role, added_by, revoked_at)
             VALUES (:uid, :role, 1, CASE WHEN :rev THEN now() ELSE NULL END)
        ON CONFLICT (user_id) DO UPDATE
                SET role = EXCLUDED.role, revoked_at = EXCLUDED.revoked_at
        """,
        {"uid": user_id, "role": role, "rev": revoked},
    )
    admin.invalidate_role(user_id)
    return user_id


async def audit_actions(actor_id: int) -> list[str]:
    result = await rows(
        "SELECT action FROM audit_log WHERE actor_id = :a ORDER BY log_id", {"a": actor_id}
    )
    return [r.action for r in result]


# ══════════════════════════════════════════════════════════════════════
# D14 — nguồn sự thật duy nhất, kiểm bằng cách đọc source
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", _GUARDED_SOURCES, ids=lambda p: p.name)
def test_khong_file_nao_kiem_quyen_doc_id_nhom_canh_bao(path):
    """Hệ cũ: `if chat_id == <id nhóm cảnh báo>: return True` — không kiểm người gửi.

    Ai lọt được vào nhóm là có `/add_giffcode` và `/broadcast`. Test này chặn dòng đó quay
    lại dưới mọi hình thức: chuỗi ấy không được xuất hiện trong source, kể cả trong comment
    — vì một comment nhắc tới nó là dấu hiệu ai đó đang cân nhắc dùng lại nó.
    """
    source = path.read_text(encoding="utf-8").lower()
    assert "admin_group_id" not in source, (
        f"{path.name} nhắc tới id nhóm cảnh báo. D14: nhóm admin KHÔNG cấp quyền gì."
    )


def test_decorator_khong_doc_bien_moi_truong():
    """Kèm theo: file phân quyền không được đọc cấu hình môi trường ở bất kỳ dạng nào."""
    source = (_SRC / "services" / "admin.py").read_text(encoding="utf-8")
    assert "get_settings" not in source
    assert "os.environ" not in source


# ══════════════════════════════════════════════════════════════════════
# Bản đồ quyền của migration 0003
# ══════════════════════════════════════════════════════════════════════


def _commands_of(role: str) -> set[str]:
    return {cmd for cmd, roles in M0003.COMMAND_ROLES.items() if role in roles}


def test_owner_co_tat_ca_lenh():
    assert _commands_of("owner") == set(M0003.COMMAND_ROLES)


def test_cskh_chi_lenh_doc_va_trao_tay_code():
    """§13.4.2/§13.4.3: CSKH đọc hồ sơ và trao code event chia sẻ, không đụng kho hay cấu hình."""
    assert _commands_of("cskh") == {
        "/stats",
        "/users",
        "/show_share_event",
        "/done_event",
        "/help_admin",
        "/huongdan",
        "/user",
    }


def test_ketoan_chi_lenh_doc_bao_cao():
    assert _commands_of("ketoan") == {
        "/stats",
        "/codes",
        "/tonkho",
        "/baocao",
        "/help_admin",
        "/huongdan",
    }


def test_khong_seed_admin_users():
    """Không một `user_id` nào được hard-code vào lịch sử schema.

    Id đó sẽ sống mãi trong repo, và ai đọc được repo là biết chính xác phải chiếm tài
    khoản nào. Người đầu tiên đi qua `scripts/add_admin.py`.
    """
    assert all(set(row) == {"role", "command"} for row in M0003.seed_rows()), (
        "0003 chỉ được nạp cặp (role, command)"
    )
    assert M0003._permissions_table().name == "admin_permissions"
    assert M0003.delete_seeded().table.name == "admin_permissions"


@pytest.mark.asyncio
async def test_downgrade_chi_xoa_dung_phan_da_seed(wired):
    """`downgrade()` phải là xoá theo cặp (role, command), không `DELETE` trần cả bảng."""
    await run_sql(
        "INSERT INTO admin_permissions (role, command) VALUES ('owner', '/lenh_them_tay')"
    )
    async with db_session() as s:
        await s.execute(M0003.delete_seeded())
        await s.commit()

    remaining = await rows("SELECT role, command FROM admin_permissions")
    assert [(r.role, r.command) for r in remaining] == [("owner", "/lenh_them_tay")], (
        "downgrade() cuốn theo cả quyền không do migration 0003 nạp"
    )


# ══════════════════════════════════════════════════════════════════════
# can_run / get_role
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_nguoi_ngoai_danh_ba_bi_tu_choi(wired):
    await make_user(9001)
    async with db_session() as s:
        assert await admin.get_role(s, 9001) is None
        assert await admin.can_run(s, 9001, "/stats") is False
        assert await admin.can_run(s, 9001, "/broadcast") is False


@pytest.mark.asyncio
async def test_cskh_khong_chay_duoc_lenh_cua_owner(wired):
    await make_admin(9002, "cskh")
    async with db_session() as s:
        assert await admin.get_role(s, 9002) == "cskh"
        assert await admin.can_run(s, 9002, "/done_event") is True, "CSKH phải trao tay được code"
        assert await admin.can_run(s, 9002, "/del_all_code") is False
        assert await admin.can_run(s, 9002, "/broadcast") is False
        assert await admin.can_run(s, 9002, "/setcauhinh") is False


@pytest.mark.asyncio
async def test_ketoan_doc_bao_cao_khong_nap_kho(wired):
    await make_admin(9003, "ketoan")
    async with db_session() as s:
        assert await admin.can_run(s, 9003, "/baocao") is True
        assert await admin.can_run(s, 9003, "/tonkho") is True
        assert await admin.can_run(s, 9003, "/add_giffcode") is False


@pytest.mark.asyncio
async def test_revoked_at_khac_null_la_mat_quyen(wired):
    await make_admin(9004, "owner", revoked=True)
    async with db_session() as s:
        assert await admin.get_role(s, 9004) is None
        assert await admin.can_run(s, 9004, "/stats") is False


@pytest.mark.asyncio
async def test_bang_quyen_rong_thi_tu_choi_tat_ca(wired):
    """Fail-closed: chưa seed `admin_permissions` thì owner cũng không chạy được gì."""
    await make_admin(9005, "owner")
    await run_sql("DELETE FROM admin_permissions")
    async with db_session() as s:
        assert await admin.get_role(s, 9005) == "owner"
        assert await admin.can_run(s, 9005, "/stats") is False


@pytest.mark.asyncio
async def test_thieu_dau_gach_cheo_van_tra_loi_dung(wired):
    await make_admin(9006, "admin")
    async with db_session() as s:
        assert await admin.can_run(s, 9006, "stats") is True
        assert await admin.can_run(s, 9006, "/stats") is True


@pytest.mark.asyncio
async def test_require_permission_nem_loi_dung_kieu(wired):
    await make_user(9007)
    async with db_session() as s:
        with pytest.raises(PermissionDenied) as exc:
            await admin.require_permission(s, 9007, "/broadcast")
    assert exc.value.user_id == 9007
    assert exc.value.command == "/broadcast"


# ══════════════════════════════════════════════════════════════════════
# Cache vai trò
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cache_giu_cau_tra_loi_giua_hai_luot_doc(wired):
    """Chứng minh cache thật sự chặn truy vấn: đổi database sau lượt đọc đầu không lộ ra."""
    await make_user(9010)
    async with db_session() as s:
        assert await admin.get_role(s, 9010) is None

    # Ghi thẳng SQL nên KHÔNG có ai xoá cache — đúng tình huống muốn đo.
    await run_sql(
        "INSERT INTO admin_users (user_id, role, added_by) VALUES (9010, 'owner', 1)",
    )
    async with db_session() as s:
        assert await admin.get_role(s, 9010) is None, "lượt đọc thứ hai phải lấy từ cache"

    admin.invalidate_role(9010)
    async with db_session() as s:
        assert await admin.get_role(s, 9010) == "owner"


@pytest.mark.asyncio
async def test_revoke_xoa_cache_cua_chinh_tien_trinh(wired):
    """Tiến trình vừa thu hồi phải thấy ngay, không chờ hết 30 giây."""
    await make_admin(9011, "owner")
    async with db_session() as s:
        assert await admin.get_role(s, 9011) == "owner"

    async with db_session() as s, s.begin():
        await admin.revoke(s, user_id=9011, revoked_by=1)

    async with db_session() as s:
        assert await admin.get_role(s, 9011) is None
        assert await admin.can_run(s, 9011, "/stats") is False


# ══════════════════════════════════════════════════════════════════════
# grant_role / revoke — đường ghi và sổ kiểm toán
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_grant_role_ghi_audit_log(wired):
    await make_user(9020)
    async with db_session() as s, s.begin():
        change = await admin.grant_role(s, user_id=9020, role="cskh", added_by=777)

    assert change.old_role is None
    assert change.new_role == "cskh"

    entry = (
        await rows("SELECT actor_type, actor_id, action, entity_id, before, after FROM audit_log")
    )[0]
    assert entry.actor_type == "admin"
    assert entry.actor_id == 777
    assert entry.action == "admin.grant_role"
    assert entry.entity_id == "9020"
    assert entry.before is None
    assert entry.after == {"role": "cskh", "active": True}


@pytest.mark.asyncio
async def test_doi_vai_tro_ghi_ca_truoc_va_sau(wired):
    await make_admin(9021, "cskh")
    async with db_session() as s, s.begin():
        change = await admin.grant_role(s, user_id=9021, role="owner", added_by=777)

    assert (change.old_role, change.new_role) == ("cskh", "owner")
    entry = (await rows("SELECT before, after FROM audit_log ORDER BY log_id DESC LIMIT 1"))[0]
    assert entry.before == {"role": "cskh", "active": True}
    assert entry.after == {"role": "owner", "active": True}


@pytest.mark.asyncio
async def test_revoke_dat_revoked_at_chu_khong_xoa_dong(wired):
    await make_admin(9022, "admin")
    async with db_session() as s, s.begin():
        change = await admin.revoke(s, user_id=9022, revoked_by=777)

    assert change.old_role == "admin"
    assert await scalar("SELECT count(*) FROM admin_users WHERE user_id = 9022") == 1, (
        "thu hồi quyền là ĐIỀN revoked_at, không DELETE — mất dòng là mất bằng chứng"
    )
    assert await scalar("SELECT revoked_at IS NOT NULL FROM admin_users WHERE user_id = 9022")
    assert "admin.revoke" in await audit_actions(777)


@pytest.mark.asyncio
async def test_revoke_nguoi_khong_phai_admin_khong_ghi_gi(wired):
    await make_user(9023)
    async with db_session() as s, s.begin():
        change = await admin.revoke(s, user_id=9023, revoked_by=777)

    assert change.unchanged is True
    assert await scalar("SELECT count(*) FROM audit_log") == 0


@pytest.mark.asyncio
async def test_cap_lai_cho_nguoi_da_bi_thu_hoi(wired):
    await make_admin(9024, "admin", revoked=True)
    async with db_session() as s, s.begin():
        change = await admin.grant_role(s, user_id=9024, role="admin", added_by=777)

    assert change.old_role is None, "người đang bị thu hồi không có vai trò 'trước đó'"
    async with db_session() as s:
        assert await admin.get_role(s, 9024) == "admin"
    assert await scalar("SELECT count(*) FROM admin_users WHERE user_id = 9024") == 1


@pytest.mark.asyncio
async def test_vai_tro_la_bi_tu_choi_o_tang_python(wired):
    await make_user(9025)
    with pytest.raises(ValueError, match="vai trò không hợp lệ"):
        async with db_session() as s, s.begin():
            await admin.grant_role(s, user_id=9025, role="superadmin", added_by=777)


# ══════════════════════════════════════════════════════════════════════
# Decorator @admin_command
# ══════════════════════════════════════════════════════════════════════


def make_handler() -> tuple[Any, list[int]]:
    """Handler đếm số lần được gọi thật — thứ duy nhất chứng minh quyền đã được thực thi."""
    calls: list[int] = []

    async def handler(update: Any, context: Any) -> None:
        calls.append(update.effective_user.id)

    return handler, calls


@pytest.mark.asyncio
async def test_nguoi_khong_co_quyen_khong_nhan_duoc_MOT_CHU_NAO(wired):
    """Trả lời "bạn không có quyền" là xác nhận lệnh đó tồn tại. Phải im lặng tuyệt đối."""
    await make_user(9030)
    handler, calls = make_handler()
    wrapped = admin.admin_command("/broadcast")(handler)

    sender = FakeSender()
    await wrapped(make_update(9030), make_context(sender, ["xin chao"]))

    assert calls == [], "handler chạy dù không có quyền"
    assert sender.messages == [], f"đã lộ sự tồn tại của lệnh: {sender.messages}"


@pytest.mark.asyncio
async def test_luot_bi_tu_choi_van_de_lai_dau_vet(wired):
    """Im lặng với người gõ, KHÔNG im lặng với sổ: hệ cũ im lặng cả hai phía."""
    await make_user(9031)
    handler, _ = make_handler()
    wrapped = admin.admin_command("/broadcast")(handler)

    await wrapped(make_update(9031), make_context(FakeSender(), ["a", "b"]))

    entry = (await rows("SELECT action, entity_id, after, actor_id FROM audit_log"))[0]
    assert entry.action == "/broadcast.denied"
    assert entry.actor_id == 9031
    assert entry.entity_id == "/broadcast"
    assert entry.after == {"args": ["a", "b"]}


@pytest.mark.asyncio
async def test_cskh_bi_chan_o_lenh_cua_owner_qua_decorator(wired):
    await make_admin(9032, "cskh")
    handler, calls = make_handler()
    sender = FakeSender()

    await admin.admin_command("/del_all_code")(handler)(make_update(9032), make_context(sender))
    assert calls == []
    assert sender.messages == []

    await admin.admin_command("/done_event")(handler)(make_update(9032), make_context(sender))
    assert calls == [9032], "CSKH phải chạy được /done_event"


@pytest.mark.asyncio
async def test_lenh_doi_trang_thai_sinh_dong_audit_log(wired):
    await make_admin(9033, "owner")
    handler, calls = make_handler()
    wrapped = admin.admin_command("/del_all_code")(handler)

    await wrapped(make_update(9033), make_context(FakeSender(), ["event", "10000"]))

    assert calls == [9033]
    entry = (await rows("SELECT action, actor_type, actor_id, after FROM audit_log"))[0]
    assert entry.action == "/del_all_code"
    assert entry.actor_type == "admin"
    assert entry.actor_id == 9033
    assert entry.after == {"args": ["event", "10000"]}


@pytest.mark.asyncio
async def test_lenh_chi_doc_khong_lam_ngap_so(wired):
    await make_admin(9034, "ketoan")
    handler, calls = make_handler()
    wrapped = admin.admin_command("/stats", mutates=False)(handler)

    await wrapped(make_update(9034), make_context(FakeSender()))

    assert calls == [9034]
    assert await scalar("SELECT count(*) FROM audit_log") == 0


@pytest.mark.asyncio
async def test_audit_duoc_ghi_ngay_ca_khi_handler_no(wired):
    """Ghi TRƯỚC khi chạy: handler nổ giữa chừng vẫn để lại dấu vết lượt gọi."""
    await make_admin(9035, "owner")

    async def handler(update: Any, context: Any) -> None:
        raise RuntimeError("nổ giữa chừng")

    wrapped = admin.admin_command("/ban")(handler)
    with pytest.raises(RuntimeError):
        await wrapped(make_update(9035), make_context(FakeSender(), ["9999"]))

    assert await audit_actions(9035) == ["/ban"]


@pytest.mark.asyncio
async def test_update_khong_co_nguoi_gui_thi_khong_lam_gi(wired):
    """Tin nhắn kênh không có `effective_user`. Không có user_id thì không có quyền."""
    handler, calls = make_handler()
    wrapped = admin.admin_command("/broadcast")(handler)

    await wrapped(make_update(None), make_context(FakeSender()))

    assert calls == []
    assert await scalar("SELECT count(*) FROM audit_log") == 0


@pytest.mark.asyncio
async def test_thu_hoi_quyen_chan_lenh_tu_luot_ke_tiep(wired):
    await make_admin(9036, "owner")
    handler, calls = make_handler()
    wrapped = admin.admin_command("/broadcast")(handler)

    await wrapped(make_update(9036), make_context(FakeSender()))
    assert calls == [9036]

    async with db_session() as s, s.begin():
        await admin.revoke(s, user_id=9036, revoked_by=1)

    await wrapped(make_update(9036), make_context(FakeSender()))
    assert calls == [9036], "người đã bị thu hồi vẫn chạy được lệnh"
    assert "/broadcast.denied" in await audit_actions(9036)


@pytest.mark.asyncio
async def test_nhieu_luot_song_song_khong_lam_hong_gi(wired):
    """Handler admin chạy song song (`concurrent_updates=True`) — sổ phải đủ số dòng."""
    await make_admin(9037, "owner")
    handler, calls = make_handler()
    wrapped = admin.admin_command("/ban")(handler)

    await asyncio.gather(
        *[wrapped(make_update(9037), make_context(FakeSender(), [str(i)])) for i in range(8)]
    )

    assert len(calls) == 8
    assert await scalar("SELECT count(*) FROM audit_log WHERE action = '/ban'") == 8


# ══════════════════════════════════════════════════════════════════════
# commands_for_role — nguồn dữ liệu của /help_admin
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_commands_for_role_doc_tu_bang(wired):
    async with db_session() as s:
        owner_cmds = await admin.commands_for_role(s, "owner")
        cskh_cmds = await admin.commands_for_role(s, "cskh")

    assert set(owner_cmds) == set(M0003.COMMAND_ROLES)
    assert set(cskh_cmds) == _commands_of("cskh")
    assert owner_cmds == sorted(owner_cmds), "/help_admin cần danh sách đã sắp xếp ổn định"
