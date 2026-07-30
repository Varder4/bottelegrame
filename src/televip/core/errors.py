"""Cây lỗi nghiệp vụ.

Mỗi lỗi mang đủ thông tin để tầng trên quyết định trả lời người dùng thế nào, mà không
cần đọc chuỗi thông báo. Bot cũ nuốt lỗi bằng `except Exception` trần rồi cộng biến đếm
`failed += 1` — mất cả nguyên nhân lẫn khả năng thử lại.
"""

from __future__ import annotations


class TelevipError(Exception):
    """Gốc của mọi lỗi nghiệp vụ. Lỗi hạ tầng KHÔNG kế thừa từ đây."""

    #: Có nên thử lại cùng thao tác không
    retryable: bool = False


# ── Cấp code ────────────────────────────────────────────────────────


class CodeError(TelevipError):
    pass


class OutOfStock(CodeError):
    """Kho hết code cho (loại, mệnh giá) yêu cầu."""

    def __init__(self, code_type: str, value_vnd: int | None = None) -> None:
        self.code_type = code_type
        self.value_vnd = value_vnd
        super().__init__(f"Hết code: {code_type}" + (f"/{value_vnd}đ" if value_vnd else ""))


class AlreadyClaimed(CodeError):
    """Người dùng đã nhận loại quà này rồi.

    Mang theo ``existing_code`` để tầng trên trả lại **đúng mã cũ** thay vì báo lỗi —
    bấm nút hai lần phải cho ra cùng một kết quả, không phải hai mã.
    """

    def __init__(self, grant_type: str, existing_code: str | None = None) -> None:
        self.grant_type = grant_type
        self.existing_code = existing_code
        super().__init__(f"Đã nhận rồi: {grant_type}")


class BudgetExceeded(CodeError):
    """Chạm trần ngân sách của đợt phát."""

    def __init__(self, spent_vnd: int, cap_vnd: int) -> None:
        self.spent_vnd = spent_vnd
        self.cap_vnd = cap_vnd
        super().__init__(f"Chạm trần ngân sách: {spent_vnd:,}đ / {cap_vnd:,}đ")


# ── Điều kiện người dùng ────────────────────────────────────────────


class NotVerified(TelevipError):
    """Chưa qua bước xác minh Mini App."""


class NotInRequiredChats(TelevipError):
    """Chưa tham gia đủ nhóm/kênh bắt buộc."""

    def __init__(self, missing_chat_ids: list[int], joined: int, total: int) -> None:
        self.missing_chat_ids = missing_chat_ids
        self.joined = joined
        self.total = total
        super().__init__(f"Thiếu nhóm: {joined}/{total}")


class UserBanned(TelevipError):
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(f"Tài khoản bị khoá: {reason or 'không rõ lý do'}")


class RateLimited(TelevipError):
    """Bấm quá nhanh — chống spam phía người dùng."""

    retryable = True

    def __init__(self, action: str, retry_after_seconds: float) -> None:
        self.action = action
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Chờ {retry_after_seconds:.0f} giây rồi thử lại ({action})")


# ── Xác thực Mini App ───────────────────────────────────────────────


class InitDataError(TelevipError):
    """Chữ ký Telegram WebApp không hợp lệ."""


class InitDataExpired(InitDataError):
    def __init__(self, age_seconds: float, ttl_seconds: int) -> None:
        self.age_seconds = age_seconds
        self.ttl_seconds = ttl_seconds
        super().__init__(f"initData quá hạn: {age_seconds:.0f}s > {ttl_seconds}s")


class InitDataReplay(InitDataError):
    """Chữ ký hợp lệ nhưng đã được dùng rồi — dấu hiệu tấn công phát lại."""


# ── Quyền admin ─────────────────────────────────────────────────────


class PermissionDenied(TelevipError):
    def __init__(self, user_id: int, command: str) -> None:
        self.user_id = user_id
        self.command = command
        super().__init__(f"user {user_id} không có quyền chạy {command}")


# ── Cấu hình ────────────────────────────────────────────────────────


class ConfigError(TelevipError):
    """Khoá cấu hình thiếu hoặc sai kiểu trong bảng `settings`."""
