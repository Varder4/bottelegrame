"""Nhóm 2 — Code & tiền. Sáu bảng, và là nhóm duy nhất mà một lỗi schema tốn tiền thật.

Hệ cũ gộp ba khái niệm vào **một** bảng `codes`, nên "đã trừ kho" và "đã tới tay người
dùng" là cùng một dòng dữ liệu. Gửi tin thất bại (timeout 15s, batch bị huỷ) là code
biến mất khỏi kho mà không ai nhận được gì. Bản mới tách làm ba, mỗi bảng trả lời đúng
một câu hỏi:

* ``codes``       — KHO.            "Còn code nào không?"
* ``code_grants`` — SỔ CÁI PHÁT HÀNH. "Ai đã được nhận gì, đã gửi tới tay chưa?"
* ``code_ledger`` — SỔ CÁI TIỀN.     "Tổng cộng đã phát ra bao nhiêu, cho ai, vì sao?"

Ba bảng còn lại là hạ tầng đỡ cho ba bảng trên: ``grant_types`` (từ điển loại phát),
``idempotency_records`` (nhớ phản hồi 24 giờ cho Mini App), ``code_pool_stats``
(đếm sẵn tồn kho cho cảnh báo hết code chạy mỗi 60 giây).

Nguồn: ``docs/ke-hoach-v2/07-db.md`` §7.2 nhóm 2 (định nghĩa cột, ràng buộc, index) và
``docs/ke-hoach-v2/03-code.md`` §3.3–3.5 (lý do từng ràng buộc tồn tại).

Ghi chú một quyết định: nhóm này khai báo ``created_at`` tường minh thay vì kế thừa
``TimestampedMixin``. Mixin đó kèm sẵn ``index=True``, mà §7.5 nguyên tắc 3 cấm đánh
index cho cột không có truy vấn; riêng ``code_grants`` thì index tự động đó còn **trùng**
với ``ix_grants_recent`` mà §7.5 liệt kê là index bắt buộc. ``UpdatedAtMixin`` không có
vấn đề này nên vẫn dùng lại.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from televip.db.base import Base, UpdatedAtMixin, user_fk_column

# ---------------------------------------------------------------------------
# Tập giá trị hợp lệ.
#
# Khai báo một lần ở đây rồi dựng chuỗi CHECK từ chính nó: `grant_types` (dữ liệu) và
# `code_grants.grant_type` (CHECK) cố ý chồng nhau, và 07-db §7.2 nhóm 2 điểm (d) bắt
# buộc phải có test CI giữ hai chỗ đó bằng nhau. Test đó đọc đúng các hằng dưới đây,
# nên thêm một loại mới mà quên seed `grant_types` sẽ đỏ ngay chứ không im lặng.
# ---------------------------------------------------------------------------

#: Loại code trong KHO — đúng 5 giá trị mà `/add_giffcode` nhận (13 §13.4.2).
CODE_TYPES: tuple[str, ...] = ("tanthu", "moibanbe", "event", "diemdanh", "eventchiase")

#: Trạng thái của một mã trong kho. `reserved` là trạng thái hệ cũ KHÔNG có.
CODE_STATUSES: tuple[str, ...] = ("available", "reserved", "issued", "revoked")

#: Loại phát trong SỔ CÁI. Khác tên với `CODE_TYPES` một cách CỐ Ý (07-db §7.2 nhóm 2):
#: nếu dùng chung chữ `event` cho cả hai thì một câu `WHERE grant_type='event'` gõ nhầm
#: vẫn chạy được và trả sai — loại lỗi không bao giờ lộ ra trong test.
GRANT_TYPES: tuple[str, ...] = (
    "tanthu",
    "referral_milestone",
    "event_box",
    "points_redeem",
    "share_event",
    "admin_manual",
)

#: Hai loại "một lần trọn đời" — và chỉ hai. Thêm một giá trị vào đây là **thay đổi luật
#: phát tiền**, phải là một migration có tên, có review (07-db §7.2 nhóm 2 điểm (a)).
ONCE_PER_LIFE_GRANT_TYPES: tuple[str, ...] = ("tanthu", "share_event")

#: Vòng đời một dòng sổ cái phát hành: reserved → delivered → (revoked).
GRANT_STATES: tuple[str, ...] = ("reserved", "delivered", "revoked")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    """Dựng mệnh đề ``col IN ('a','b')`` cho CheckConstraint / partial index."""
    joined = ",".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Code(Base):
    """KHO. Một dòng = một chuỗi mã chưa/đã phát. Không biết gì về người dùng.

    Bảng này cố ý **không** có `used_by` / `used_at` như hệ cũ: hai cột đó đã chuyển
    thành `code_grants.user_id` / `code_grants.created_at`. Ở đây chỉ còn câu hỏi tồn kho.
    """

    __tablename__ = "codes"

    code_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code_value: Mapped[str] = mapped_column(Text, nullable=False)

    # CHECK cứng thay cho TEXT tự do của hệ cũ: một lần gõ nhầm `'tanthu '` (thừa dấu
    # cách) là sinh một loại code mới không ai theo dõi, và tồn kho của loại thật hụt đi
    # mà không có cảnh báo nào.
    code_type: Mapped[str] = mapped_column(Text, nullable=False)

    value_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'available'"),
    )

    # Hai cột của mô hình hai pha. Cấp code = `available → reserved` (giữ chỗ 15 phút),
    # chỉ sau khi Telegram xác nhận đã gửi mới `reserved → issued`. Hết 15 phút mà chưa
    # gửi được thì job `reap_reservations` trả mã về kho — code KHÔNG bị đốt.
    reserved_for: Mapped[int | None] = user_fk_column(nullable=True)
    reserved_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Lô nạp của một lần `/add_giffcode`, để `/del_code` thu hồi trọn lô. Không có FK vì
    # đặc tả không định nghĩa bảng `batches`.
    batch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(_sql_in("code_type", CODE_TYPES), name="code_type_valid"),
        CheckConstraint(_sql_in("status", CODE_STATUSES), name="status_valid"),
        CheckConstraint("value_vnd > 0", name="value_vnd_positive"),
        Index("uq_codes_value", "code_value", unique=True),
        # Index quan trọng nhất của toàn hệ thống: đây là index của câu
        # `... WHERE status='available' ORDER BY code_id FOR UPDATE SKIP LOCKED LIMIT 1`.
        # Partial vì chỉ phần CHƯA phát mới cần tra cứu — index tự nhỏ đi khi kho vơi,
        # ở mốc 2 triệu code đã phát gần hết nó nặng vài MB thay vì vài trăm MB.
        Index(
            "ix_codes_pick",
            "code_type",
            "value_vnd",
            "code_id",
            postgresql_where=text("status = 'available'"),
        ),
        # Job `reap_reservations` chạy định kỳ để trả về kho những code đã giữ chỗ nhưng
        # tiến trình giữ nó chết giữa chừng. Không có index này thì job đó quét toàn bảng
        # `codes` mỗi lần chạy. Partial nên nó gần như rỗng lúc bình thường — chỉ phình
        # lên đúng bằng số code đang được giữ, rồi tự co lại.
        Index(
            "ix_codes_reap",
            "reserved_until",
            postgresql_where=text("status = 'reserved'"),
        ),
    )


class GrantType(Base):
    """Từ điển các luồng phát. ~6 dòng, seed một lần trong migration rồi quên.

    Tồn tại vì `code_grants.grant_type` trỏ FK vào đây, và vì `once_per_life` phải sửa
    được bằng lệnh admin chứ không phải bằng một Enum Python cứng (15 D4).
    """

    __tablename__ = "grant_types"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label_vi: Mapped[str] = mapped_column(Text, nullable=False)

    # Cờ mô tả: loại này có nằm trong `uq_grant_once_semantic` không. Hàng rào thật vẫn
    # là partial unique index dưới kia — cờ này để lệnh admin và báo cáo đọc, KHÔNG phải
    # để code tự suy ra rồi bỏ qua ràng buộc DB.
    once_per_life: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )


class CodeGrant(Base):
    """SỔ CÁI PHÁT HÀNH. Một dòng = một suất đã hứa với một người.

    Dòng sinh ra ở `state='reserved'` với `code_id IS NULL`, gắn mã sau, chuyển
    `delivered` khi Telegram xác nhận. Nhờ tách khỏi `codes`, gửi tin thất bại chỉ làm
    dòng này đứng lại ở `reserved` để thử lại — mã quay về kho, không ai mất gì.
    """

    __tablename__ = "code_grants"

    grant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Khoá tất định sinh từ chính hành động: `tanthu:{uid}`, `event:{event_id}:{uid}`…
    # TUYỆT ĐỐI không được chứa timestamp — key mới mỗi lần bấm là vô hiệu hoá toàn bộ
    # cơ chế idempotency (03-code.md §3.3).
    grant_key: Mapped[str] = mapped_column(Text, nullable=False)

    user_id: Mapped[int] = user_fk_column(nullable=False)

    grant_type: Mapped[str] = mapped_column(
        Text,
        ForeignKey("grant_types.code", ondelete="RESTRICT"),
        nullable=False,
    )

    # NULL khi grant vừa sinh ở `reserved`. Bất biến MỘT CHIỀU: đi từ NULL sang một giá
    # trị đúng một lần rồi khoá cứng (trigger `grants_immutable()`, 07-db điểm (b)).
    code_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("codes.code_id", ondelete="RESTRICT"),
        nullable=True,
    )

    value_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # KHÔNG cùng nghĩa với `codes.status`: một bên là sổ cái (ai được nhận), một bên là
    # kho (mã còn hay hết). Cố ý khác tên cột và khác tập giá trị để không trộn được.
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'reserved'"),
    )

    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    # `X-Request-Id` do Mini App gửi lên. CHỈ để tra lại phản hồi đã lưu, không bao giờ
    # dùng để cấp quyền — tin client ở đây là mở cửa cho kẻ tấn công tự sinh id mới
    # (quyết định #8).
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    reason: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(_sql_in("grant_type", GRANT_TYPES), name="grant_type_valid"),
        CheckConstraint(_sql_in("state", GRANT_STATES), name="state_valid"),
        CheckConstraint("value_vnd > 0", name="value_vnd_positive"),
        # (1) Chống lặp KỸ THUẬT: cùng một yêu cầu gửi lại nhiều lần → đúng một grant.
        # Đường xử lý là `INSERT ... ON CONFLICT (grant_key) DO NOTHING` rồi đọc lại
        # dòng cũ và TRẢ VỀ ĐÚNG CODE CŨ — người dùng bấm lại vì chưa thấy tin nhắn.
        Index("uq_grants_key", "grant_key", unique=True),
        # (2) Một mã chỉ thuộc về tối đa MỘT grant. Partial vì nhiều dòng `code_id IS
        # NULL` (đang reserved) là hợp lệ.
        Index(
            "uq_grants_code",
            "code_id",
            unique=True,
            postgresql_where=text("code_id IS NOT NULL"),
        ),
        # (3) Hàng rào một-lần-trọn-đời THEO NGỮ NGHĨA. Đây là ràng buộc mà hệ cũ không
        # có: 226 user đã ôm 544 code tân thủ thừa (≈ 5,44 triệu VNĐ). Partial vì
        # `referral_milestone` / `event_box` / `points_redeem` / `admin_manual` được lặp
        # lại hợp lệ và bị chặn bởi cơ chế khác (trần mốc, PK sự kiện, số dư điểm).
        Index(
            "uq_grant_once_semantic",
            "user_id",
            "grant_type",
            unique=True,
            postgresql_where=text(_sql_in("grant_type", ONCE_PER_LIFE_GRANT_TYPES)),
        ),
        # `/checkip`: số code và tổng giá trị của từng tài khoản cùng IP. Hai con số này
        # đọc từ đây, TUYỆT ĐỐI không đọc `users.total_codes_received` (bộ nhớ đệm).
        Index("ix_grants_user", "user_id", text("created_at DESC")),
        # `/codes used`: 20 grant gần nhất toàn cục. Thiếu index là sort toàn bảng.
        Index("ix_grants_recent", text("created_at DESC")),
    )


class IdempotencyRecord(Base):
    """Nhớ phản hồi 24 giờ cho bài toán "Mini App gửi lại request do mạng 4G chập chờn".

    Khác `grant_key` ở chỗ nó chặn **lần gửi lại của cùng một request**, còn `grant_key`
    chặn **người bấm nhiều lần**. Hai bài toán khác nhau, phải có cả hai (03-code §3.4).
    """

    __tablename__ = "idempotency_records"

    # `sha256(scope || user_id || business_key)`, trong đó `user_id` lấy từ `initData` đã
    # giải mã — không bao giờ lấy từ JSON body (quyết định #8).
    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)

    user_id: Mapped[int] = user_fk_column(nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL trong khoảng giữa "đã nhận yêu cầu" và "đã có kết quả để trả".
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Scheduler dọn bằng `DELETE ... WHERE expires_at < now()`. Không có index này
        # thì bảng phình vô hạn vì lệnh dọn tự nó là full-scan.
        Index("ix_idem_gc", "expires_at"),
    )


class CodePoolStat(UpdatedAtMixin, Base):
    """Tồn kho đếm sẵn theo `(code_type, value_vnd)`, do trigger trên `codes` duy trì.

    Cảnh báo hết code chạy mỗi 60 giây; `COUNT(*)` trên bảng 2 triệu dòng mỗi phút là
    lãng phí thuần tuý. Ngưỡng cảnh báo KHÔNG nằm ở đây mà ở `settings.alert.
    low_code_threshold` (13 §13.6) — bảng này chỉ giữ số đếm.
    """

    __tablename__ = "code_pool_stats"

    code_type: Mapped[str] = mapped_column(Text, primary_key=True)
    value_vnd: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    available: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    issued: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(_sql_in("code_type", CODE_TYPES), name="code_type_valid"),
        CheckConstraint("value_vnd > 0", name="value_vnd_positive"),
        # Số đếm âm nghĩa là trigger đã sai. Chặn ở tầng DB để lỗi lộ ra ngay lần trừ
        # thứ nhất, không phải sau vài nghìn lượt phát khi tồn kho đã vô nghĩa.
        CheckConstraint("available >= 0", name="available_non_negative"),
        CheckConstraint("issued >= 0", name="issued_non_negative"),
    )


class CodeLedgerEntry(Base):
    """SỔ CÁI TIỀN. Chỉ ghi thêm — không sửa, không xoá, kể cả bằng tài khoản ứng dụng.

    Đối soát tài chính = ``SELECT SUM(value_vnd * direction) FROM code_ledger``. Thu hồi
    code = ghi **bút toán ngược** (`direction = -1`, `ref_entry_id` trỏ về bút toán gốc),
    tuyệt đối không sửa dòng cũ.

    Hệ cũ không ghi log cho `mark_code_used`, `redeem_points_for_code` và phát code
    milestone — mất vài chục triệu là không truy được nguyên nhân.

    Hai thứ **không** biểu diễn được ở tầng ORM và phải đặt trong migration:
    ``REVOKE UPDATE, DELETE ON code_ledger FROM televip_app`` và job `verify_chain`
    chạy hằng đêm.
    """

    __tablename__ = "code_ledger"

    entry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # `entry_hash = sha256(prev_hash ‖ canonical_json(entry))` — mỗi dòng ký lên dòng
    # trước. Sửa lén một dòng ở giữa là chuỗi đứt và `verify_chain` phát hiện ngay.
    # `prev_hash` NULL đúng một lần, ở dòng đầu tiên của chuỗi.
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    entry_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    user_id: Mapped[int] = user_fk_column(nullable=False)
    grant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("code_grants.grant_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # NULL khi bút toán được ghi lúc grant còn ở `reserved`, chưa gắn mã.
    code_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("codes.code_id", ondelete="RESTRICT"),
        nullable=True,
    )

    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    value_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # Bút toán ngược trỏ về bút toán gốc. NULL ở bút toán thuận.
    ref_entry_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("code_ledger.entry_id", ondelete="RESTRICT"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("direction IN (1, -1)", name="direction_valid"),
        # Dấu nằm ở `direction`, độ lớn luôn dương. Cho phép `value_vnd` âm là mở đường
        # cho một bút toán vừa âm dấu vừa âm giá trị — cộng lại thành dương.
        CheckConstraint("value_vnd > 0", name="value_vnd_positive"),
    )
