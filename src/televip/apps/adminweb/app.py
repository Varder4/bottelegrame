"""Tiến trình panel quản trị.

    uvicorn televip.apps.adminweb.app:create_app --factory --port 8100

**Tiến trình RIÊNG, không dùng chung với Mini App.** Hai lý do, và cả hai đều cụ thể:

1. **Tư thế bảo mật ngược nhau.** Mini App phục vụ 19.151 người lạ; panel phục vụ một
   nhúm admin và nhìn thấy toàn bộ mã code chưa dùng. Chung tiến trình nghĩa là một lỗi ở
   đường công khai chạm được tới bộ nhớ của đường quản trị.
2. **`Mount("/")` của Mini App nuốt mọi đường dẫn chưa khớp** (`apps/web/app.py:91`).
   Thêm route panel vào đó là phải cẩn thận thứ tự đăng ký mãi mãi.

Tách tiến trình còn cho một thứ nữa gần như miễn phí: `systemctl stop tv-admin` tắt được
panel trong hai giây mà bot vẫn chạy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from televip.apps.adminweb.security import SecurityHeaders
from televip.cache.client import close_redis, init_redis
from televip.core.clock import VN_TZ
from televip.core.config import get_settings
from televip.core.logging import get_logger, setup_logging
from televip.db.engine import dispose_engine, init_engine
from televip.domain import texts

log = get_logger(__name__)

_HERE: Final = Path(__file__).resolve().parent
TEMPLATES_DIR: Final = _HERE / "templates"
STATIC_DIR: Final = _HERE / "static"

#: Jinja2 với `autoescape` BẬT cho .html — mặc định của `Jinja2Templates`, nhưng nói rõ ở
#: đây vì nó là hàng rào XSS chính: `users.full_name` là văn bản tự do do 19.151 người tự
#: đặt, và panel hiện nó ra. Không bao giờ dùng `|safe` trên dữ liệu người dùng.
templates: Final = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: Định dạng số dùng chung với bot — cùng một hàm, nên `10.000đ` trên web và trong tin nhắn
#: Telegram không bao giờ khác nhau một dấu chấm. Đăng ký làm bộ lọc Jinja thay vì định
#: dạng sẵn trong route: template mới không phải nhớ gọi nó ở tầng dưới.
templates.env.filters["vnd"] = texts.format_vnd
templates.env.filters["menh_gia"] = texts.value_label


def _gio_vn(moment: datetime | None) -> str:
    """Mốc thời gian theo giờ Việt Nam. Người vận hành ở VN, database lưu UTC.

    In thẳng UTC ra màn hình là mời người đọc tự trừ 7 tiếng — và họ sẽ quên đúng một lần,
    vào lúc đang so hai mốc để tìm nguyên nhân một sự cố.
    """
    if moment is None:
        return "—"
    return moment.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y")


templates.env.filters["giovn"] = _gio_vn

#: Tên hiển thị — CHÍNH hàm mà bot dùng, để web và tin nhắn Telegram gọi một người
#: bằng cùng một cái tên. Là global chứ không phải filter vì nó nhận hai tham số.
#: Giá trị trả về là văn bản THÔ của người dùng (hàm này viết cho Telegram, không
#: escape gì) — template phải bọc nó trong <bdi> và để autoescape lo phần HTML.
templates.env.globals["ten_hien"] = texts.display_name


@asynccontextmanager
async def _vong_doi(app: FastAPI) -> AsyncIterator[None]:
    """Dọn engine và Redis lúc tắt.

    `lifespan` chứ không `@app.on_event`: cái sau đã bị khai tử, và repo này biến
    `DeprecationWarning` từ `televip.*` thành LỖI (`pyproject.toml`), nên dùng nó là cả bộ
    kiểm thử đỏ.
    """
    yield
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.env != "dev")

    init_engine(settings)
    init_redis(settings)

    app = FastAPI(
        lifespan=_vong_doi,
        title="HK79 - Quà Tặng — Quản trị",
        # Tắt hẳn tài liệu API tự sinh: panel không phải một API công khai, và
        # `/docs` là bản đồ đường dẫn miễn phí cho người dò.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Cookie `Secure` chỉ đặt được qua HTTPS. Trên máy dev chạy `http://localhost` thì
    # trình duyệt lặng lẽ VỨT cookie có cờ `Secure` — đăng nhập xong quay lại vẫn thấy
    # trang đăng nhập, không có lỗi nào hiện ra.
    app.state.secure_cookies = settings.env != "dev"
    app.state.settings = settings

    app.add_middleware(SecurityHeaders, secure=app.state.secure_cookies)

    from televip.apps.adminweb.routes import (
        auth,
        bantin,
        baocao,
        cauchu,
        cauhinh,
        chiendich,
        dashboard,
        dinhdanh,
        kho,
        nguoidung,
        nhatky,
    )

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(kho.router)
    app.include_router(nguoidung.router)
    app.include_router(baocao.router)
    app.include_router(dinhdanh.router)
    app.include_router(nhatky.router)
    app.include_router(chiendich.router)
    app.include_router(cauchu.router)
    app.include_router(cauhinh.router)
    app.include_router(bantin.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    else:
        log.warning("adminweb_thieu_thu_muc_tinh", path=str(STATIC_DIR))

    log.info(
        "adminweb_khoi_dong",
        env=settings.env,
        cookie_secure=app.state.secure_cookies,
    )
    return app


__all__ = ["STATIC_DIR", "TEMPLATES_DIR", "create_app", "templates"]
