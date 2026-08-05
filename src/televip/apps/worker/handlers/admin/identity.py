"""`/checkip` — tra tín hiệu định danh (§13.4.2 mục 13).

    /checkip <@user|user_id>   mọi tín hiệu của một người, kèm số tài khoản dùng chung
    /checkip <IP>              mọi tài khoản từng dùng một IP

Lệnh này **chỉ ĐỌC và chỉ ĐO**. Nó không chặn ai, không chấm điểm ai, không tự kết luận
ai gian lận — và đó là chủ ý, không phải thiếu sót:

- `04-fraud.md` quy định các bảng luật để rỗng cho tới khi có **bốn tuần số liệu shadow**,
  vì bật luật sớm là chặn oan người thật. Nhưng bốn tuần ấy chỉ bắt đầu đếm khi có người
  nhìn vào số liệu — và đây là cái nhìn đó.
- Cột `risk_assessments.score` chưa có ai ghi. In ra `0` sẽ là một con số **bịa**, tệ hơn
  hẳn việc nói thẳng "chưa chấm điểm". Màn hình này nói ra đúng những gì đo được.

Hai hàng rào về quyền riêng tư, cả hai đều cố ý:

1. **Không đường nào đi từ tín hiệu ngược về @username của người khác.** `/checkip <IP>`
   trả `user_id` chứ không trả tên — muốn biết ai thì gõ `/user <id>`, và lượt đó để lại
   dấu trong `audit_log`. §13.2.2 cấm để lộ tài khoản khác trong thông báo tự động; ở đây
   là màn hình admin nên nới hơn, nhưng vẫn không biến một IP thành một danh bạ.
2. **IP hiện dạng rút gọn.** Đủ để đối chiếu hai lượt tra, không đủ để dán đi nơi khác.
"""

from __future__ import annotations

from typing import Any, Final

from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers.admin.ops import resolve_user
from televip.core.clock import VN_TZ
from televip.core.logging import get_logger
from televip.db.engine import session
from televip.services import identity_admin
from televip.services.admin import Handler, admin_command

# Nhập lại dưới tên cũ: phần ĐỌC và hai hàng rào riêng tư đã chuyển sang service để panel
# web dùng chung. Câu chữ ở lại đây.
from televip.services.identity_admin import DANG_CHU_Y, LIMIT, parse_ip, rut_gon_ip
from televip.services.identity_admin import nhan_tin_hieu as _nhan

log = get_logger(__name__)

CMD: Final = "/checkip"

USAGE = (
    "📥 CÁCH DÙNG\n"
    "/checkip @username\n"
    "/checkip 123456789\n"
    "/checkip 1.2.3.4        (hoặc IPv6)\n"
    "\n"
    "👉 Lệnh này chỉ ĐỌC. Nó không chặn ai và không chấm điểm ai — lớp luật chống gian\n"
    "lận cố ý chưa bật cho tới khi có đủ số liệu đo."
)


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return None if chat is None else chat.id


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


def _moment(value: Any) -> str:
    return "—" if value is None else value.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y")


@admin_command(CMD, mutates=False)
async def cmd_checkip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    sender = _sender(context)

    args = _args(context)
    if not args:
        await sender.send_message(chat_id, USAGE)
        return

    token = args[0].strip()
    ip = parse_ip(token)
    if ip is not None:
        await sender.send_message(chat_id, await _tra_theo_gia_tri(loai="ip", gia_tri=ip))
        return

    async with session() as db:
        user_id = await resolve_user(db, token)
    if user_id is None:
        await sender.send_message(
            chat_id, f"❌ Không tìm thấy người dùng `{token}`, và cũng không phải một IP hợp lệ."
        )
        return
    await sender.send_message(chat_id, await _tra_theo_nguoi(user_id))


