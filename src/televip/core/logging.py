"""Log có cấu trúc, và một bộ lọc che bí mật.

Bot cũ để `httpx` ghi INFO nên **mọi dòng log chứa nguyên bot token trong URL** —
`log.txt` của bản cũ có 172 lần token xuất hiện. Ở đây token bị thay bằng `<đã che>`
ngay tại processor, trước khi dòng log được ghi ra bất cứ đâu.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

#: Token Telegram trong URL, trong chuỗi, ở bất kỳ đâu.
_TOKEN_RE = re.compile(r"\b(\d{8,12}):(AA[A-Za-z0-9_\-]{30,})")
#: Chuỗi kết nối có mật khẩu: postgresql://user:password@host
_DSN_RE = re.compile(r"(?P<scheme>[a-z+]+://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")


def _redact_text(text: str) -> str:
    text = _TOKEN_RE.sub(lambda m: f"{m.group(1)}:<đã che>", text)
    return _DSN_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:<đã che>@", text)


def redact_secrets(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Processor structlog: che token và mật khẩu ở mọi giá trị chuỗi."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _redact_text(value)
    return event_dict


class _RedactingFilter(logging.Filter):
    """Che bí mật cho các thư viện dùng logging chuẩn (httpx, telegram, sqlalchemy)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if record.args:
            record.args = tuple(
                _redact_text(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


def setup_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Gọi đúng một lần lúc khởi động tiến trình."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # httpx ở mức INFO in nguyên URL kèm token cho MỖI lời gọi API — đây chính là
    # cách token của bot cũ lọt vào log 172 lần. Bộ lọc trên đã che, nhưng im lặng
    # luôn thì log vừa sạch vừa nhẹ (log httpx từng chiếm 58% dung lượng log hệ cũ).
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
