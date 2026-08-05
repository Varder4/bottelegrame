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

from typing import Final

from telegram import Update
from telegram.ext import ContextTypes

from televip.core.clock import business_date
from televip.core.logging import get_logger
from televip.db.engine import session
from televip.domain import texts
from televip.services.admin import Handler, admin_command

# Nhập lại dưới tên cũ: phần TÍNH đã chuyển sang service để panel web dùng chung. `render()`
# ở lại đây vì nó là câu chữ cho Telegram.
from televip.services.report import (
    DEFAULT_PERIOD,
    PERIODS,
    Report,
    collect,
    parse_args,
    period_bounds,
    to_csv,
)

log = get_logger(__name__)

CMD: Final = "/baocao"

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
