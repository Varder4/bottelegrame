"""`/chiendich` — bật, tắt, gia hạn chiến dịch mời bạn (§13.4.3, §13.5 dòng F).

    /chiendich                     trạng thái hiện tại
    /chiendich start <số ngày> [tên]
    /chiendich extend <số ngày>
    /chiendich end

**Lệnh này chỉ điều khiển CỬA SỔ THỜI GIAN, không điều khiển số tiền.**

Đó không phải là giới hạn của lệnh mà là mô tả trung thực của hệ đang chạy: bảng
`campaigns` có ba cột `interval_people` / `reward_value_vnd` / `max_claims`, nhưng
`services/referral.py` đọc ba con số ấy từ `settings` (`referral.interval`,
`referral.reward_value_vnd`, `referral.max_claims`) chứ không đọc từ hàng chiến dịch.
Hàng chiến dịch chỉ được hỏi đúng một câu: *có cửa sổ nào đang mở không* (`campaign_window`).

Nếu lệnh này nhận tham số tiền và ghi vào ba cột đó, admin sẽ đọc lại được con số mình
vừa đặt trong khi hệ thống trả một con số khác — một màn hình nói dối, đúng loại sai lệch
mà cả khối lệnh admin sinh ra để xoá bỏ. Nên ba cột ấy được ghi bằng **ảnh chụp giá trị
đang chạy tại thời điểm tạo**, và câu trả lời cho admin nói thẳng rằng muốn đổi tiền thì
dùng `/setcauhinh`.

**Không có chiến dịch nào đang chạy ⇒ không phát mốc mời bạn nào.** Fail-closed là chủ ý
của `referral.campaign_window()`: đây là đường tiêu tiền, và "chưa ai cấu hình" không phải
lý do để tự động chi. Hệ quả là `/chiendich start` là thao tác **mở van tiền**, còn
`/chiendich end` là khoá van — cả hai đều ghi `audit_log`.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.clock import VN_TZ
from televip.core.logging import get_logger
from televip.db.engine import session, transaction
from televip.domain import texts
from televip.services import referral
from televip.services.admin import Handler, admin_command, write_audit

log = get_logger(__name__)

CMD: Final = "/chiendich"

#: Trần số ngày cho một lần `start`/`extend`. Không phải con số thẩm mỹ: một cú gõ thừa
#: chữ số biến chiến dịch 30 ngày thành 300 ngày, và cửa sổ đang mở là cửa sổ đang chi.
MAX_DAYS: Final = 365

USAGE = (
    "📥 CÁCH DÙNG\n"
    "/chiendich                      xem trạng thái\n"
    "/chiendich start <số ngày> [tên]\n"
    "/chiendich extend <số ngày>     đẩy hạn kết thúc ra xa\n"
    "/chiendich end                  dừng ngay\n"
    "\n"
    "Ví dụ: /chiendich start 30 Hè rực rỡ\n"
    "\n"
    "👉 Lệnh này chỉ đặt CỬA SỔ THỜI GIAN. Mốc mời, mệnh giá thưởng và số lần tối đa\n"
    "đọc từ settings — đổi bằng /setcauhinh referral.interval | referral.reward_value_vnd\n"
    "| referral.max_claims.\n"
    "👉 Không có chiến dịch nào đang chạy = KHÔNG phát mốc mời bạn nào."
)

_SQL_RUNNING = """
SELECT campaign_id, code, name, starts_at, ends_at,
       interval_people, reward_value_vnd, max_claims
  FROM campaigns
 WHERE is_active
   AND (starts_at IS NULL OR starts_at <= now())
   AND ends_at IS NOT NULL
   AND ends_at > now()
 ORDER BY campaign_id DESC
 LIMIT 1
"""

#: Hàng gần nhất bất kể còn hiệu lực hay không — để `/chiendich` nói được "vừa kết thúc
#: lúc nào" thay vì chỉ nói "không có gì", thứ mà admin không phân biệt được với "chưa
#: từng cấu hình".
_SQL_LATEST = """
SELECT campaign_id, code, name, starts_at, ends_at, is_active
  FROM campaigns
 ORDER BY campaign_id DESC
 LIMIT 1
