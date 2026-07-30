"""Nhóm 1 — Người dùng & định danh (10 bảng).

Nguồn sự thật: `docs/ke-hoach-v2/07-db.md` §7.2 (nhóm 1) và §7.2.1 (tên chuẩn).

Ba điều chi phối toàn bộ file này:

1. **Không có counter tự cộng trên `users`.** Hệ cũ giữ `total_codes_received` /
   `total_value_received` ngay trên hàng user rồi `+= 1` mỗi lượt phát; hai cột đó lệch
   khỏi sổ cái và không bao giờ đối soát lại được. Số tổng của một user phải derive từ
   `code_ledger`, bản tổng hợp nằm ở `user_stats` (nhóm 8).
2. **Xoá user là hành vi bị cấm** (07-db §F). `user_bans` thay hoàn toàn cho
   `DELETE FROM users` — mọi FK trong file này vì thế dùng `ondelete=RESTRICT`.
3. **Bốn bảng chống gian lận tạo rỗng và ĐỂ RỖNG tới GĐ5** (`signal_owners`,
   `identity_clusters`, `cluster_stats`, `risk_assessments`, `fraud_cases`). Tạo sẵn ở
   `0001_initial` để không phải migration giữa chừng; ràng buộc phải đúng ngay từ bây giờ
   vì sửa `CHECK` trên bảng đã có dữ liệu bẩn thì phải dọn dữ liệu trước.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from televip.db.base import Base, TimestampedMixin, UpdatedAtMixin, user_fk_column

#: Danh sách loại tín hiệu, dùng chung cho `identity_signals` và `signal_owners`. Hai
#: bảng là hai chiều của CÙNG một tập dữ liệu: nếu danh sách giá trị lệch nhau thì trigger
#: ghi lọt vào bảng này nhưng bị CHECK chặn ở bảng kia — hỏng im lặng giữa đường nóng.
_SIGNAL_TYPE_CK = "signal_type IN ('ip', 'ip24', 'asn', 'device_hash', 'ua_hash', 'device_raw')"


class User(Base):
    """Hồ sơ người dùng. PK là `user_id` của Telegram, KHÔNG tự sinh.

    Cố ý KHÔNG có `updated_at`: hàng này bị `last_active` chạm tới mỗi phút, nên một cột
    `updated_at` chung sẽ chỉ lặp lại `last_active` mà không thêm thông tin nào.
    """

    __tablename__ = "users"

    # autoincrement=False: đây là ID do Telegram cấp, không phải BIGSERIAL. Thiếu cờ này
    # SQLAlchemy sinh ra một sequence và Postgres sẽ ghi đè ID thật bằng số đếm nội bộ.
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Thời ĐIỂM verify, không phải cờ boolean: điều tra gian lận cần biết "verify lúc nào"
    # (vận tốc đăng ký của một cụm), thứ mà `is_verified BOOLEAN` vứt bỏ. Điều kiện
    # idempotent của luồng `/api/verify` viết thành `WHERE verified_at IS NULL`.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # CHECK >= 0 là thứ chặn cứng lỗi in tiền. Mọi thay đổi số dư phải là phép tương đối
    # có điều kiện (`SET x = x - $1 WHERE x >= $1`), không bao giờ ghi giá trị tuyệt đối
    # đọc trước đó — hai request song song đọc cùng số dư là cách hệ cũ phát trùng.
    points_balance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    checkin_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Ngày nghiệp vụ giờ VN, tính bằng `core/clock.py: business_date()`. Lưu DATE riêng
    # thay vì so sánh TIMESTAMPTZ: máy chạy UTC còn user sống ở UTC+7, so trực tiếp làm
    # "ngày mới" của điểm danh nhảy lúc 07:00 sáng giờ Việt Nam.
    last_checkin_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # User đã chặn bot (Telegram trả 403). Loại vĩnh viễn khỏi mọi đợt broadcast — cứ gửi
    # tiếp cho người đã chặn là tín hiệu xấu nhất có thể gửi tới hệ chống spam.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Lệnh `/thongbao`. Mọi `audience` luôn loại `notify_optout = true`.
    notify_optout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # Lần đầu user `/start` với bot MỚI. Không phải bộ lọc sở thích mà là điều kiện VẬT LÝ
    # của broadcast: chưa từng /start thì bot không có quyền nhắn, gửi đi chỉ sinh lỗi 403.
    started_bot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    risk_score: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))

    # ── Bộ nhớ đệm hiển thị ──────────────────────────────────────────
    # Hai cột dưới đây KHÔNG phải nguồn sự thật. Nguồn sự thật là `code_grants` +
    # `code_ledger`; chúng chỉ tồn tại để màn hình Thống Kê TK không phải chạy
    # SUM() trên sổ cái mỗi lần user bấm nút.
    #
    # Luật bắt buộc: chỉ được cập nhật trong CÙNG transaction với dòng `code_grants`
    # tương ứng, bằng phép cộng tương đối (`SET x = x + $1`). Hệ cũ để hai con số này
    # tự cộng ở một transaction riêng nên chúng trôi khỏi thực tế — đo được 2 user lệch.
    # Job đối soát đêm so lại với sổ cái và báo động nếu lệch.
    total_codes_received: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_value_received: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Ghi theo lô (gom qua Redis, scheduler UPDATE mỗi 60 giây), KHÔNG ghi thẳng mỗi lượt
    # tương tác: ở 30-60 update/giây thì ghi thẳng chỉ để đụng một cột làm phình WAL.
    last_active: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("points_balance >= 0", name="points_balance_non_negative"),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="risk_score_range"),
        CheckConstraint("total_codes_received >= 0", name="total_codes_non_negative"),
        CheckConstraint("total_value_received >= 0", name="total_value_non_negative"),
        # `/done_event @user` và `/resend_tanthu @user` tra theo username. Username của
        # Telegram không phân biệt hoa thường nên khoá duy nhất phải đặt trên lower().
        Index(
            "uq_users_username_lower",
            text("lower(username)"),
            unique=True,
            postgresql_where=text("username IS NOT NULL"),
        ),
        # Index phục vụ `audience='active_30d'`: biến việc chọn đích broadcast từ quét
        # toàn bảng thành đọc đúng phần cần. Ba điều kiện trong WHERE là ba điều kiện
        # đứng TRƯỚC mọi bộ lọc audience trong câu materialize.
        Index(
            "ix_users_audience",
            text("last_active DESC"),
            postgresql_where=text(
                "blocked_at IS NULL AND notify_optout = false AND started_bot_at IS NOT NULL"
            ),
        ),
    )


class UserBan(Base):
    """Thay hoàn toàn cho `DELETE FROM users`.

    Xoá một hàng `users` làm thủng sổ cái (`code_ledger` còn bút toán trỏ tới user không
    còn tồn tại) và xoá luôn bằng chứng điều tra. Ban là một hàng ở đây; gỡ ban là điền
    `unbanned_at`, không phải xoá hàng.
    """

    __tablename__ = "user_bans"

    user_id: Mapped[int] = user_fk_column(primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # user_id Telegram của admin bấm lệnh. Không FK sang `admin_users`: một admin bị thu
    # hồi quyền vẫn phải còn tên trong hồ sơ ban cũ.
    banned_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    banned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # NULL = ban còn hiệu lực. Đây là định nghĩa của "ban đang active" mà câu materialize
    # của broadcast dùng trong `NOT EXISTS`.
    unbanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Hồ sơ điều tra dẫn tới lệnh ban. NULL khi owner ban tay bằng `/ban`.
    case_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("fraud_cases.case_id", ondelete="RESTRICT"),
        nullable=True,
    )


class IdentitySignal(Base):
    """Thay bảng `user_ip_log` cũ — giữ TOÀN BỘ lịch sử tín hiệu định danh.

    Hệ cũ ghi đè `users.ip_address` mỗi lượt verify, nên mất sạch lịch sử: có khiếu nại
    là không tra được người đó từng vào từ đâu. PK tổ hợp ở đây làm việc lặp lại trở
    thành UPSERT tăng `hits`, không sinh dòng mới.
    """

    __tablename__ = "identity_signals"

    user_id: Mapped[int] = user_fk_column(primary_key=True)
    signal_type: Mapped[str] = mapped_column(Text, primary_key=True)
    signal_value: Mapped[str] = mapped_column(Text, primary_key=True)

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint(_SIGNAL_TYPE_CK, name="signal_type_allowed"),
        CheckConstraint("hits > 0", name="hits_positive"),
        # Chiều NGƯỢC của PK: PK trả lời "user này có tín hiệu gì", index này trả lời
        # "IP/thiết bị này có bao nhiêu tài khoản" cho lệnh `/checkip`.
        Index("ix_signals_reverse", "signal_type", "signal_value"),
    )


class SignalOwner(Base):
    """Bảng chiều ngược đã tổng hợp sẵn của `identity_signals`, do trigger duy trì.

    Lý do tồn tại là hiệu năng trên ĐƯỜNG NÓNG: mỗi lượt `/api/verify` cần biết "IP này
    đang có mấy tài khoản" để áp luật `asn_policy`. Đếm trực tiếp là `COUNT` trên hàng
    triệu dòng ngay giữa request; đọc ở đây là một lượt tra PK.
    """

    __tablename__ = "signal_owners"

    signal_type: Mapped[str] = mapped_column(Text, primary_key=True)
    signal_value: Mapped[str] = mapped_column(Text, primary_key=True)

    user_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_user_id: Mapped[int | None] = user_fk_column(nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(_SIGNAL_TYPE_CK, name="signal_type_allowed"),
        # Counter do trigger duy trì. Hệ cũ chết vì counter tự cộng trôi khỏi sự thật;
        # ràng buộc này biến một trigger giảm sai thành lỗi nổ ngay thay vì âm thầm.
        CheckConstraint("user_count >= 0", name="user_count_non_negative"),
    )


class VerificationEvent(Base, TimestampedMixin):
    """Nhật ký từng lượt gọi `/api/verify`.

    Hệ cũ KHÔNG lưu lý do từ chối ở bất kỳ đâu — có khiếu nại là không điều tra được, và
    không có cách nào đo xem một luật chống gian lận đang chặn oan bao nhiêu người thật.
    Bốn tuần dữ liệu của bảng này là điều kiện để chuyển `risk.mode` sang `'enforce'`.

    Chính sách lưu trữ: đây là NƠI DUY NHẤT giữ IP đầy đủ, và chỉ giữ 90 ngày (sau đó thu
    về /24 rồi xoá cột IP). `created_at` của mixin có index để job dọn theo hạn.
    """

    __tablename__ = "verification_events"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = user_fk_column()
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    # Kiểu `inet`/`cidr` của Postgres, KHÔNG phải TEXT: chỉ có chúng mới truy vấn được
    # theo dải (`WHERE ip << '14.160.0.0/16'`) — thao tác cốt lõi khi lần theo một cụm.
    # NULL được phép vì tra GeoIP là fail-open: hỏng cơ sở dữ liệu geo thì cho user qua,
    # không chặn người thật vì lỗi hạ tầng của mình.
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    ip_24: Mapped[str | None] = mapped_column(CIDR, nullable=True)
    asn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    country: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)

    # Chỉ lưu HASH của vân tay thiết bị, không bao giờ lưu giá trị thô — đủ để so khớp và
    # gom cụm, không phục hồi ngược được thành đặc điểm máy của người dùng.
    device_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    ua_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Kết quả kiểm chữ ký HMAC của `initData`. Bản thân `initData` thô KHÔNG BAO GIỜ được
    # ghi vào đây: nó là một chữ ký hợp lệ dùng lại được trong 300 giây.
    initdata_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)

    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    risk_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # Tên luật đã kích hoạt. Đây chính là cột trả lời được câu "vì sao người này bị từ
    # chối" sáu tháng sau.
    reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    __table_args__ = (
        CheckConstraint("event_type IN ('verify', 'recheck')", name="event_type_allowed"),
        CheckConstraint("verdict IN ('pass', 'flag', 'block')", name="verdict_allowed"),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="risk_score_range"),
        # Đường tra cứu của CSKH khi có khiếu nại và của `/user`: toàn bộ lượt verify của
        # một người, mới nhất trước.
        Index("ix_verification_events_user_time", "user_id", text("created_at DESC")),
    )


class IdentityCluster(Base):
    """Kết quả của cluster builder (union-find, chạy mỗi 5 phút).

    Nối hai tài khoản khi chúng chung vân tay thiết bị, hoặc cùng dải /24 với vân tay gần
    giống nhau. TẠO RỖNG ở `0001_initial` và để rỗng tới GĐ5.
    """

    __tablename__ = "identity_clusters"

    cluster_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = user_fk_column(primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # PK đi theo chiều cluster → user; index này là chiều user → cluster, dùng khi so
        # "người mời và người được mời có cùng cụm không".
        Index("ix_clusters_user", "user_id"),
    )


class ClusterStats(Base, UpdatedAtMixin):
    """Bản tổng hợp theo cụm — đầu vào của `fraud_cases`.

    Câu hỏi phát hiện gian lận đắt nhất là "cụm nào nhiều người mà ít thiết bị"; hỏi
    trực tiếp là GROUP BY toàn bộ `identity_clusters` mỗi lần. Bảng này trả lời tức thì.
    """

    __tablename__ = "cluster_stats"

    cluster_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    n_users: Mapped[int] = mapped_column(Integer, nullable=False)
    n_verified: Mapped[int] = mapped_column(Integer, nullable=False)
    n_devices: Mapped[int] = mapped_column(Integer, nullable=False)

    # BIGINT, đơn vị VNĐ nguyên. Là tổng cộng dồn nên cho phép bằng 0 (cụm chưa nhận gì);
    # ràng buộc `> 0` của cột tiền giao dịch không áp dụng cho cột tổng hợp.
    total_value_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Bảng tổng hợp do job ghi lại mỗi 5 phút; số âm ở đây nghĩa là job tính sai, và
        # nó là đầu vào của nút "Ban cả cụm" nên sai số phải nổ chứ không được lặng.
        CheckConstraint(
            "n_users >= 0 AND n_verified >= 0 AND n_devices >= 0 AND total_value_vnd >= 0",
            name="counters_non_negative",
        ),
    )


class RiskAssessment(Base, TimestampedMixin):
    """Ghi bất biến mỗi lần chấm điểm rủi ro, để sáu tháng sau còn giải thích được vì sao.

    `mode` chỉ có HAI giá trị. Giá trị `'strict_ip'` đã bị loại khỏi thiết kế: nó là tên
    gọi của luật "1 IP = 1 tài khoản" trong hệ cũ, luật đã chặn oan 898 người mà 0/898
    trong số đó từng verify thành công. Tư thế ngày đầu là `'shadow'` — chấm điểm, ghi
    log, KHÔNG chặn ai; chỉ chuyển `'enforce'` sau ≥ 4 tuần shadow và dương tính giả < 2%.
    """

    __tablename__ = "risk_assessments"

    assessment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = user_fk_column()
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # CHỈ tên luật đã kích hoạt và số đã rút gọn — không chứa giá trị thô (IP, UA).
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    mode: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("mode IN ('shadow', 'enforce')", name="mode_allowed"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        Index("ix_risk_assessments_user_time", "user_id", text("created_at DESC")),
    )


class AsnPolicy(Base):
    """Ngưỡng chống gian lận theo LỚP MẠNG, thay cho luật "1 IP = 1 tài khoản" cứng.

    Luật cứng của hệ cũ chặn oan 898 người thật vì nó không phân biệt được CGNAT của nhà
    mạng di động — nơi hàng nghìn thuê bao thật dùng chung một IP công cộng — với một máy
    chủ thuê ở datacenter. Bảng này cho mỗi lớp một ngưỡng riêng:
    mobile_cgnat 25/IP/24h · residential 6/IP/7 ngày · hosting 1 · vpn 0.

    Bảng "seed rồi quên": nạp một lần trong migration, không có repository.
    """

    __tablename__ = "asn_policy"

    asn: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    network_class: Mapped[str] = mapped_column(Text, nullable=False)

    # 0 nghĩa là chặn hẳn lớp mạng đó (lớp `vpn`), không phải "không giới hạn".
    max_accounts: Mapped[int] = mapped_column(Integer, nullable=False)
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "network_class IN ('mobile_cgnat', 'residential', 'hosting', 'vpn')",
            name="network_class_allowed",
        ),
        CheckConstraint("max_accounts >= 0", name="max_accounts_non_negative"),
        CheckConstraint("window_hours > 0", name="window_hours_positive"),
    )


class FraudCase(Base, TimestampedMixin):
    """Hồ sơ một phát hiện gian lận. KHÔNG luật nào tự động ban ai.

    Quyết định #15: không có dry-run in ra CHÍNH XÁC số VNĐ và số user bị tác động thì
    không được bấm nút "Ban cả cụm" — một luật sai không có dry-run sẽ ban nhầm hàng
    nghìn người thật trong một lệnh. Ràng buộc `executed_requires_dry_run` bên dưới đưa
    đúng quy tắc đó xuống tầng database, chỗ không ai lỡ tay bỏ qua được.
    """

    __tablename__ = "fraud_cases"

    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Khoá định danh cụm bị nghi ngờ (cluster_id, hoặc tín hiệu chung đã sinh ra hồ sơ).
    cluster_key: Mapped[str] = mapped_column(Text, nullable=False)

    # Hai con số mà dry-run bắt buộc phải in ra trước khi cho thực thi.
    user_count: Mapped[int] = mapped_column(Integer, nullable=False)
    value_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    dry_run_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    executed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'dry_run', 'executed', 'dismissed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "user_count >= 0 AND value_vnd >= 0",
            name="impact_non_negative",
        ),
        # Quyết định #15 viết thành ràng buộc: một hồ sơ chỉ được sang trạng thái
        # 'executed' khi đã có báo cáo dry-run, người bấm và thời điểm bấm.
        CheckConstraint(
            "status <> 'executed'"
            " OR (dry_run_report IS NOT NULL"
            " AND executed_by IS NOT NULL"
            " AND executed_at IS NOT NULL)",
            name="executed_requires_dry_run",
        ),
        # Hàng đợi việc phải xử lý tay. Partial index vì chỉ hồ sơ chưa đóng mới được
        # truy vấn — index co lại khi hồ sơ được giải quyết.
        Index(
            "ix_fraud_cases_open",
            text("created_at DESC"),
            postgresql_where=text("status IN ('open', 'dry_run')"),
        ),
    )
