"""Nhóm 6-7-8: nhóm/kênh bắt buộc · gửi tin · vận hành.

Ba nhóm nằm chung một file vì chúng tạo thành một chuỗi nhân quả duy nhất:
biết ai đang ở trong nhóm (6) → quyết định gửi gì cho ai (7) → ghi lại ai đã làm
gì và hệ thống đang ra sao (8).

Nguồn sự thật về cột/ràng buộc/index: `docs/ke-hoach-v2/07-db.md` §7.2, nhóm 6-8;
riêng `settings` / `settings_audit` lấy nguyên DDL của `13-dac-ta-bot-moi.md` §13.6.1.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from televip.db.base import Base, TimestampedMixin, UpdatedAtMixin, user_fk_column

# ══════════════════════════════════════════════════════════════════════════════
# Nhóm 6 — Nhóm/kênh bắt buộc
# ══════════════════════════════════════════════════════════════════════════════


class RequiredChat(Base):
    """Danh sách nhóm/kênh bắt buộc — thay việc parse `.env` mỗi lượt kiểm tra.

    Hệ cũ gọi `load_required_groups()` **bên trong vòng lặp user**: log thật in dòng
    "Loaded 2 required groups from .env" ~30 lần trong đúng 1 giây. Ở 300k user một
    lượt broadcast là 300k lần đọc file.
    """

    __tablename__ = "required_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str | None] = mapped_column(Text)
    # NOT NULL: không có link mời thì màn hình "vào nhóm để nhận code" là ngõ cụt.
    invite_link: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Ba giá trị chứ không phải BOOLEAN `bot_is_admin`: một boolean không phân biệt được
    # "đã kiểm, bot là admin" với "chưa kiểm bao giờ" — mà đó đúng là chế độ hỏng âm thầm
    # nguy hiểm nhất ở đây. Hệ cũ có một kênh bắt buộc chết mà không ai biết.
    health: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unknown'"))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "health IN ('healthy','degraded','unknown')",
            name="health",
        ),
    )


class GroupMembership(Base, UpdatedAtMixin):
    """Ảnh chụp tư cách thành viên — bảng xoá sổ N+1 `getChatMember` của hệ cũ.

    Log thật: 90,4% lưu lượng API là lời gọi này. Một truy vấn chỉ số ~0,2ms thay cho
    2 lời gọi HTTPS mất 1-5 giây. Nguồn cập nhật chính là update `chat_member`
    (realtime, 0 lời gọi API), nên `allowed_updates` khi `setWebhook` **phải liệt kê
    tường minh `chat_member`** — Telegram mặc định KHÔNG gửi loại update này.

    Cố ý KHÔNG có FK tới `users`: sự kiện `chat_member` có thể đến trước khi người đó
    từng bấm /start, và một FK ở đây sẽ biến mọi sự kiện lạ thành lỗi ghi.
    """

    __tablename__ = "group_memberships"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("required_chats.chat_id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Cột dẫn xuất của `status`, giữ sẵn để partial index bên dưới nhỏ và truy vấn
    # "đã vào đủ nhóm chưa" không phải liệt kê lại 3 giá trị mỗi lần.
    is_member: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Luật cache bất đối xứng của 05-groups.md ở đây: NULL = tin tới khi có sự kiện mới;
    # có giá trị = chỉ tin tới mốc đó (chat đang `degraded` thì chỉ tin 10 phút).
    # Không có cột này thì lời hứa "đợi 15 giây rồi bấm lại" in trên màn hình user
    # không có chỗ nào để lưu.
    trust_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('creator','administrator','member','restricted','left','kicked')",
            name="status",
        ),
        # 'event' ≡ update `chat_member`; 'poll' ≡ `getChatMember` gọi tay;
        # 'backfill' ≡ nạp bù những ngày đầu sau cutover (ngày đầu là 100% cache miss).
        CheckConstraint("source IN ('event','poll','backfill')", name="source"),
        Index("ix_gm_chat", "chat_id", "status"),
        Index("ix_gm_user_member", "user_id", postgresql_where=text("is_member")),
    )


class MembershipEvent(Base):
    """Nhật ký đổi tư cách thành viên, append-only — **chỉ để đo, không để chặn ai**.

    Thu hồi code khi rời nhóm là hành vi MỚI và §F cấm. Chỉ số cần đo trong 4 tuần
    shadow: tỉ lệ rời nhóm trong 24 giờ / 7 ngày sau khi nhận code tân thủ, tách theo
    cụm ASN và theo referrer. Ước tính ~6.000 dòng/ngày (≈ 0,07 sự kiện/giây).
    """

    __tablename__ = "membership_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL khi đây là lần đầu thấy người này ở chat đó.
    old_status: Mapped[str | None] = mapped_column(Text)
    new_status: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Phục vụ đúng câu hỏi đo lường ở trên: "người này rời nhóm lúc nào so với
        # lúc nhận code", tra theo từng user chứ không quét toàn bảng.
        Index("ix_membership_events_user_at", "user_id", "at"),
    )


class MembershipMismatch(Base):
    """Đối chiếu shadow: kết quả đọc bảng vs kết quả `getChatMember` thật.

    Bảng sống tạm cho GĐ4 — tiêu chí bật cache là **khớp ≥ 99,9%**. TTL 30 ngày,
    dọn bằng `DELETE ... WHERE checked_at < ...` nên `checked_at` phải có index.
    """

    __tablename__ = "membership_mismatch"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ket_qua_bang: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ket_qua_api: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_membership_mismatch_checked_at", "checked_at"),)


# ══════════════════════════════════════════════════════════════════════════════
# Nhóm 7 — Gửi tin
# ══════════════════════════════════════════════════════════════════════════════


class OutboxMessage(Base, TimestampedMixin):
    """Hàng đợi tin nhắn cá nhân (trả code, kết quả đập hộp).

    Dòng outbox được ghi **trong cùng transaction** với việc cấp code: giữ code + ghi
    grant + ghi ledger + ghi outbox → COMMIT, xong mới thử gửi nhanh. Nếu tiến trình
    chết đúng lúc đó, dòng này vẫn nằm đấy và worker khác nhặt lên gửi. Hệ cũ commit
    rồi mới `await send_message` (`handlers/admin.py:1683` → `:1711`); batch timeout
    15s huỷ task ⇒ code bị đốt, user không nhận được gì.
    """

    __tablename__ = "outbox_messages"

    outbox_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Khoá chống gửi trùng, dạng 'msg:' || grant_key (03-code.md §3.4). Ghi bằng
    # INSERT ... ON CONFLICT (dedupe_key) DO NOTHING: kể cả khi ba lớp idempotency
    # phía trên đều thủng, người dùng vẫn chỉ nhận đúng một tin.
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # 0 pending · 1 sent · 2 failed
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    # Mốc "được phép thử lại từ lúc này" — backoff ghi vào đây, không ngủ trong tiến trình.
    visible_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Partial index: chỉ đánh mục lục cho việc CHƯA LÀM. Nhờ nó bảng chứa được
        # 5 triệu dòng lịch sử mà việc lấy 100 dòng kế tiếp vẫn tốn <1ms.
        Index("ix_outbox_claim", "visible_at", postgresql_where=text("state = 0")),
    )


class BroadcastJob(Base, TimestampedMixin):
    """Một lượt bắn tin. Nội dung lưu **một lần** ở đây, không lặp lại 300k lần."""

    __tablename__ = "broadcast_jobs"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind IN ('broadcast','send_event','resend_tanthu')", name="kind"),
        CheckConstraint("audience IN ('active_30d','all','custom')", name="audience"),
        CheckConstraint("state IN ('draft','running','paused','done','cancelled')", name="state"),
    )


class BroadcastTarget(Base):
    """Bảng đông dòng nhất hệ thống — ba việc trên cùng một dòng.

    (1) hàng đợi gửi tạm dừng / tiếp tục / resume được sau restart;
    (2) `sent_at` là gốc tính cửa sổ event 10 phút **theo từng người nhận** — không
        phải một mốc chung cho cả job;
    (3) `tg_message_id` + `deleted_at` cho job xoá tin sau 24 giờ, thay hẳn bảng
        `event_messages` cũ (không tạo thêm bảng `sent_event_messages`: hai nguồn thì
        job janitor phải quét hai chỗ).

    PK tổ hợp `(job_id, user_id)` là thứ chống gửi trùng, do DB tự bảo đảm.
    Khi Telegram trả `RetryAfter(n)`: target quay về `state = 0` và `attempts` KHÔNG
    tăng. Hệ cũ để `RetryAfter` rơi vào `except Exception: failed += 1` và bỏ vĩnh
    viễn người đó.
    """

    __tablename__ = "broadcast_targets"

    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("broadcast_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # 0 pending · 1 sent · 2 failed · 3 skipped
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mã lỗi HTTP của Telegram, tách khỏi `last_error` (chuỗi tự do, không lọc được):
    # 403 = user đã chặn bot ⇒ bỏ vĩnh viễn và đánh dấu `users.blocked_at`;
    # 429 / 5xx = lỗi tạm thời ⇒ phải thử lại. Hệ cũ bắt 403 rồi vứt (`admin.py:514-518`,
    # không có cột nào ghi) nên sau chiến dịch vẫn không biết tệp thật còn bao nhiêu người.
    error_code: Mapped[int | None] = mapped_column(SmallInteger)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Worker lấy việc: chỉ chứa dòng CÒN VIỆC. Job xong 90% thì index chỉ còn ~10%.
        Index("ix_bt_claim", "job_id", "next_attempt_at", postgresql_where=text("state = 0")),
        # Janitor xoá tin sau 24 giờ: chỉ những tin đã gửi mà chưa xoá.
        Index(
            "ix_bt_janitor",
            "sent_at",
            postgresql_where=text("deleted_at IS NULL AND tg_message_id IS NOT NULL"),
        ),
    )


class MediaAsset(Base):
    """Ảnh/media đã upload, khoá theo `asset_key`.

    Thay dict RAM `cached_photos` của hệ cũ — mất sạch mỗi lần restart, nên mỗi lần
    khởi động lại là một đợt upload lại toàn bộ. `sha256` để phát hiện file trên đĩa
    đã đổi: khác hash thì tự upload lại và thay `file_id`.
    """

    __tablename__ = "media_assets"

    asset_key: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# Nhóm 8 — Vận hành
# ══════════════════════════════════════════════════════════════════════════════


class AuditLog(Base, TimestampedMixin):
    """Nhật ký hành động, thay `admin_logs`.

    Hệ cũ chỉ có `admin_id / action / details` với `details` là chuỗi tự do kiểu
    'Sent to N users': không có đối tượng bị tác động, không có trước/sau, không index
    ⇒ có khiếu nại là không điều tra được. `before`/`after` giữ nguyên trạng thái hai
    phía để trả lời "ai đổi cái gì thành cái gì".
    """

    __tablename__ = "audit_log"

    log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    # `user_id` Telegram của người gõ lệnh — khớp với `admin_users.user_id`. NULL khi
    # actor_type = 'system'. Từ chối quyền cũng ghi vào đây (bot cũ im lặng).
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("actor_type IN ('admin','system')", name="actor_type"),
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
    )


class AdminUser(Base):
    """**Nguồn sự thật DUY NHẤT của phân quyền.**

    `ADMIN_GROUP_ID` CHỈ là nơi nhận cảnh báo; nó không cấp quyền và không được xuất
    hiện trong bất kỳ nhánh `if` nào của decorator kiểm quyền. Hệ cũ dùng
    `if chat_id == ADMIN_GROUP_ID: return True` — không kiểm người gửi, nên ai lọt vào
    nhóm là có `/add_giffcode` và `/broadcast`. 147.990.000đ đã đi qua cánh cửa đó.

    Thu hồi quyền là **đặt `revoked_at`**, không `DELETE` — mất dòng là mất bằng chứng.
    """

    __tablename__ = "admin_users"

    user_id: Mapped[int] = user_fk_column(primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: NULL = đang có hiệu lực.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','cskh','ketoan')", name="role"),
        Index("ix_admin_users_active", "role", postgresql_where=text("revoked_at IS NULL")),
    )


class AdminPermission(Base):
    """Bản đồ vai trò → lệnh, quyền theo **từng lệnh**.

    Nằm trong DB chứ không phải dict trong code: chỉ khi đó `/help_admin` mới sinh
    được từ registry và `/admin_add` mới đổi được quyền mà không phải deploy.
    """

    __tablename__ = "admin_permissions"

    role: Mapped[str] = mapped_column(Text, primary_key=True)
    #: '/add_giffcode', '/broadcast', …
    command: Mapped[str] = mapped_column(Text, primary_key=True)

    __table_args__ = (CheckConstraint("role IN ('owner','admin','cskh','ketoan')", name="role"),)


class Setting(Base, UpdatedAtMixin):
    """**Nơi ở của mọi con số nghiệp vụ** (nguyên tắc N2: không con số nào nằm trong code).

    Hệ cũ phải `nano config.py` trên VPS rồi restart để đổi một mệnh giá. Đọc: cache
    trong tiến trình TTL 60 giây, nạp toàn bảng (vài chục dòng) trong một truy vấn.

    ⚠️ Bản trước của ghi chú này viết "ghi xong phát `NOTIFY settings_changed` để mọi
    worker nạp lại ngay". **Không có cơ chế đó** — không `LISTEN/NOTIFY`, không kênh Redis.
    Mỗi tiến trình tự nạp lại khi bản sao của CHÍNH NÓ quá 60 giây, nên sàn lan truyền là
    60 giây và không rút ngắn được từ xa. Xem `services/settings_service.py`.
    Ghi: chỉ qua `/setcauhinh`, validate `value_type` + `min/max`, luôn ghi `settings_audit`.

    Cấu hình **nhiều dòng** không nhét vào đây mà giữ bảng riêng (`required_chats`,
    `campaigns`, `asn_policy`, `share_event_config`, `grant_types`).
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict | list | str | int | float | bool] = mapped_column(JSONB, nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: Nhãn hiện trong `/cauhinh`.
    label_vi: Mapped[str] = mapped_column(Text, nullable=False)
    # Chặn nhập nhầm: `/setcauhinh` không cho vượt khoảng này. NUMERIC chứ không phải
    # BIGINT vì đây là biên của mọi loại giá trị (điểm cơ bản, giây, tiền), không riêng tiền.
    min_value: Mapped[Decimal | None] = mapped_column(Numeric)
    max_value: Mapped[Decimal | None] = mapped_column(Numeric)
    #: true = phải có người thứ hai bấm xác nhận trong 10 phút mới đổi được.
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_by: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint(
            "value_type IN ('int','money_vnd','bp','seconds','bool','string','json')",
            name="value_type",
        ),
    )


