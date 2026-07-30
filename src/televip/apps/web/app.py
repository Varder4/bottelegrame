"""Tiến trình web: phục vụ Mini App tĩnh và API xác minh.

Đây là tiến trình **duy nhất** hứng lưu lượng từ internet, nên nó cố ý giữ đúng hai việc:
kiểm danh tính (`apps/web/miniapp.py`) và trả file tĩnh. Nó KHÔNG gọi Bot API — tin nhắn
gửi cho người dùng đi qua `outbox_messages` để `tv-worker` phát, vì token bucket 30 tin/giây
của Telegram tính theo bot token chứ không theo tiến trình.

Vòng đời: database và Redis mở lúc startup, đóng lúc shutdown. Cả hai đều là singleton
theo tiến trình (`db/engine.py`, `cache/client.py`), nên chỗ này chỉ có nhiệm vụ gọi đúng
một lần ở đúng thời điểm.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from televip.apps.web import miniapp
from televip.cache.client import close_redis, get_redis, init_redis
from televip.core.config import Settings, get_settings
from televip.core.logging import get_logger, setup_logging
from televip.db.engine import dispose_engine, init_engine, session

log = get_logger(__name__)

#: File tĩnh của Mini App, ở gốc repo. `parents[4]` đi từ
#: `src/televip/apps/web/app.py` ngược lên: web → apps → televip → src → gốc repo.
WEBAPP_DIR: Path = Path(__file__).resolve().parents[4] / "webapp"


def _build_lifespan(settings: Settings) -> Any:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        init_redis(settings)
        log.info("web.khoi_dong", env=settings.env, initdata_mode=str(settings.initdata_mode))
        try:
            yield
        finally:
            # Đóng theo thứ tự ngược lúc mở. Cả hai hàm đều chịu được việc chưa từng khởi
            # tạo, nên tắt giữa chừng lúc startup lỗi cũng không sinh lỗi thứ hai che mất
            # nguyên nhân thật.
            await close_redis()
            await dispose_engine()
            log.info("web.da_dong_ket_noi")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Dựng ứng dụng ASGI.

    ``settings`` để trống thì đọc từ biến môi trường. Tham số này tồn tại cho test: nó là
    cách duy nhất trỏ ứng dụng sang database `televip_test` và một bot token cố định mà
    không phải chạm vào biến môi trường của tiến trình.
    """
    settings = settings or get_settings()
    setup_logging(settings.log_level, json_output=settings.env != "dev")

    is_dev = settings.env == "dev"
    app = FastAPI(
        title="televip web",
        version="0.1.0",
        lifespan=_build_lifespan(settings),
        # Máy chủ này mở ra internet; sơ đồ API công khai chỉ giúp người dò tìm bề mặt tấn
        # công. Ở dev thì giữ lại vì nó tiện thật.
        docs_url="/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if is_dev else None,
    )

    # Router đọc cấu hình từ đây thay vì gọi `get_settings()`: một tiến trình test dựng
    # nhiều app với settings khác nhau vẫn phải cho ra kết quả khác nhau, mà `get_settings()`
    # thì bị `lru_cache` giữ một bản duy nhất.
    app.state.settings = settings

    app.add_api_route("/health", health, methods=["GET"], name="health")
    app.include_router(miniapp.router)

    # Mount cuối cùng và chỉ khi thư mục tồn tại: `Mount("/")` nuốt mọi đường dẫn chưa khớp,
    # nên đăng ký nó trước `/health` và `/api/*` là chôn luôn hai thứ đó. Thư mục do khối
    # Mini App tạo; chưa có thì API vẫn phải chạy được.
    if WEBAPP_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
    else:
        log.warning("web.thieu_thu_muc_tinh", path=str(WEBAPP_DIR))

    return app


async def health() -> JSONResponse:
    """Trạng thái hai phụ thuộc bắt buộc.

    Trả 503 khi có thứ hỏng để bộ cân bằng tải rút tiến trình này ra khỏi vòng quay —
    một tiến trình mất database vẫn trả 200 là cách biến sự cố hạ tầng thành lỗi cho người
    dùng cuối.
    """
    db_ok = await _ping_db()
    redis_ok = await _ping_redis()
    healthy = db_ok and redis_ok

    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "db": "ok" if db_ok else "down",
            "redis": "ok" if redis_ok else "down",
        },
        status_code=200 if healthy else 503,
    )


async def _ping_db() -> bool:
    try:
        async with session() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - health check phải trả lời được cả khi hỏng
        log.warning("health.db_hong", error=str(exc))
        return False
    return True


async def _ping_redis() -> bool:
    try:
        await get_redis().ping()
    except Exception as exc:  # noqa: BLE001 - như trên
        log.warning("health.redis_hong", error=str(exc))
        return False
    return True
