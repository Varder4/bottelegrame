"""Canh những luật kiến trúc mà ruff không diễn đạt được.

    PYTHONPATH=src .venv/Scripts/python.exe scripts/check_architecture.py

Thoát 0 = mọi luật còn được giữ. Thoát 1 = có vi phạm, in ra file:dòng.

## Vì sao file này tồn tại

`pyproject.toml:59-60` và `core/clock.py:19` đều viện dẫn script này như một hàng rào
**đang chạy**. Nó chưa từng tồn tại. Hai luật dưới đây vẫn đang được giữ, nhưng được giữ
bằng sự cẩn thận của người viết chứ không bằng thứ gì tự động — và repo này đã có tiền lệ
đánh mất script canh gác (`gd0/scripts/check_consistency.py` cũng đã biến mất).

Một luật được viết trong tài liệu mà không có gì canh thì nó không phải luật, nó là một
lời chúc. Việc này càng quan trọng khi panel web sắp mọc thêm một tầng gọi vào cùng các
service — tầng mới không biết những luật này tồn tại.

## Luật 1 — chỉ `telegram/sender.py` được GỬI tin qua Bot API

Không phải "không được chạm vào `bot`". Đọc thuộc tính (`context.bot.username`), truyền
đối tượng bot vào service để **đọc** tư cách thành viên, hay `set_my_commands` lúc khởi
động đều hợp lệ và đang có thật trong mã.

Thứ bị cấm là **gửi**: mỗi lời gọi gửi phải đi qua `Sender` vì đó là nơi có xô token
30 tin/giây. Một lời gọi đi vòng làm cả bot ăn 429 — Telegram phạt **cả bot**, không phạt
riêng một tin. Bot cũ ngủ cứng một giây sau mỗi lô nên vừa chậm hơn hạn mức (12-23
tin/giây) vừa vẫn ăn 429.

## Luật 2 — chỉ `core/clock.py` được đọc đồng hồ tường

`datetime.now()` / `date.today()` / `datetime.utcnow()` trả giờ **máy chủ**. Ngày nghiệp
vụ của hệ này là ngày theo giờ Việt Nam. Bot cũ dùng `datetime.now().date()` naive và
streak điểm danh của người dùng lệch 7 tiếng — mỗi ngày một lần, với mọi người.

## Luật 3 — không tầng trình bày nào được UPDATE/DELETE thẳng bảng tiền

`handlers/` và `apps/` chỉ được gọi service. Các bất biến tiền (giữ chỗ nguyên tử, sổ cái
chuỗi hash, một-lần-trọn-đời) sống trong `services/`; một câu SQL viết thẳng ở tầng trình
bày đi vòng qua tất cả. Luật này được thêm vào **trước** khi panel web ra đời, đúng lúc
còn dễ giữ.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "televip"

# ── Luật 1 ──────────────────────────────────────────────────────────

#: Phương thức Bot API thật sự GỬI thứ gì đó tới người dùng.
GUI_BOT_API: frozenset[str] = frozenset(
    {
        "send_message",
        "send_photo",
        "send_document",
        "send_video",
        "send_animation",
        "send_media_group",
        "edit_message_text",
        "edit_message_caption",
        "edit_message_reply_markup",
        "answer_callback_query",
        "delete_message",
        "copy_message",
        "forward_message",
    }
)

#: File DUY NHẤT được phép gọi chúng.
CHU_NHA_GUI = "telegram/sender.py"

# ── Luật 2 ──────────────────────────────────────────────────────────

DONG_HO_CAM: frozenset[str] = frozenset({"now", "utcnow", "today"})
DONG_HO_CHU: frozenset[str] = frozenset({"datetime", "date", "time"})
CHU_NHA_DONG_HO = "core/clock.py"

# ── Luật 3 ──────────────────────────────────────────────────────────

BANG_TIEN: frozenset[str] = frozenset(
    {"codes", "code_grants", "code_ledger", "points_ledger", "referral_rewards"}
)
#: Tầng trình bày: gọi service, không tự viết SQL đổi trạng thái bảng tiền.
TANG_TRINH_BAY = ("apps/worker/handlers/", "apps/web/", "apps/adminweb/")


def _duong_dan(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _la_chuoi(node: ast.AST) -> str | None:
    """Chuỗi hằng của một node, kể cả f-string chỉ gồm phần chữ."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str)
        )
    return None