async def _tra_theo_nguoi(user_id: int) -> str:
    async with session() as db:
        signals = await identity_admin.tin_hieu_cua_nguoi(db, user_id, limit=LIMIT)
        events = await identity_admin.luot_xac_minh(db, user_id)

    lines = [f"🔎 TÍN HIỆU ĐỊNH DANH — {user_id}", ""]

    if not signals:
        # Nói rõ VÌ SAO trống, không để admin đoán giữa "sạch" và "chưa đo".
        lines += [
            "(chưa có tín hiệu nào)",
            "",
            "Tín hiệu chỉ được ghi khi người này xác minh qua Mini App. Chưa xác minh",
            "hoặc đã xác minh từ trước khi bật thu thập thì ở đây trống.",
        ]
        return "\n".join(lines)

    for row in signals:
        canh_bao = " ⚠️" if row.so_tai_khoan >= DANG_CHU_Y else ""
        lines.append(
            f"• [{row.signal_type}] {_nhan(row.signal_type, row.signal_value)}\n"
            f"  👥 {row.so_tai_khoan} tài khoản dùng chung{canh_bao}"
            f" · {row.hits} lượt · gần nhất {_moment(row.last_seen)}"
        )

    if events:
        lines += ["", "🧾 LƯỢT XÁC MINH GẦN NHẤT:"]
        for e in events:
            asn = e.asn or "—"
            quoc_gia = e.country or "—"
            lines.append(
                f"• {_moment(e.created_at)} · {e.verdict} · ASN {asn} · {quoc_gia}"
                # `e.ip` ĐÃ được service rút gọn — không rút gọn lần thứ hai ở đây.
                f" · IP {e.ip or '—'}"
            )

    lines += ["", _chan_trang(), f"👉 Hồ sơ đầy đủ: /user {user_id}"]
    return "\n".join(lines)


async def _tra_theo_gia_tri(*, loai: str, gia_tri: str) -> str:
    async with session() as db:
        owner = await identity_admin.chu_tin_hieu(db, loai=loai, gia_tri=gia_tri)
        rows = await identity_admin.tai_khoan_theo_tin_hieu(
            db, loai=loai, gia_tri=gia_tri, limit=LIMIT
        )

    hien = rut_gon_ip(gia_tri) if loai == "ip" else gia_tri
    if not rows:
        return f"🔎 {hien}\n\n(chưa có tài khoản nào gắn với giá trị này)"

    tong = owner.user_count if owner is not None else len(rows)
    canh_bao = "\n⚠️ Nhiều tài khoản dùng chung — đáng xem kỹ." if tong >= DANG_CHU_Y else ""

    lines = [
        f"🔎 {hien}",
        f"👥 {tong} tài khoản{canh_bao}",
        "",
    ]
    for r in rows:
        dau = "🚫" if r.dang_khoa else ("✅" if r.da_xac_minh else "⬜")
        lines.append(
            f"{dau} {r.user_id} · {r.hits} lượt · {_moment(r.first_seen)} → {_moment(r.last_seen)}"
        )
    if tong > len(rows):
        lines.append(f"… và {tong - len(rows)} tài khoản nữa (chỉ hiện {LIMIT} dòng)")

    lines += ["", _chan_trang(), "👉 Xem một người: /user <id>"]
    return "\n".join(lines)


def _chan_trang() -> str:
    """Nói thẳng giới hạn của màn hình này.

    Không có dòng này, một màn hình toàn tín hiệu trông như một kết luận — và người đọc
    sẽ dùng nó để khoá tài khoản. Nhiều người thật dùng chung một IP là chuyện bình
    thường ở Việt Nam: NAT của nhà mạng di động, wifi quán, ký túc xá.
    """
    return (
        "ℹ️ Đây là SỐ ĐO, không phải kết luận. Nhiều tài khoản chung một IP là chuyện\n"
        "bình thường (NAT nhà mạng, wifi quán, ký túc xá). Lớp chấm điểm rủi ro chưa bật."
    )


#: Vòng quét của `main.py` nhặt bảng này.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (("checkip", cmd_checkip),)


__all__ = [
    "CMD",
    "COMMANDS",
    "DANG_CHU_Y",
    "LIMIT",
    "cmd_checkip",
    "parse_ip",
    "rut_gon_ip",
]
