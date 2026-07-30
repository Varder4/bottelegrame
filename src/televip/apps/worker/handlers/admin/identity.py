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

import ipaddress
from typing import Any, Final

from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from televip.apps.worker.handlers.admin.ops import resolve_user
from televip.core.clock import VN_TZ
from televip.core.logging import get_logger
from televip.db.engine import session
from televip.services.admin import Handler, admin_command

log = get_logger(__name__)

CMD: Final = "/checkip"

#: Số dòng tối đa mỗi khối. Một IP của nhà mạng di động có thể gắn với hàng trăm tài
#: khoản; in hết vừa vượt trần Telegram vừa không ai đọc.
LIMIT: Final = 20

#: Từ bao nhiêu tài khoản dùng chung thì đánh dấu. KHÔNG phải ngưỡng chặn — không có luật
#: nào đọc con số này. Nó chỉ quyết định chỗ nào in thêm một biểu tượng ⚠️.
DANG_CHU_Y: Final = 3

USAGE = (
    "📥 CÁCH DÙNG\n"
    "/checkip @username\n"
    "/checkip 123456789\n"
    "/checkip 1.2.3.4        (hoặc IPv6)\n"
    "\n"
    "👉 Lệnh này chỉ ĐỌC. Nó không chặn ai và không chấm điểm ai — lớp luật chống gian\n"
    "lận cố ý chưa bật cho tới khi có đủ số liệu đo."
)

#: Tín hiệu của MỘT người, kèm số tài khoản dùng chung lấy sẵn từ `signal_owners`.
_SQL_BY_USER = """
SELECT s.signal_type,
       s.signal_value,
       s.hits,
       s.first_seen,
       s.last_seen,
       coalesce(o.user_count, 1) AS so_tai_khoan
  FROM identity_signals s
  LEFT JOIN signal_owners o
         ON o.signal_type = s.signal_type AND o.signal_value = s.signal_value
 WHERE s.user_id = :uid
 ORDER BY so_tai_khoan DESC, s.last_seen DESC
 LIMIT :lim
"""

#: Các tài khoản từng mang một giá trị tín hiệu.
_SQL_BY_VALUE = """
SELECT s.user_id, s.hits, s.first_seen, s.last_seen,
       (u.verified_at IS NOT NULL) AS da_xac_minh,
       (b.user_id IS NOT NULL)     AS dang_khoa
  FROM identity_signals s
  LEFT JOIN users u ON u.user_id = s.user_id
  LEFT JOIN user_bans b ON b.user_id = s.user_id AND b.unbanned_at IS NULL
 WHERE s.signal_type = :loai AND s.signal_value = :gia_tri
 ORDER BY s.last_seen DESC
 LIMIT :lim
"""

_SQL_OWNER = """
SELECT user_count, first_user_id, last_seen
  FROM signal_owners WHERE signal_type = :loai AND signal_value = :gia_tri
"""

#: Lượt xác minh gần nhất của một người — nguồn duy nhất hiện có cho ASN / quốc gia.
_SQL_EVENTS = """
SELECT event_type, host(ip) AS ip, asn, country, verdict, risk_score, created_at
  FROM verification_events
 WHERE user_id = :uid
 ORDER BY created_at DESC
 LIMIT 5
"""


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return None if chat is None else chat.id


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


def _moment(value: Any) -> str:
    return "—" if value is None else value.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y")


def parse_ip(raw: str) -> str | None:
    """Chuỗi có phải một địa chỉ IP không. Trả về **dạng chuẩn hoá**, hoặc `None`.

    Chuẩn hoá là bắt buộc chứ không phải làm đẹp: `identity_signals.signal_value` lưu
    chuỗi do `ipaddress` sinh ra, nên gõ `2405:4802:0:0::1` mà so thẳng sẽ không khớp
    dòng lưu dưới dạng `2405:4802::1`.
    """
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


def rut_gon_ip(value: str) -> str:
    """Che phần cuối địa chỉ. Đủ để đối chiếu hai lượt tra, không đủ để dán đi nơi khác."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(addr, ipaddress.IPv4Address):
        phan = value.split(".")
        return ".".join(phan[:3] + ["x"])
    phan = value.split(":")
    return ":".join(phan[:4] + ["…"])


def _nhan(signal_type: str, signal_value: str) -> str:
    return rut_gon_ip(signal_value) if signal_type == "ip" else signal_value


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
        signals = (await db.execute(text(_SQL_BY_USER), {"uid": user_id, "lim": LIMIT})).all()
        events = (await db.execute(text(_SQL_EVENTS), {"uid": user_id})).all()

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
                f" · IP {rut_gon_ip(e.ip) if e.ip else '—'}"
            )

    lines += ["", _chan_trang(), f"👉 Hồ sơ đầy đủ: /user {user_id}"]
    return "\n".join(lines)


async def _tra_theo_gia_tri(*, loai: str, gia_tri: str) -> str:
    async with session() as db:
        owner = (
            await db.execute(text(_SQL_OWNER), {"loai": loai, "gia_tri": gia_tri})
        ).one_or_none()
        rows = (
            await db.execute(text(_SQL_BY_VALUE), {"loai": loai, "gia_tri": gia_tri, "lim": LIMIT})
        ).all()

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