def kiem_gui(path: Path, cay: ast.AST) -> list[str]:
    if _duong_dan(path) == CHU_NHA_GUI:
        return []
    loi = []
    for node in ast.walk(cay):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in GUI_BOT_API:
            continue
        # `sender.send_message(...)` là ĐÚNG — đó chính là lớp bọc. Chỉ bắt khi đối tượng
        # nhận lời gọi là một `bot` nào đó.
        goc = node.func.value
        ten_goc = goc.attr if isinstance(goc, ast.Attribute) else getattr(goc, "id", "")
        if ten_goc in {"bot", "_bot"}:
            loi.append(
                f"{_duong_dan(path)}:{node.lineno} gọi thẳng Bot API `{ten_goc}."
                f"{node.func.attr}()` — phải đi qua `Sender` để còn qua xô token"
            )
    return loi


def kiem_dong_ho(path: Path, cay: ast.AST) -> list[str]:
    if _duong_dan(path) == CHU_NHA_DONG_HO:
        return []
    loi = []
    for node in ast.walk(cay):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in DONG_HO_CAM:
            continue
        goc = node.func.value
        if isinstance(goc, ast.Name) and goc.id in DONG_HO_CHU:
            loi.append(
                f"{_duong_dan(path)}:{node.lineno} đọc đồng hồ tường "
                f"`{goc.id}.{node.func.attr}()` — dùng `core.clock` "
                f"(ngày nghiệp vụ tính theo giờ VN)"
            )
    return loi


def kiem_sql_tang_trinh_bay(path: Path, cay: ast.AST) -> list[str]:
    rel = _duong_dan(path)
    if not any(rel.startswith(p) for p in TANG_TRINH_BAY):
        return []
    loi = []
    for node in ast.walk(cay):
        chuoi = _la_chuoi(node)
        if not chuoi:
            continue
        hoa = " ".join(chuoi.split()).upper()
        if not any(hoa.startswith(k) or f" {k}" in hoa for k in ("UPDATE ", "DELETE FROM ")):
            continue
        for bang in BANG_TIEN:
            # S608 báo động nhầm ở đây: hai chuỗi này không phải câu SQL sắp chạy, chúng
            # là MẪU TÌM KIẾM để soi câu SQL của người khác. Script này không nối lệnh.
            sua = f"UPDATE {bang.upper()}"  # noqa: S608
            xoa = f"DELETE FROM {bang.upper()}"  # noqa: S608
            if sua in hoa or xoa in hoa:
                loi.append(
                    f"{rel}:{node.lineno} viết thẳng `UPDATE/DELETE {bang}` ở tầng trình "
                    f"bày — mọi thay đổi bảng tiền phải đi qua `services/`"
                )
                break
    return loi


LUAT = (
    ("Chỉ telegram/sender.py được GỬI qua Bot API", kiem_gui),
    ("Chỉ core/clock.py được đọc đồng hồ tường", kiem_dong_ho),
    ("Tầng trình bày không UPDATE/DELETE bảng tiền", kiem_sql_tang_trinh_bay),
)


def main() -> int:
    files = sorted(SRC.rglob("*.py"))
    if not files:
        print(f"❌ Không thấy file .py nào dưới {SRC} — kiểm tra đường dẫn.")
        return 1

    tong_loi: list[str] = []
    print(f"Soát {len(files)} file dưới src/televip/\n")
    for ten_luat, ham in LUAT:
        loi: list[str] = []
        for path in files:
            try:
                cay = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                loi.append(f"{_duong_dan(path)}: không phân tích được — {exc}")
                continue
            loi += ham(path, cay)
        dau = "❌" if loi else "✅"
        print(f"{dau} {ten_luat}" + (f"  ({len(loi)} vi phạm)" if loi else ""))
        for d in loi:
            print(f"     {d}")
        tong_loi += loi

    if tong_loi:
        print(f"\n❌ {len(tong_loi)} vi phạm kiến trúc.")
        return 1
    print("\n✅ Mọi luật kiến trúc còn được giữ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
