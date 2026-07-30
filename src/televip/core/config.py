"""Cấu hình đọc từ biến môi trường.

Nguyên tắc: chỉ **bí mật và hạ tầng** nằm ở đây. Mọi con số nghiệp vụ (mệnh giá code,
tỉ lệ trúng, mốc mời bạn, trần ngân sách...) nằm trong bảng `settings` của database và
đổi được bằng lệnh admin — xem `13-dac-ta-bot-moi.md` §13.6.

Fail-fast: thiếu biến bắt buộc thì ứng dụng từ chối khởi động, không chạy với giá trị mặc định
âm thầm sai.
"""

from __future__ import annotations

import enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class InitDataMode(enum.StrEnum):
    """Ba mức xác thực chữ ký Telegram Mini App.

    Bot cũ để cơ chế này bị comment suốt 7 tháng, ai gọi thẳng API với `user_id` bất kỳ
    cũng nhận được code. Ở đây `off` bị cấm tuyệt đối trong production.
    """

    OFF = "off"
    SHADOW = "shadow"  # kiểm chữ ký, sai thì chỉ ghi log — dùng để đo trước khi bật cứng
    ON = "on"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEVIP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── Telegram ────────────────────────────────────────────────────
    bot_token: str = Field(min_length=20)
    admin_group_id: int
    announce_channel_id: int | None = None

    webhook_base_url: str = ""
    webhook_secret_path: str = ""
    webhook_secret_token: str = ""

    # ── Hạ tầng ─────────────────────────────────────────────────────
    database_url: str
    db_pool_size: int = Field(default=10, ge=1, le=100)
    redis_url: str

    # ── Môi trường ──────────────────────────────────────────────────
    env: Literal["dev", "staging", "production"] = "dev"
    log_level: str = "INFO"

    # ── Bảo mật Mini App ────────────────────────────────────────────
    initdata_mode: InitDataMode = InitDataMode.ON
    initdata_ttl_seconds: int = Field(default=300, ge=30, le=3600)

    # ── GeoIP (rỗng ở dev — tra cứu sẽ fail-open) ───────────────────
    geoip_asn_db: str = ""
    geoip_country_db: str = ""

    @field_validator("bot_token")
    @classmethod
    def _token_shape(cls, v: str) -> str:
        # Định dạng Telegram: "<bot_id>:<chuỗi>". Bắt lỗi dán nhầm ngay lúc khởi động
        # thay vì để nó nổ ở lời gọi API đầu tiên.
        if ":" not in v or not v.split(":", 1)[0].isdecimal():
            raise ValueError("BOT_TOKEN sai định dạng, phải là '<bot_id>:<chuỗi>'")
        return v

    @field_validator("initdata_mode")
    @classmethod
    def _no_off_in_production(cls, v: InitDataMode, info: ValidationInfo) -> InitDataMode:
        # Đây là lỗ hổng đã tồn tại 7 tháng ở bot cũ. Không để nó tái diễn bằng
        # một biến môi trường đặt nhầm.
        if v is InitDataMode.OFF and info.data.get("env") == "production":
            raise ValueError(
                "INITDATA_MODE=off bị CẤM ở production — đó chính là lỗ hổng của bot cũ "
                "(api/app.py:424-432 bị comment 'TEMPORARY DISABLED FOR TESTING'). "
                "Dùng 'shadow' nếu cần đo trước khi bật cứng."
            )
        return v

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.split(":", 1)[0])

    @property
    def use_webhook(self) -> bool:
        return bool(self.webhook_base_url and self.webhook_secret_path)

    def redacted(self) -> dict[str, object]:
        """Bản sao an toàn để ghi log lúc khởi động."""
        data = self.model_dump()
        data["bot_token"] = f"{self.bot_id}:<đã che>"
        if data.get("webhook_secret_token"):
            data["webhook_secret_token"] = "<đã che>"  # noqa: S105 — đang XOÁ bí mật, không đặt
        if "://" in str(data.get("database_url", "")):
            scheme, rest = str(data["database_url"]).split("://", 1)
            if "@" in rest:
                data["database_url"] = f"{scheme}://<đã che>@{rest.split('@', 1)[1]}"
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