class SettingsAudit(Base):
    """Lịch sử đổi cấu hình, **append-only** — không UPDATE, không DELETE.

    Ở cấp Postgres phải `REVOKE UPDATE, DELETE ON settings_audit FROM televip_app`,
    cùng nguyên tắc với `code_ledger`.
    """

    __tablename__ = "settings_audit"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL khi đây là lần đặt giá trị đầu tiên.
    old_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    new_value: Mapped[dict | list | str | int | float | bool] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Người duyệt thứ hai, bắt buộc khi `settings.sensitive = true`.
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_settings_audit_key_at", "key", "changed_at"),)


class MessageTemplate(Base, UpdatedAtMixin):
    """Câu chữ gửi cho người dùng, sửa được bằng lệnh admin — không phải bằng deploy.

    Cùng nguyên tắc với `settings` nhưng cho VĂN BẢN thay vì con số: `domain/texts.py` giữ
    bản **mặc định** (và là lưới an toàn khi bảng này mất một dòng), còn bảng này giữ bản
    admin đang chạy. Đọc qua `services/text_service.py` với cache 60 giây.

    `required_vars` là danh sách biến bắt buộc của khoá (`["code_value", "value_vnd"]`).
    Nó tồn tại để `set_content` **từ chối** một nội dung mới đánh rơi `{code_value}`: nếu
    không có hàng rào đó, tin trả code vẫn gửi đi nhưng không có mã trong đó, và không ai
    biết cho tới khi người dùng khiếu nại.
    """

    __tablename__ = "message_templates"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Nhãn hiện trong `/noidung`.
    label_vi: Mapped[str] = mapped_column(Text, nullable=False)
    required_vars: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class MessageTemplateAudit(Base):
    """Lịch sử sửa câu chữ, **append-only** — đường quay lui khi ai đó sửa hỏng.

    Giữ cả `old_content` lẫn `new_content` vì câu hỏi cần trả lời không phải "nội dung bây
    giờ là gì" (đọc `message_templates` là biết) mà "trước khi hỏng thì nó là gì": khôi
    phục bằng `/suanoidung` cần chính chuỗi cũ, không cần một dòng mô tả về nó.
    """

    __tablename__ = "message_templates_audit"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL khi đây là lần đặt nội dung đầu tiên cho khoá.
    old_content: Mapped[str | None] = mapped_column(Text)
    new_content: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_message_templates_audit_key_at", "key", "changed_at"),)


