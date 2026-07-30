"""Câu chữ sửa được từ admin — service và bốn lệnh, chạy trên database thật.

Bốn mệnh đề trọng tâm:

- Sửa nội dung rồi `render` ra **đúng chuỗi mới** — nếu không thì cả khối này vô nghĩa.
- Thiếu biến bắt buộc thì **bị từ chối và nội dung KHÔNG đổi**. Đây là hàng rào đắt nhất:
  admin xoá `{code_value}` khỏi tin trả code thì người dùng nhận một tin không có mã, và
  không ai biết cho tới lúc có khiếu nại.
- Biến lạ bị từ chối, vì `str.format` sẽ ném `KeyError` giữa đường nóng.
- Khoá chưa có trong bảng thì rơi về `domain/texts.py` — bot không bao giờ câm.

⚠️ Cùng luật với `test_admin_ops.py`: KHÔNG dùng fixture `db` của conftest cho phần chạy
qua handler. Handler đọc ghi qua engine **toàn cục**, nên dựng dữ liệu bằng engine thứ hai
biến thứ tự commit giữa hai bên thành một cuộc đua.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from televip.apps.worker.handlers.admin import texts as admin_texts
from televip.db.engine import session as db_session
from televip.domain import texts as domain_texts
from televip.services import text_service
from televip.services.text_service import TemplateError
from tests.conftest import TEST_DATABASE_URL, _truncate_all

OWNER_ID = 910_001
CSKH_ID = 910_002
OUTSIDER_ID = 910_003


# ── Hạ tầng giả ─────────────────────────────────────────────────────


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.messages.append(text)
        return 1000 + len(self.messages)

    async def answer_callback(self, query: Any, text: str = "", show_alert: bool = False) -> None:
        return None

    @property
    def last(self) -> str:
        assert self.messages, "bot không gửi gì cả"
        return self.messages[-1]

    @property
    def all_text(self) -> str:
        return "\n".join(self.messages)


def make_update(user_id: int, raw_text: str | None = None) -> Any:
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U {user_id}")
    message = SimpleNamespace(message_id=1, text=raw_text, chat=chat)
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=user,
        effective_message=message,
        callback_query=None,
    )


def make_context(sender: FakeSender, *args: str) -> Any:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"sender": sender}),
        bot=SimpleNamespace(),
        args=list(args),
    )


@pytest_asyncio.fixture
async def wired():
    from televip.db import engine as db_engine
    from televip.services import admin as admin_service

    db_engine.init_engine(
        SimpleNamespace(database_url=TEST_DATABASE_URL, db_pool_size=15)  # type: ignore[arg-type]
    )
    text_service.invalidate()
    admin_service.invalidate_role()

    async with db_session() as s:
        await _truncate_all(s)
        await s.commit()

    try:
        yield
    finally:
        await db_engine.dispose_engine()
        text_service.invalidate()
        admin_service.invalidate_role()


async def run_sql(sql: str, params: Any = None) -> None:
    async with db_session() as s:
        await s.execute(text(sql), params or {})
        await s.commit()


async def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    async with db_session() as s:
        return (await s.execute(text(sql), params or {})).scalar_one()


async def stored_content(key: str) -> Any:
    async with db_session() as s:
        return (
            await s.execute(
                text("SELECT content FROM message_templates WHERE key = :k"), {"k": key}
            )
        ).scalar_one_or_none()


async def make_admin(user_id: int, role: str, commands: tuple[str, ...]) -> None:
    from televip.services import admin as admin_service

    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:uid, :un) ON CONFLICT DO NOTHING",
        {"uid": user_id, "un": f"user{user_id}"},
    )
    await run_sql(
        """
        INSERT INTO admin_users (user_id, role, added_by) VALUES (:uid, :role, :uid)
        ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, revoked_at = NULL
        """,
        {"uid": user_id, "role": role},
    )
    for command in commands:
        await run_sql(
            "INSERT INTO admin_permissions (role, command) VALUES (:role, :cmd) "
            "ON CONFLICT DO NOTHING",
            {"role": role, "cmd": command},
        )
    admin_service.invalidate_role(user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Đăng ký — không sót hàm nào của domain/texts.py
# ══════════════════════════════════════════════════════════════════════════════

#: Hàm trong `domain/texts.py` → khoá câu chữ. Bảng này là **cổng kiểm**: thêm một hàm câu
#: chữ mới mà quên khai khoá thì `test_moi_ham_cau_chu_deu_co_khoa` đỏ ngay.
FUNCTION_TO_KEY: dict[str, str] = {
    "start_welcome": "start.welcome",
    "start_gift_teaser": "start.gift_teaser",
    "not_verified": "verify.not_verified",
    "already_verified": "verify.already_verified",
    "verify_success": "verify.success",
    "verify_session_expired": "verify.session_expired",
    "verify_rejected": "verify.rejected",
    "tanthu_already_claimed": "tanthu.already_claimed",
    "tanthu_step1": "tanthu.step1",
    "tanthu_step2": "tanthu.step2",
    "not_verified_retry_tanthu": "tanthu.not_verified_retry",
    "missing_groups": "tanthu.missing_groups",
    "code_delivered": "code.delivered",
    "out_of_stock": "code.out_of_stock",
    "countdown": "referral.countdown_running",
    "campaign_block": "referral.campaign_block",
    "referral_invite": "referral.invite",
    "share_post_intro": "referral.share_post_intro",
    "share_post": "referral.share_post",
    "referral_milestone": "referral.milestone",
    "referral_milestone_out_of_stock": "referral.milestone_out_of_stock",
    "account_stats": "stats.account",
    "leaderboard": "leaderboard.screen",
    "checkin_success": "checkin.success",
    "checkin_already": "checkin.already",
    "redeem_menu": "redeem.menu",
    "redeem_success": "redeem.success",
    "redeem_out_of_stock": "redeem.out_of_stock",
    "redeem_not_enough": "redeem.not_enough",
    "event_box_caption": "event.box_caption",
    "event_box_win": "event.box_win",
    "event_box_empty": "event.box_empty",
    "event_box_budget_capped": "event.box_budget_capped",
    "share_event_default": "event.share_default",
    "support": "support.contact",
    "play_game": "game.play",
    "help_text": "help.text",
    "share_progress": "share.progress",
    "share_status_line": "share.status_waiting",
    "rate_limited": "error.rate_limited",
    "system_busy": "error.system_busy",
}

#: Hàm KHÔNG phải câu chữ mà là quy tắc dựng chuỗi — cố ý không cho admin sửa.
#: Xem mục cuối docstring của `services/text_service.py`.
NOT_EDITABLE: frozenset[str] = frozenset(
    {
        "format_vnd",
        "value_label",
        "display_name",
        "referral_link",
        "event_prize_lines",
        # Hai khối dòng của điểm danh / đổi điểm: sinh trong vòng lặp, đi vào template dưới
        # dạng biến `{tier_lines}` đã dựng sẵn.
        "checkin_tier_lines",
        "redeem_tier_lines",
        # Ba khối dòng của bảng xếp hạng, cùng lý do: huy chương và đơn vị sinh trong vòng
        # lặp rồi đi vào `leaderboard.screen` dưới dạng `{block_*}`. Phần văn xuôi bao
        # quanh chúng vẫn sửa được bình thường.
        "top_lines",
        "referrer_lines",
        "receiver_lines",
        "streak_lines",
        # Định dạng mốc thời gian: đây là quy ước hiển thị giờ, không phải câu chữ. Cho sửa
        # nghĩa là cho phép đổi cách hệ thống đọc một con số.
        "vn_time",
    }
)


def test_moi_ham_cau_chu_deu_co_khoa() -> None:
    """Không sót hàm nào: mỗi hàm công khai của `texts.py` hoặc có khoá, hoặc được miễn."""
    public = {
        name
        for name, obj in vars(domain_texts).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and obj.__module__ == domain_texts.__name__
    }
    thieu = public - set(FUNCTION_TO_KEY) - NOT_EDITABLE
    assert thieu == set(), f"hàm chưa có khoá câu chữ: {sorted(thieu)}"

    du = set(FUNCTION_TO_KEY) - public
    assert du == set(), f"khai khoá cho hàm không tồn tại: {sorted(du)}"


def test_moi_khoa_deu_ton_tai_trong_dang_ky() -> None:
    for name, key in FUNCTION_TO_KEY.items():
        assert key in text_service.TEMPLATES, f"{name} trỏ tới khoá không có: {key}"


def test_alert_cung_co_khoa() -> None:
    """9 hằng `ALERT_*` là câu chữ hiện lên màn hình người dùng, nên cũng phải sửa được."""
    alerts = [name for name in vars(domain_texts) if name.startswith("ALERT_")]
    assert len(alerts) == 9
    keys = {k for k in text_service.TEMPLATES if k.startswith("alert.")}
    assert len(keys) == len(alerts)


def test_seed_cua_migration_khop_dang_ky() -> None:
    """Dữ liệu migration nạp phải là **chính** bản mặc định mà `reset` sẽ khôi phục."""
    seed = {row["key"]: row for row in text_service.TEMPLATE_SEED}
    assert set(seed) == set(text_service.TEMPLATES)
    for key, spec in text_service.TEMPLATES.items():
        assert seed[key]["content"] == spec.content
        assert seed[key]["required_vars"] == list(spec.required_vars)
        assert seed[key]["label_vi"] == spec.label_vi


def test_khong_co_khoa_trung() -> None:
    keys = [row["key"] for row in text_service.TEMPLATE_SEED]
    assert len(keys) == len(set(keys))


# ══════════════════════════════════════════════════════════════════════════════
# Bản mặc định phải khớp domain/texts.py
# ══════════════════════════════════════════════════════════════════════════════

#: (khoá, biến truyền vào, chuỗi mà `domain/texts.py` sinh ra với **cùng** giá trị đó).
#: Đây là chỗ chốt lời hứa lớn nhất của module: gọi `render` bằng đúng bộ tham số của hàm
#: gốc thì ra đúng chuỗi của hàm gốc, không lệch một dấu chấm hàng nghìn nào.
SAME_AS_TEXTS: list[tuple[str, dict[str, Any], str]] = [
    ("start.welcome", {}, domain_texts.start_welcome()),
    ("verify.not_verified", {}, domain_texts.not_verified()),
    ("tanthu.step1", {}, domain_texts.tanthu_step1()),
    ("game.play", {}, domain_texts.play_game()),
    ("error.system_busy", {}, domain_texts.system_busy()),
    (
        "verify.rejected",
        {"support_link": "https://t.me/cskh"},
        domain_texts.verify_rejected("https://t.me/cskh"),
    ),
    (
        "tanthu.already_claimed",
        {"game_link": "https://game"},
        domain_texts.tanthu_already_claimed("https://game"),
    ),
    ("tanthu.missing_groups", {"joined": 1, "total": 3}, domain_texts.missing_groups(1, 3)),
    (
        "code.delivered",
        {"code_value": "TV-9", "value_vnd": 88_000},
        domain_texts.code_delivered("TV-9", 88_000),
    ),
    (
        "code.out_of_stock",
        {"support_link": "https://t.me/cskh"},
        domain_texts.out_of_stock("https://t.me/cskh"),
    ),
    (
        "event.box_win",
        {"code_value": "TV-9", "value_vnd": 50_000},
        domain_texts.event_box_win("TV-9", 50_000),
    ),
    (
        "event.box_empty",
        {"game_link": "https://game"},
        domain_texts.event_box_empty("https://game"),
    ),
    (
        "support.contact",
        {"support_link": "https://t.me/cskh"},
        domain_texts.support("https://t.me/cskh"),
    ),
    (
        "checkin.already",
        {"balance": 120_000, "streak": 9},
        domain_texts.checkin_already(balance=120_000, streak=9),
    ),
    (
        "redeem.success",
        {"code_value": "TV-9", "value_vnd": 20_000, "balance": 5_000},
        domain_texts.redeem_success(code_value="TV-9", value_vnd=20_000, balance=5_000),
    ),
    (
        "redeem.not_enough",
        {"balance": 5_000, "value_vnd": 20_000},
        domain_texts.redeem_not_enough(balance=5_000, value_vnd=20_000),
    ),
    (
        "redeem.out_of_stock",
        {"value_vnd": 20_000, "balance": 5_000, "support_link": "https://t.me/cskh"},
        domain_texts.redeem_out_of_stock(
            value_vnd=20_000, balance=5_000, support_link="https://t.me/cskh"
        ),
    ),
    (
        "referral.milestone",
        {
            "tier_no": 2,
            "max_claims": 10,
            "qualified": 10,
            "code_value": "TV-9",
            "value_vnd": 10_000,
            "interval": 5,
        },
        domain_texts.referral_milestone(
            tier_no=2,
            max_claims=10,
            qualified=10,
            code_value="TV-9",
            value_vnd=10_000,
            interval=5,
        ),
    ),
    (
        "referral.milestone_out_of_stock",
        {"qualified": 10, "tier_no": 2, "max_claims": 10, "support_link": "https://t.me/cskh"},
        domain_texts.referral_milestone_out_of_stock(
            qualified=10, tier_no=2, max_claims=10, support_link="https://t.me/cskh"
        ),
    ),
    ("error.rate_limited", {"seconds": 4}, domain_texts.rate_limited(4)),
]


@pytest.mark.parametrize(("key", "variables", "expected"), SAME_AS_TEXTS)
def test_ban_mac_dinh_khop_domain_texts(key, variables, expected) -> None:
    rendered = text_service.format_template(text_service.default_content(key), variables)
    assert rendered == expected


def test_dinh_dang_tien_giu_dau_cham_hang_nghin() -> None:
    """`{value_vnd:vnd}` phải cho ra `10.000`, không phải `10000` hay `10,000`."""
    body = text_service.format_template(
        text_service.default_content("code.delivered"),
        {"code_value": "TV-1", "value_vnd": 10_000},
    )
    assert "10.000đ" in body


# ══════════════════════════════════════════════════════════════════════════════
# Hàm thuần: tách biến, kiểm nội dung
# ══════════════════════════════════════════════════════════════════════════════


def test_template_vars_lay_dung_ten_bo_qua_format_spec() -> None:
    assert text_service.template_vars("a {x} b {y:vnd} c {x}") == ("x", "y")
    assert text_service.template_vars("khong co bien") == ()


def test_template_vars_ngoac_lech_thi_nem() -> None:
    with pytest.raises(TemplateError):
        text_service.template_vars("thieu ngoac {x")


def test_validate_tu_choi_noi_dung_rong() -> None:
    with pytest.raises(TemplateError):
        text_service.validate("code.delivered", "   \n  ")


def test_validate_tu_choi_thieu_bien() -> None:
    with pytest.raises(TemplateError) as exc:
        text_service.validate("code.delivered", "Ma cua ban: {value_vnd:vnd}")
    assert "{code_value}" in str(exc.value)


def test_validate_tu_choi_bien_la() -> None:
    with pytest.raises(TemplateError) as exc:
        text_service.validate("code.delivered", "{code_value} {value_vnd:vnd} {abc}")
    assert "{abc}" in str(exc.value)


def test_validate_tu_choi_khoa_khong_ton_tai() -> None:
    with pytest.raises(TemplateError):
        text_service.validate("khong.ton.tai", "abc")


def test_validate_tu_choi_format_spec_sai_kieu() -> None:
    """`{code_value:vnd}` là một chuỗi đi vào `int()` — phải chặn lúc ghi, không lúc gửi."""
    with pytest.raises(TemplateError):
        text_service.validate("code.delivered", "{code_value:vnd} {value_vnd:vnd}")


def test_preview_dien_bien_mau() -> None:
    body = text_service.preview("code.delivered")
    assert "{" not in body
    assert "TELEVIP-ABC123" in body


# ══════════════════════════════════════════════════════════════════════════════
# render / set_content / reset trên database thật
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_khoa_chua_co_trong_db_thi_ve_ban_trong_code(wired) -> None:
    """Bảng rỗng (vừa TRUNCATE) — bot vẫn nói đúng câu mặc định."""
    assert await stored_content("start.welcome") is None
    assert await text_service.render("start.welcome") == domain_texts.start_welcome()
    assert await text_service.render(
        "code.delivered", code_value="TV-1", value_vnd=10_000
    ) == domain_texts.code_delivered("TV-1", 10_000)


@pytest.mark.asyncio
async def test_sua_noi_dung_roi_render_ra_chuoi_moi(wired) -> None:
    moi = "🎁 Mã của bạn: {code_value} — trị giá {value_vnd:vnd}đ nhé!"
    await text_service.set_content("code.delivered", moi, updated_by=OWNER_ID)

    body = await text_service.render("code.delivered", code_value="TV-7", value_vnd=20_000)
    assert body == "🎁 Mã của bạn: TV-7 — trị giá 20.000đ nhé!"
    assert await stored_content("code.delivered") == moi


@pytest.mark.asyncio
async def test_thieu_bien_bat_buoc_thi_tu_choi_va_noi_dung_khong_doi(wired) -> None:
    """Hàng rào chính của khối: mất `{code_value}` thì tin trả code không có mã."""
    tot = "Mã: {code_value} · {value_vnd:vnd}đ"
    await text_service.set_content("code.delivered", tot, updated_by=OWNER_ID)

    with pytest.raises(TemplateError) as exc:
        await text_service.set_content(
            "code.delivered", "Chúc mừng! Giá trị {value_vnd:vnd}đ", updated_by=OWNER_ID
        )
    assert "{code_value}" in str(exc.value)

    assert await stored_content("code.delivered") == tot
    assert await text_service.render("code.delivered", code_value="TV-7", value_vnd=10_000) == (
        "Mã: TV-7 · 10.000đ"
    )
    # Bị từ chối thì không có gì để ghi vào sổ — chỉ còn dòng của lần ghi hợp lệ.
    assert await scalar("SELECT count(*) FROM message_templates_audit") == 1


@pytest.mark.asyncio
async def test_bien_la_bi_tu_choi(wired) -> None:
    with pytest.raises(TemplateError) as exc:
        await text_service.set_content(
            "code.delivered", "{code_value} {value_vnd:vnd} {ten_nguoi_dung}", updated_by=OWNER_ID
        )
    assert "{ten_nguoi_dung}" in str(exc.value)
    assert await stored_content("code.delivered") is None


@pytest.mark.asyncio
async def test_reset_tra_ve_dung_chuoi_trong_domain_texts(wired) -> None:
    await text_service.set_content("start.welcome", "Xin chào bản mới", updated_by=OWNER_ID)
    assert await text_service.render("start.welcome") == "Xin chào bản mới"

    back = await text_service.reset("start.welcome", updated_by=OWNER_ID)
    assert back == domain_texts.start_welcome()
    assert await text_service.render("start.welcome") == domain_texts.start_welcome()


@pytest.mark.asyncio
async def test_moi_lan_sua_deu_co_duong_quay_lui(wired) -> None:
    await text_service.set_content("start.welcome", "bản 1", updated_by=OWNER_ID)
    await text_service.set_content("start.welcome", "bản 2", updated_by=OWNER_ID)

    async with db_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT old_content, new_content, changed_by FROM message_templates_audit "
                    "WHERE key = 'start.welcome' ORDER BY audit_id"
                )
            )
        ).all()

    assert [r.new_content for r in rows] == ["bản 1", "bản 2"]
    assert rows[0].old_content is None
    assert rows[1].old_content == "bản 1"
    assert {r.changed_by for r in rows} == {OWNER_ID}


@pytest.mark.asyncio
async def test_ban_trong_db_hong_thi_van_ve_ban_mac_dinh(wired) -> None:
    """Ai đó sửa thẳng database, bỏ qua `set_content` — người dùng vẫn nhận câu tử tế."""
    await run_sql(
        "INSERT INTO message_templates (key, content, label_vi, required_vars) "
        "VALUES ('start.welcome', 'hỏng {khong_co_bien}', 'x', '[]'::jsonb)"
    )
    text_service.invalidate()
    assert await text_service.render("start.welcome") == domain_texts.start_welcome()


@pytest.mark.asyncio
async def test_render_khoa_la_thi_nem(wired) -> None:
    with pytest.raises(TemplateError):
        await text_service.render("khong.ton.tai.dau")


@pytest.mark.asyncio
async def test_list_keys_loc_theo_tien_to_va_danh_dau_da_sua(wired) -> None:
    await text_service.set_content("tanthu.step1", "bước 1 mới", updated_by=OWNER_ID)

    infos = await text_service.list_keys("tanthu.")
    assert infos, "phải liệt kê cả khoá chưa có dòng trong bảng"
    assert all(info.key.startswith("tanthu.") for info in infos)
    changed = {info.key for info in infos if info.customized}
    assert changed == {"tanthu.step1"}


# ══════════════════════════════════════════════════════════════════════════════
# Lệnh admin
# ══════════════════════════════════════════════════════════════════════════════


def test_split_key_and_content_giu_xuong_dong() -> None:
    key, content = admin_texts.split_key_and_content("code.delivered dòng 1\ndòng 2")
    assert key == "code.delivered"
    assert content == "dòng 1\ndòng 2"


def test_raw_argument_giu_xuong_dong() -> None:
    update = make_update(OWNER_ID, "/suanoidung start.welcome dòng 1\ndòng 2")
    assert admin_texts.raw_argument(update) == "start.welcome dòng 1\ndòng 2"

    with_bot = make_update(OWNER_ID, "/suanoidung@televip_bot start.welcome a\nb")
    assert admin_texts.raw_argument(with_bot) == "start.welcome a\nb"


@pytest.mark.asyncio
async def test_noidung_liet_ke_va_loc_tien_to(wired) -> None:
    await make_admin(OWNER_ID, "owner", ("/noidung",))

    sender = FakeSender()
    await admin_texts.cmd_noidung(make_update(OWNER_ID), make_context(sender, "tanthu."))
    body = sender.all_text

    assert "tanthu.step1" in body
    assert "tanthu.step2" in body
    assert "code.delivered" not in body


@pytest.mark.asyncio
async def test_xemnoidung_in_bien_bat_buoc_va_ban_xem_thu(wired) -> None:
    await make_admin(OWNER_ID, "owner", ("/xemnoidung",))

    sender = FakeSender()
    await admin_texts.cmd_xemnoidung(make_update(OWNER_ID), make_context(sender, "code.delivered"))
    body = sender.last

    assert "{code_value}" in body, "phải in template gốc"
    assert "{value_vnd}" in body or "{value_vnd:vnd}" in body
    assert "TELEVIP-ABC123" in body, "phải kèm bản xem thử đã điền biến mẫu"
    assert "10.000đ" in body


@pytest.mark.asyncio
async def test_suanoidung_giu_xuong_dong_va_tra_ve_ban_xem_thu(wired) -> None:
    await make_admin(OWNER_ID, "owner", ("/suanoidung",))

    moi = "Dòng 1: {code_value}\nDòng 2: {value_vnd:vnd}đ"
    update = make_update(OWNER_ID, f"/suanoidung code.delivered {moi}")
    sender = FakeSender()
    await admin_texts.cmd_suanoidung(update, make_context(sender))

    assert await stored_content("code.delivered") == moi
    assert "\n" in moi
    assert "Dòng 1: TELEVIP-ABC123" in sender.last, "trả lời phải kèm bản xem thử"


@pytest.mark.asyncio
async def test_suanoidung_thieu_bien_thi_tu_choi_va_khong_doi(wired) -> None:
    await make_admin(OWNER_ID, "owner", ("/suanoidung",))

    update = make_update(OWNER_ID, "/suanoidung code.delivered Chúc mừng bạn nhé!")
    sender = FakeSender()
    await admin_texts.cmd_suanoidung(update, make_context(sender))

    assert await stored_content("code.delivered") is None, "từ chối thì không ghi gì"
    assert "{code_value}" in sender.last
    assert await scalar("SELECT count(*) FROM message_templates_audit") == 0


@pytest.mark.asyncio
async def test_resetnoidung_ve_ban_mac_dinh(wired) -> None:
    await make_admin(OWNER_ID, "owner", ("/suanoidung", "/resetnoidung"))

    await admin_texts.cmd_suanoidung(
        make_update(OWNER_ID, "/suanoidung start.welcome Bản thử nghiệm"),
        make_context(FakeSender()),
    )
    assert await text_service.render("start.welcome") == "Bản thử nghiệm"

    sender = FakeSender()
    await admin_texts.cmd_resetnoidung(make_update(OWNER_ID), make_context(sender, "start.welcome"))

    assert await text_service.render("start.welcome") == domain_texts.start_welcome()
    assert "TELEVIP" in sender.last


@pytest.mark.asyncio
async def test_khoa_la_thi_bao_loi_chu_khong_no(wired) -> None:
    await make_admin(OWNER_ID, "owner", ("/xemnoidung", "/resetnoidung"))

    sender = FakeSender()
    await admin_texts.cmd_xemnoidung(make_update(OWNER_ID), make_context(sender, "khong.co.that"))
    assert "khong.co.that" in sender.last

    sender2 = FakeSender()
    await admin_texts.cmd_resetnoidung(
        make_update(OWNER_ID), make_context(sender2, "khong.co.that")
    )
    assert "khong.co.that" in sender2.last


@pytest.mark.asyncio
async def test_khong_co_quyen_thi_khong_sua_duoc(wired) -> None:
    """`cskh` xem được nhưng không sửa được — và lượt từ chối để lại dấu vết."""
    await make_admin(CSKH_ID, "cskh", ("/noidung", "/xemnoidung"))

    sender = FakeSender()
    await admin_texts.cmd_suanoidung(
        make_update(CSKH_ID, "/suanoidung start.welcome Bản của tôi"), make_context(sender)
    )

    assert sender.messages == [], "từ chối quyền là IM LẶNG"
    assert await stored_content("start.welcome") is None
    denied = await scalar(
        "SELECT count(*) FROM audit_log WHERE action = '/suanoidung.denied' AND actor_id = :uid",
        {"uid": CSKH_ID},
    )
    assert denied == 1


@pytest.mark.asyncio
async def test_nguoi_ngoai_khong_doc_duoc_danh_sach(wired) -> None:
    await run_sql(
        "INSERT INTO users (user_id, username) VALUES (:uid, 'ngoai') ON CONFLICT DO NOTHING",
        {"uid": OUTSIDER_ID},
    )

    sender = FakeSender()
    await admin_texts.cmd_noidung(make_update(OUTSIDER_ID), make_context(sender))
    assert sender.messages == []


def test_commands_xuat_du_bon_lenh() -> None:
    assert set(admin_texts.COMMANDS) == {
        "noidung",
        "xemnoidung",
        "suanoidung",
        "resetnoidung",
    }
