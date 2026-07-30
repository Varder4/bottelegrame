"""`/baocao` — báo cáo chi theo ngày / tuần / tháng (§13.4.3).

    /baocao [ngay|tuan|thang] [csv]

Lệnh này tồn tại để trả lời **một** câu hỏi: *kỳ này đã chi bao nhiêu, cho cái gì.* Nó là
điều kiện để trần ngân sách 12.000.000đ/đợt kiểm soát được — một con số trần không có
cách đọc số đã chi thì chỉ là một dòng cấu hình.

Ba luật của file:

1. **Mọi con số đếm trên `code_grants` với `state = 'delivered'`.** Không đếm trên `codes`
   (kho không biết ai nhận), không đếm trên bộ đếm tổng của `users` (bộ đếm là bản sao
   hiển thị và nó đã trôi khỏi sự thật ở hệ cũ). `delivered` chứ không phải `reserved`:
   một mã giữ chỗ mà gửi hỏng sẽ quay về kho, tính nó là đã chi là báo cáo thừa tiền.

2. **Ranh giới kỳ tính theo NGÀY NGHIỆP VỤ giờ VN**, qua `vn_day_bounds`. Cắt kỳ theo UTC
   đẩy 7 tiếng cuối mỗi ngày sang kỳ sau — cùng cái bẫy đã làm streak điểm danh của bot cũ
   lệch một ngày.

3. **`csv` xuất đúng những dòng vừa hiện trên màn hình.** Không phải một truy vấn thứ hai:
   hai truy vấn cho cùng một báo cáo là hai con số khác nhau ngay khi có ai đó nhận code
   giữa hai lần chạy, và người đọc không có cách nào biết bản nào đúng.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from televip.core.clock import business_date, vn_day_bounds
from televip.core.logging import get_logger
from televip.db.engine import session
from televip.domain import texts
from televip.services.admin import Handler, admin_command

log = get_logger(__name__)

CMD: Final = "/baocao"

#: Kỳ → số ngày lùi lại tính cả hôm nay. `tuan` là 7 ngày gần nhất chứ không phải "tuần
#: theo lịch": câu hỏi vận hành luôn là "bảy ngày qua tiêu bao nhiêu", và một báo cáo
#: chạy sáng thứ Hai mà chỉ có dữ liệu của một ngày là vô dụng.
PERIODS: Final[dict[str, tuple[int, str]]] = {
    "ngay": (1, "HÔM NAY"),
    "tuan": (7, "7 NGÀY QUA"),
    "thang": (30, "30 NGÀY QUA"),
}

DEFAULT_PERIOD: Final = "ngay"

USAGE = (
    "📥 CÁCH DÙNG\n"
    "/baocao [ngay|tuan|thang] [csv]\n"
    "\n"
    "Ví dụ:\n"
    "• /baocao            (hôm nay)\n"
    "• /baocao tuan\n"
    "• /baocao thang csv  (kèm file CSV)\n"
    "\n"
    "👉 Mọi con số đếm trên sổ phát hành, chỉ tính mã ĐÃ GIAO tới tay người dùng."
)

#: Chi tiết theo (loại phát × mệnh giá). `grant_type` là loại trong SỔ CÁI, không phải
#: loại trong kho — đó là thứ trả lời "tiền đi vào luồng nào".
_SQL_BY_TYPE = """
SELECT grant_type,
       value_vnd,
       count(*)::int                   AS so_ma,
       coalesce(sum(value_vnd), 0)::bigint AS tong_vnd
  FROM code_grants
 WHERE state = 'delivered'
   AND delivered_at >= :tu AND delivered_at < :den
 GROUP BY grant_type, value_vnd
 ORDER BY tong_vnd DESC, grant_type, value_vnd