class SystemStats(Base, UpdatedAtMixin):
    """**Đúng một dòng** (`id = 1`), scheduler làm mới mỗi 30 giây.

    Nút "📊Thống Kê TK" của hệ cũ chạy 5 câu `COUNT(*)` / `SUM` full-scan **mỗi lần
    bấm**; sau broadcast là hàng nghìn lượt bấm/phút — tự DDoS chính mình.
    """

    __tablename__ = "system_stats"

    # CHECK id = 1 là thứ biến bảng này thành một ô nhớ: không ai INSERT được dòng thứ
    # hai, nên đọc luôn là `WHERE id = 1` và không bao giờ mơ hồ đọc trúng dòng nào.
    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False, server_default=text("1")
    )
    total_users: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    codes_available: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    codes_issued: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_referrals: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_value_vnd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class UserStats(Base, UpdatedAtMixin):
    """Tổng hợp theo từng user, cập nhật bằng UPSERT khi có sự kiện.

    Hệ cũ mỗi lần bấm "👑 BXH hôm nay" là JOIN toàn bảng `users` với `referrals`,
    GROUP BY toàn bộ, ORDER BY rồi mới `LIMIT 3`. Ba index DESC dưới đây là ba khối
    bảng xếp hạng, mỗi khối đọc đúng vài dòng đầu.
    """

    __tablename__ = "user_stats"

    user_id: Mapped[int] = user_fk_column(primary_key=True)
    refs_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Số lượt mời đã đủ điều kiện tính thưởng (đã xác minh, không bị chặn bởi risk).
    refs_qualified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    value_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    checkin_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("ix_user_stats_refs_total", text("refs_total DESC")),
        Index("ix_user_stats_value_total", text("value_total DESC")),
        Index("ix_user_stats_checkin_streak", text("checkin_streak DESC")),
    )
