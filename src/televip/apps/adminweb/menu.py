"""Bảng menu của panel — một nơi duy nhất, gác bằng quyền thật.

## Vì sao menu là DỮ LIỆU chứ không phải HTML viết tay

Source tham chiếu (BotTelegramCSKH) dựng menu bằng một mảng `navItems` trong React, và
chọn mảng nào bằng `admin?.role === 'CSKH' ? [...] : [...]`. Cách đó có hai chỗ hỏng mà ta
không lặp lại:

1. **Danh sách cứng theo vai trò sẽ trôi khỏi bảng phân quyền.** Thêm một vai trò, hay đổi
   quyền của `ketoan` bằng lệnh, thì mảng trong mã không biết gì — menu hiện ra là menu sai
   đúng vào lúc người ta cần nó nhất.
2. **Thanh bên được dựng hai lần** (máy tính và điện thoại), nên mọi thay đổi phải sửa hai
   chỗ. Lần thứ ba là lần chúng lệch nhau.

Ở đây mỗi mục khai **quyền nó cần** — bằng chính tên lệnh của bot — và `dung_menu()` hỏi
`admin_permissions` xem người đang đăng nhập có quyền đó không. Không có bảng ánh xạ vai
trò nào trong file này. Vai trò `cskh` **không nhìn thấy** mục "Kho code" vì bảng phân
quyền không có hàng đó, chứ không phải vì có một câu `if` nào đó ẩn nó đi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from televip.services import admin as admin_service


@dataclass(frozen=True, slots=True)
class MucMenu:
    duong_dan: str
    nhan: str
    icon: str
    #: Quyền cần có — **tên lệnh của bot**, không phải một không gian tên riêng cho web.
    quyen: str
    #: Màn hình chưa dựng: vẫn hiện trong menu kèm nhãn "sắp có", để người dùng biết panel
    #: sẽ đi tới đâu thay vì tưởng chức năng đó không tồn tại.
    sap_co: bool = False


#: Thứ tự ở đây là thứ tự hiện trên màn hình.
BANG_MENU: Final[tuple[MucMenu, ...]] = (
    MucMenu("/", "Tổng quan", "tongquan", "/stats"),
    MucMenu("/kho", "Kho code", "kho", "/tonkho"),
    MucMenu("/nguoidung", "Người dùng", "nguoidung", "/user", sap_co=True),
    MucMenu("/cauchu", "Câu chữ bot", "cauchu", "/noidung", sap_co=True),
    MucMenu("/cauhinh", "Cấu hình", "cauhinh", "/cauhinh", sap_co=True),
    MucMenu("/bantin", "Bắn tin", "bantin", "/broadcast", sap_co=True),
    MucMenu("/chiendich", "Chiến dịch", "chiendich", "/chiendich", sap_co=True),
    MucMenu("/baocao", "Báo cáo", "baocao", "/baocao", sap_co=True),
    MucMenu("/dinhdanh", "Tra định danh", "dinhdanh", "/checkip", sap_co=True),
    MucMenu("/nhatky", "Nhật ký", "nhatky", "/cauhinh", sap_co=True),
)


async def dung_menu(
    db: AsyncSession, *, user_id: int, role: str, duong_hien_tai: str
) -> list[dict]:
    """Menu của ĐÚNG người này, đọc từ `admin_permissions`.

    Trả về `dict` chứ không phải dataclass vì Jinja đọc `dict` gọn hơn, và template không
    nên biết gì về kiểu dữ liệu của tầng dưới.
    """
    duoc_phep = set(await admin_service.commands_for_role(db, role))
    return [
        {
            "duong_dan": muc.duong_dan,
            "nhan": muc.nhan,
            "icon": muc.icon,
            "sap_co": muc.sap_co,
            "duoc_phep": muc.quyen in duoc_phep,
            # So khớp TUYỆT ĐỐI, không dùng `startswith`: `/kho` là tiền tố của `/khoa`, và
            # một so khớp lỏng sẽ tô sáng hai mục cùng lúc.
            "dang_mo": muc.duong_dan == duong_hien_tai,
        }
        for muc in BANG_MENU
    ]


__all__ = ["BANG_MENU", "MucMenu", "dung_menu"]