"""

#: Tắt MỌI chiến dịch đang bật, không chỉ cái mới nhất. Để sót một hàng bật ở giữa bảng
#: nghĩa là admin bấm "dừng", đọc được "đã dừng", mà van tiền vẫn mở.
_SQL_END_ALL = "UPDATE campaigns SET is_active = false WHERE is_active RETURNING campaign_id"

#: Khoá tư vấn xếp hàng các thao tác ĐỔI TRẠNG THÁI chiến dịch. Giữ tới hết giao dịch.
#:
#: Cần thiết vì `_SQL_END_ALL` không tự bảo vệ được: dưới READ COMMITTED, giao dịch của
#: admin B chụp ảnh bảng TRƯỚC khi hàng mới của A tồn tại, nên nó không thể tắt hàng đó,
#: rồi B chèn tiếp hàng của mình. Kết quả là **hai chiến dịch cùng bật**, và không lời
#: đáp nào in ra "đã dừng N chiến dịch" nên không ai thấy. `campaign_window()` chỉ đọc
#: `ORDER BY campaign_id DESC LIMIT 1`, nên hàng cũ trở thành đúng thứ mà docstring module
#: này nói là không được phép tồn tại: bật nhưng vô hình.
_SQL_LOCK = "SELECT pg_advisory_xact_lock(:ns, 0)"

#: Không gian tên khoá của chiến dịch — khác `event_box` để hai thứ không xếp hàng sau nhau.
_LOCK_NS_CAMPAIGN: Final = 0x4344  # "CD"

#: `code` có `UNIQUE`, nên nó phải duy nhất **theo cấu trúc**, không theo may mắn. Một
#: dấu thời gian tới giây thì hai lần bấm liên tiếp — chuyện admin làm thật khi tưởng lần
#: đầu không ăn — nổ `UniqueViolation`, một lỗi không nhánh nào bắt.
#:
#: Lấy `nextval` trước rồi chèn `campaign_id` tường minh: mã mang luôn số hiệu chiến dịch
#: (`cd-20260730-7`), vừa đọc được bằng mắt vừa không thể trùng.
_SQL_START = """
WITH so_moi AS (
  SELECT nextval(pg_get_serial_sequence('campaigns', 'campaign_id')) AS id
)
INSERT INTO campaigns
       (campaign_id, code, name, interval_people, reward_value_vnd, max_claims,
        starts_at, ends_at, is_active)
SELECT so_moi.id,
       'cd-' || to_char(now() AT TIME ZONE 'Asia/Ho_Chi_Minh', 'YYYYMMDD') || '-' || so_moi.id,
       :name, :interval_people, :reward_value_vnd, :max_claims,
       now(), now() + make_interval(days => :days), true
  FROM so_moi
RETURNING campaign_id, code, ends_at
"""

_SQL_EXTEND = """
UPDATE campaigns
   -- Cộng dồn từ hạn CŨ, không tính từ `now()`: gia hạn 7 ngày cho một chiến dịch còn 3
   -- ngày phải ra 10 ngày. Tính từ `now()` là âm thầm CẮT NGẮN 3 ngày đang có.
   SET ends_at = ends_at + make_interval(days => :days)
 WHERE campaign_id = :cid