"""

_SQL_TOTALS = """
SELECT
  (SELECT count(*)::int FROM code_grants
    WHERE state = 'delivered' AND delivered_at >= :tu AND delivered_at < :den) AS so_ma,
  (SELECT coalesce(sum(value_vnd), 0)::bigint FROM code_grants
    WHERE state = 'delivered' AND delivered_at >= :tu AND delivered_at < :den) AS tong_vnd,
  (SELECT count(DISTINCT user_id)::int FROM code_grants
    WHERE state = 'delivered' AND delivered_at >= :tu AND delivered_at < :den) AS so_nguoi,
  (SELECT count(*)::int FROM users
    WHERE joined_at >= :tu AND joined_at < :den)                               AS user_moi,
  -- Tử số phải cùng TỆP với mẫu số: người VÀO trong kỳ và đã xác minh. Đếm
  -- `verified_at` trong kỳ là một tệp khác hẳn (gồm cả người vào từ tháng trước),
  -- và tỉ lệ giữa hai tệp khác nhau in ra được những con số như 300%.
  (SELECT count(*)::int FROM users
    WHERE joined_at >= :tu AND joined_at < :den AND verified_at IS NOT NULL)   AS da_xac_minh,
  -- Số lượt xác minh XẢY RA trong kỳ, bất kể vào từ bao giờ. Đây là con số vận hành
  -- ("hôm nay có bao nhiêu người xác minh"), không phải tử số của tỉ lệ trên.
  (SELECT count(*)::int FROM users
    WHERE verified_at >= :tu AND verified_at < :den)                           AS xac_minh_trong_ky
"""

#: Chi của các đợt event nằm TRONG kỳ. Trần ngân sách là trần MỖI ĐỢT, nên báo cáo phải
#: tách được từng đợt chứ không chỉ đưa một con số gộp.
#:
#: Hai con số cho mỗi đợt, và chúng trả lời hai câu hỏi khác nhau:
#: - `*_ky` — chi TRONG KỲ, cùng ranh giới với con số "💰 Đã chi" ở đầu báo cáo. Không
#:   cùng ranh giới thì hai khối trên cùng một màn hình cộng ra hai tổng khác nhau và
#:   không có gì trên đó giải thích tại sao.
#: - `*_doi` — chi TRỌN ĐỜI của đợt, thứ phải đem so với trần ngân sách mỗi đợt. Một đợt
#:   mở cuối kỳ trước, tiêu tiếp sang kỳ này, có phần trong kỳ nhỏ mà tổng đã sát trần.
_SQL_EVENTS = """
SELECT e.event_id,
       e.created_at,
       count(g.grant_id) FILTER (
         WHERE g.delivered_at >= :tu AND g.delivered_at < :den)::int          AS so_ma_ky,
       coalesce(sum(g.value_vnd) FILTER (
         WHERE g.delivered_at >= :tu AND g.delivered_at < :den), 0)::bigint   AS tong_vnd_ky,
       count(g.grant_id)::int                                                 AS so_ma_doi,
       coalesce(sum(g.value_vnd), 0)::bigint                                  AS tong_vnd_doi
  FROM events e
  LEFT JOIN event_participations p ON p.event_id = e.event_id
  LEFT JOIN code_grants g ON g.grant_id = p.code_grant_id AND g.state = 'delivered'
 WHERE e.created_at >= :tu AND e.created_at < :den
 GROUP BY e.event_id, e.created_at
 ORDER BY e.event_id
