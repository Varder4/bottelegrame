"""Sinh khoá tất định cho grant và idempotency.

"Tất định" nghĩa là cùng đầu vào luôn cho ra cùng khoá. Đó là thứ biến "bấm nút hai lần"
từ một lỗi phát trùng code thành một thao tác vô hại: lần thứ hai đụng UNIQUE constraint,
hệ thống trả lại đúng kết quả cũ.

Hệ cũ không có khái niệm này, nên bấm nút nhiều lần khi bot phản hồi chậm là nhận nhiều
code — 226 người đã nhận tổng cộng 544 code tân thủ thừa theo cách đó.
"""

from __future__ import annotations

import hashlib


def grant_key_tanthu(user_id: int) -> str:
    """Code tân thủ: mỗi người đúng một lần, vĩnh viễn."""
    return f"tanthu:{user_id}"


def grant_key_referral_tier(user_id: int, tier_no: int) -> str:
    """Mốc thưởng mời bạn: mỗi người mỗi mốc đúng một lần."""
    return f"refmile:{user_id}:{tier_no}"


def grant_key_event(event_id: int, user_id: int) -> str:
    """Đập hộp: mỗi người mỗi đợt event đúng một lần."""
    return f"event:{event_id}:{user_id}"


def grant_key_checkin_redeem(user_id: int, business_day: str) -> str:
    """Đổi điểm lấy code: khoá theo ngày nghiệp vụ."""
    return f"redeem:{user_id}:{business_day}"


def grant_key_share_event(user_id: int) -> str:
    return f"eventshare:{user_id}"


def grant_key_admin_manual(user_id: int, note: str) -> str:
    """Admin trao tay — `note` phân biệt các lần trao khác nhau cho cùng một người."""
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:16]
    return f"manual:{user_id}:{digest}"


def idempotency_key(scope: str, *parts: object) -> str:
    """Khoá idempotency chung cho các thao tác ghi.

    Băm để độ dài luôn cố định dù đầu vào dài bao nhiêu, và giữ `scope` ở dạng đọc được
    để nhìn khoá là biết nó thuộc luồng nào lúc điều tra sự cố.
    """
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{scope}:{digest}"


def outbox_key(chat_id: int, purpose: str, *parts: object) -> str:
    """Khoá chống gửi trùng một tin nhắn cho cùng một người.

    Worker có thể lấy lại cùng một việc sau khi restart giữa chừng; khoá này đảm bảo
    người dùng không nhận hai lần cùng một tin.
    """
    return idempotency_key(f"outbox:{purpose}", chat_id, *parts)