RETURNING ends_at
"""


def _sender(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sender"]


def _chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return None if chat is None else chat.id


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(getattr(context, "args", None) or [])


def parse_days(raw: str) -> int | None:
    """Số ngày dương trong `[1, MAX_DAYS]`, hoặc `None`."""
    # `isdecimal`, KHÔNG phải `isdigit`: `"²".isdigit()` là True nhưng `int("²")` ném
    # `ValueError` — một ngoại lệ không nhánh nào bắt, thoát khỏi handler và để admin
    # không nhận được câu trả lời nào.
    if not raw.isdecimal():
        return None
    days = int(raw)
    return days if 1 <= days <= MAX_DAYS else None


def _moment(value: Any) -> str:
    """Mốc thời gian giờ VN cho màn hình vận hành. Tầng dưới lưu và truyền UTC."""
    return "—" if value is None else value.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y")


def _remaining(seconds: int) -> str:
    days, rest = divmod(max(0, seconds), 86_400)
    hours = rest // 3_600
    return f"{days} ngày {hours} giờ"


async def _render_status(db: Any) -> str:
    """Trạng thái đọc bằng CHÍNH truy vấn mà luồng phát thưởng dùng.

    `referral.campaign_window()` là thứ quyết định có phát mốc hay không. Dựng màn hình
    này bằng một truy vấn khác thì có ngày nó nói "đang chạy" trong khi tiền đã ngừng đi.
    """
    window = await referral.campaign_window(db)
    row = (await db.execute(text(_SQL_RUNNING))).one_or_none()
    rule = await referral.params(db=db)

    tien = (
        f"👥 Mốc mời: mỗi {rule.interval} người\n"
        f"💰 Thưởng: {texts.format_vnd(rule.reward_value_vnd)}đ\n"
        f"🔁 Tối đa: {rule.max_claims} lần\n"
        f"   (ba số này đọc từ settings — đổi bằng /setcauhinh, không bằng lệnh này)"
    )

    if window.is_running and row is not None:
        return (
            "🟢 CHIẾN DỊCH ĐANG CHẠY\n"
            f"\n"
            f"🏷 {row.name} (#{row.campaign_id})\n"
            f"🕒 Bắt đầu: {_moment(row.starts_at)}\n"
            f"🏁 Kết thúc: {_moment(row.ends_at)}\n"
            f"⏳ Còn lại: {_remaining(window.remaining_seconds)}\n"
            f"\n{tien}\n"
            f"\n"
            f"👉 Gia hạn: /chiendich extend 7   ·   Dừng ngay: /chiendich end"
        )

    latest = (await db.execute(text(_SQL_LATEST))).one_or_none()
    if latest is None:
        lich_su = "Chưa từng có chiến dịch nào."
    elif not latest.is_active:
        lich_su = f"Gần nhất: {latest.name} (#{latest.campaign_id}) — đã DỪNG bằng lệnh."
    else:
        lich_su = (
            f"Gần nhất: {latest.name} (#{latest.campaign_id}) — còn bật nhưng đã "
            f"HẾT HẠN lúc {_moment(latest.ends_at)}."
        )

    return (
        "🔴 KHÔNG CÓ CHIẾN DỊCH NÀO ĐANG CHẠY\n"
        f"\n"
        f"⚠️ Nghĩa là bot KHÔNG phát mốc mời bạn nào — người dùng vẫn mời được, vẫn được\n"
        f"tính là đã mời, nhưng không nhận thưởng.\n"
        f"\n"
        f"{lich_su}\n"
        f"\n{tien}\n"
        f"\n"
        f"👉 Mở lại: /chiendich start 30 Tên chiến dịch"
    )


@admin_command(CMD)
async def cmd_chiendich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    tg_user = update.effective_user
    if chat_id is None or tg_user is None:
        return
    sender = _sender(context)

    args = _args(context)
    if not args:
        async with session() as db:
            body = await _render_status(db)
        await sender.send_message(chat_id, body)
        return

    hanh_dong = args[0].strip().lower()
    if hanh_dong == "start":
        await _start(sender, chat_id, tg_user.id, args[1:])
    elif hanh_dong == "extend":
        await _extend(sender, chat_id, tg_user.id, args[1:])
    elif hanh_dong == "end":
        await _end(sender, chat_id, tg_user.id)
    else:
        await sender.send_message(chat_id, f"❌ Không hiểu: {args[0]}\n\n{USAGE}")


async def _start(sender: Any, chat_id: int, actor_id: int, rest: list[str]) -> None:
    if not rest or (days := parse_days(rest[0])) is None:
        await sender.send_message(
            chat_id, f"⚠️ Số ngày phải là số nguyên từ 1 đến {MAX_DAYS}.\n\n{USAGE}"
        )
        return
    name = " ".join(rest[1:]).strip() or "Chiến dịch mời bạn"

    rule = await referral.params()

    async with transaction() as db:
        # Xếp hàng TRƯỚC khi đọc bảng: xem chú thích `_SQL_LOCK`. Không có dòng này thì
        # hai admin bấm cùng lúc để lại hai chiến dịch cùng bật, im lặng.
        await db.execute(text(_SQL_LOCK), {"ns": _LOCK_NS_CAMPAIGN})
        # Dừng cái đang chạy TRƯỚC. `campaign_window()` lấy hàng `campaign_id` lớn nhất,
        # nên để hai hàng cùng bật thì hàng cũ thành vô hình mà vẫn nằm đó — một trạng
        # thái không ai đọc ra được từ `/chiendich`.
        da_dung = (await db.execute(text(_SQL_END_ALL))).all()
        created = (
            await db.execute(
                text(_SQL_START),
                {
                    "name": name,
                    # Ảnh chụp giá trị ĐANG CHẠY, không phải luật. Xem docstring module.
                    "interval_people": rule.interval,
                    "reward_value_vnd": rule.reward_value_vnd,
                    "max_claims": rule.max_claims,
                    "days": days,
                },
            )
        ).one()
        await write_audit(
            db,
            actor_id=actor_id,
            action="chiendich_start",
            entity_type="campaigns",
            entity_id=str(created.campaign_id),
            before={"da_dung": [r.campaign_id for r in da_dung]} if da_dung else None,
            after={"code": created.code, "name": name, "days": days},
        )

    thay_the = f"\n🛑 Đã dừng {len(da_dung)} chiến dịch đang chạy trước đó.\n" if da_dung else ""
    await sender.send_message(
        chat_id,
        (
            f"🟢 ĐÃ MỞ CHIẾN DỊCH\n"
            f"\n"
            f"🏷 {name} (#{created.campaign_id})\n"
            f"🏁 Kết thúc: {_moment(created.ends_at)} ({days} ngày)\n"
            f"{thay_the}"
            f"\n"
            f"⚠️ Từ giờ bot BẮT ĐẦU phát mốc mời bạn: mỗi {rule.interval} người = "
            f"{texts.format_vnd(rule.reward_value_vnd)}đ, tối đa {rule.max_claims} lần.\n"
            f"👉 Kiểm kho trước khi đông người: /tonkho"
        ),
    )
    log.warning(
        "chien_dich_mo",
        actor_id=actor_id,
        campaign_id=created.campaign_id,
        days=days,
        code=created.code,
    )


async def _extend(sender: Any, chat_id: int, actor_id: int, rest: list[str]) -> None:
    if not rest or (days := parse_days(rest[0])) is None:
        await sender.send_message(
            chat_id, f"⚠️ Số ngày phải là số nguyên từ 1 đến {MAX_DAYS}.\n\n{USAGE}"
        )
        return

    async with transaction() as db:
        row = (await db.execute(text(_SQL_RUNNING))).one_or_none()
        if row is None:
            # Gia hạn một chiến dịch đã hết hạn là mở lại van tiền — thao tác khác hẳn về
            # bản chất với "kéo dài cái đang mở", nên nó phải đi qua `start`.
            await sender.send_message(
                chat_id,
                "❌ Không có chiến dịch nào ĐANG CHẠY để gia hạn.\n\n"
                "Chiến dịch đã hết hạn thì mở lại bằng /chiendich start — gia hạn một cửa "
                "sổ đã đóng chính là mở lại van tiền, và hai việc đó phải nhìn khác nhau "
                "trong sổ.",
            )
            return
        han_moi = (
            await db.execute(text(_SQL_EXTEND), {"cid": row.campaign_id, "days": days})
        ).scalar_one()
        await write_audit(
            db,
            actor_id=actor_id,
            action="chiendich_extend",
            entity_type="campaigns",
            entity_id=str(row.campaign_id),
            before={"ends_at": row.ends_at.isoformat()},
            after={"ends_at": han_moi.isoformat(), "days": days},
        )

    await sender.send_message(
        chat_id,
        (
            f"⏩ ĐÃ GIA HẠN THÊM {days} NGÀY\n"
            f"\n"
            f"🏷 {row.name} (#{row.campaign_id})\n"
            f"🏁 Hạn cũ: {_moment(row.ends_at)}\n"
            f"🏁 Hạn mới: {_moment(han_moi)}"
        ),
    )
    log.info("chien_dich_gia_han", actor_id=actor_id, campaign_id=row.campaign_id, days=days)


async def _end(sender: Any, chat_id: int, actor_id: int) -> None:
    async with transaction() as db:
        # Cùng khoá với `_start`: "dừng" chạy song song với "mở" mà không xếp hàng thì có
        # thể dừng xong lại còn một hàng vừa được bật xen vào.
        await db.execute(text(_SQL_LOCK), {"ns": _LOCK_NS_CAMPAIGN})
        stopped = (await db.execute(text(_SQL_END_ALL))).all()
        if stopped:
            await write_audit(
                db,
                actor_id=actor_id,
                action="chiendich_end",
                entity_type="campaigns",
                entity_id=",".join(str(r.campaign_id) for r in stopped),
                after={"da_dung": [r.campaign_id for r in stopped]},
            )

    if not stopped:
        await sender.send_message(chat_id, "ℹ️ Không có chiến dịch nào đang bật để dừng.")
        return

    await sender.send_message(
        chat_id,
        (
            f"🛑 ĐÃ DỪNG {len(stopped)} CHIẾN DỊCH\n"
            f"\n"
            f"Từ giờ bot KHÔNG phát mốc mời bạn nào nữa. Người dùng vẫn mời được và vẫn\n"
            f"được tính là đã mời, chỉ là không nhận thưởng.\n"
            f"\n"
            f"👉 Mở lại: /chiendich start 30 Tên chiến dịch"
        ),
    )
    log.warning("chien_dich_dung", actor_id=actor_id, campaign_ids=[r.campaign_id for r in stopped])


#: Vòng quét của `main.py` nhặt bảng này.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (("chiendich", cmd_chiendich),)


__all__ = ["CMD", "COMMANDS", "MAX_DAYS", "USAGE", "cmd_chiendich", "parse_days"]