"""


@dataclass(frozen=True, slots=True)
class Report:
    period: str
    label: str
    tu: datetime
    den: datetime
    tong: Any
    dong: list[Any]
    events: list[Any]


def parse_args(args: list[str]) -> tuple[str, bool] | None:
    """`(kỳ, có xuất csv)`, hoặc `None` nếu có token không hiểu.

    Không đoán bừa: một token lạ thường là admin gõ nhầm tên kỳ, và im lặng chạy kỳ mặc
    định sẽ trả về một báo cáo trông hợp lệ cho một khoảng thời gian khác với ý định.
    """
    period = DEFAULT_PERIOD
    xuat_csv = False
    for token in args:
        low = token.strip().lower()
        if low in PERIODS:
            period = low
        elif low == "csv":
            xuat_csv = True
        elif low:
            return None
    return period, xuat_csv


def period_bounds(period: str, today: date) -> tuple[datetime, datetime]:
    """Khoảng UTC của kỳ, cắt theo ranh giới ngày nghiệp vụ giờ VN."""
    days, _ = PERIODS[period]
    tu, _ = vn_day_bounds(today - timedelta(days=days - 1))
    _, den = vn_day_bounds(today)
    return tu, den


async def collect(db: Any, *, period: str, today: date) -> Report:
    tu, den = period_bounds(period, today)
    params = {"tu": tu, "den": den}
    return Report(
        period=period,
        label=PERIODS[period][1],
        tu=tu,
        den=den,
        tong=(await db.execute(text(_SQL_TOTALS), params)).one(),
        dong=list((await db.execute(text(_SQL_BY_TYPE), params)).all()),
        events=list((await db.execute(text(_SQL_EVENTS), params)).all()),
    )


def render(report: Report) -> str:
    tong = report.tong
    lines = [
        f"📊 BÁO CÁO — {report.label}",
        "",
        f"💰 Đã chi: {texts.format_vnd(tong.tong_vnd)}đ ({tong.so_ma:,} mã)",
        f"👤 Người nhận: {tong.so_nguoi:,}",
        f"🆕 User mới: {tong.user_moi:,} · lượt xác minh trong kỳ: {tong.xac_minh_trong_ky:,}",
    ]

    # Tỉ lệ chuyển đổi chỉ có nghĩa khi mẫu số khác 0. In "0%" cho một kỳ không có ai vào
    # là bịa ra một con số; nói thẳng "chưa có ai" thì đọc được.
    if tong.user_moi:
        lines.append(
            f"📈 Trong {tong.user_moi:,} người mới: {tong.da_xac_minh:,} đã xác minh "
            f"({tong.da_xac_minh * 100 // tong.user_moi}%)"
        )

    if report.dong:
        lines += ["", "🎁 CHI THEO LUỒNG:"]
        lines += [
            f"• {r.grant_type} · {texts.value_label(r.value_vnd)}: "
            f"{r.so_ma:,} mã = {texts.format_vnd(r.tong_vnd)}đ"
            for r in report.dong
        ]

    if report.events:
        lines += ["", "📢 EVENT ĐẬP HỘP TRONG KỲ:"]
        for r in report.events:
            dong = f"• Event #{r.event_id}: {r.so_ma_ky:,} mã = {texts.format_vnd(r.tong_vnd_ky)}đ"
            # Chỉ in tổng trọn đời khi nó KHÁC phần trong kỳ. In luôn hai con số bằng nhau
            # ở mọi dòng là nhiễu; im lặng khi chúng khác nhau mới là chỗ mất tiền.
            if r.tong_vnd_doi != r.tong_vnd_ky:
                dong += f"  (trọn đợt: {texts.format_vnd(r.tong_vnd_doi)}đ)"
            lines.append(dong)

    if not report.dong and not report.events:
        lines += ["", "(kỳ này chưa phát mã nào)"]

    return "\n".join(lines)


def to_csv(report: Report) -> bytes:
    """Đúng những dòng vừa hiện trên màn hình, không phải một truy vấn thứ hai.

    UTF-8 **có BOM**: Excel bản Việt mở CSV UTF-8 không BOM thành chữ vỡ, và người nhận
    báo cáo này mở bằng Excel chứ không mở bằng trình soạn thảo.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ky", "tu", "den", "nhom", "loai", "menh_gia_vnd", "so_ma", "tong_vnd"])
    khoang = [report.period, report.tu.isoformat(), report.den.isoformat()]
    for r in report.dong:
        writer.writerow([*khoang, "luong", r.grant_type, r.value_vnd, r.so_ma, r.tong_vnd])
    for r in report.events:
        writer.writerow(
            [*khoang, "event_trong_ky", f"event_{r.event_id}", "", r.so_ma_ky, r.tong_vnd_ky]
        )
        writer.writerow(
            [*khoang, "event_tron_doi", f"event_{r.event_id}", "", r.so_ma_doi, r.tong_vnd_doi]
        )
    return buf.getvalue().encode("utf-8-sig")


@admin_command(CMD, mutates=False)
async def cmd_baocao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    sender = context.application.bot_data["sender"]

    parsed = parse_args(list(getattr(context, "args", None) or []))
    if parsed is None:
        await sender.send_message(chat.id, USAGE)
        return
    period, xuat_csv = parsed

    async with session() as db:
        report = await collect(db, period=period, today=business_date())

    await sender.send_message(chat.id, render(report))

    if xuat_csv:
        ten = f"baocao_{period}_{business_date().isoformat()}.csv"
        sent = await sender.send_document(chat.id, to_csv(report), filename=ten)
        if sent is None:
            # Con số đã tới nơi ở tin trên; chỉ file là không. Nói ra thay vì để admin
            # ngồi đợi một tệp không bao giờ đến.
            await sender.send_message(chat.id, "⚠️ Gửi file CSV thất bại — số liệu ở trên vẫn đủ.")


#: Vòng quét của `main.py` nhặt bảng này.
COMMANDS: Final[tuple[tuple[str, Handler], ...]] = (("baocao", cmd_baocao),)


__all__ = [
    "CMD",
    "COMMANDS",
    "DEFAULT_PERIOD",
    "PERIODS",
    "Report",
    "cmd_baocao",
    "collect",
    "parse_args",
    "period_bounds",
    "render",
    "to_csv",
]
