"""Nhóm 3 (mời bạn bè), nhóm 4 (điểm danh & điểm), nhóm 5 (event đập hộp).

Chín bảng ở đây có một điểm chung: chúng là những chỗ hệ cũ **phát trùng tiền**.
Vì vậy luật "một lần" của từng luồng được đặt xuống tầng database bằng khoá chính
tổ hợp hoặc ràng buộc `UNIQUE`, chứ không nằm trong một câu `if` của Python —
lần thứ hai va vào khoá và bị Postgres từ chối, dù code ứng dụng có lỗi gì đi nữa.

Nguồn sự thật: `docs/ke-hoach-v2/07-db.md` §7.2 nhóm 3–5 và §7.2.1 (tên chuẩn).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from televip.db.base import Base, TimestampedMixin, UpdatedAtMixin, user_fk_column

# ─────────────────────────────────────────────────────────────────────────────
# Nhóm 3 — Mời bạn bè
# ─────────────────────────────────────────────────────────────────────────────


class ReferralIntent(Base, TimestampedMixin):
    """Ý định giới thiệu đang chờ, thay cột `users.pending_referrer_id` của hệ cũ.

    Hệ cũ đọc cột đó rồi mới `UPDATE ... SET pending_referrer_id = NULL` ở hai
    bước tách rời, nên hai request `/verify` song song của hai worker cùng đọc
    được giá trị và **ghi 2 dòng referral cho một người**. Ở đây việc tiêu thụ là
    một câu lệnh nguyên tử:

        DELETE FROM referral_intents WHERE referee_id = $1 RETURNING referrer_id

    Worker thua cuộc nhận về 0 dòng và không ghi gì cả.
    """

    __tablename__ = "referral_intents"

    #: Không đặt FK sang `users`: ý định được ghi từ deep-link `/start?ref=…`,
    #: thời điểm mà cả người được mời lẫn mã người mời trong link đều có thể chưa
    #: (hoặc không bao giờ) tồn tại. FK ở đây sẽ biến một link rác thành lỗi 500.
    #: `autoincrement=False` là bắt buộc: đây là `user_id` Telegram do người gọi
    #: đưa vào, không phải số tự sinh — thiếu nó SQLAlchemy dựng cột thành
    #: `BIGSERIAL` vì thấy một khoá chính số duy nhất.
    referee_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    referrer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Referral(Base, TimestampedMixin):
    """Quan hệ giới thiệu đã chốt — mỗi người có đúng MỘT người mời, vĩnh viễn."""

    __tablename__ = "referrals"

    #: PK là `referee_id`, không phải cột tự tăng: đây chính là ràng buộc
    #: "một người chỉ được giới thiệu một lần". Ghi bằng
    #: `INSERT ... ON CONFLICT (referee_id) DO NOTHING` rồi đọc `rowcount` để
    #: quyết định có tăng số đếm hay không. Hệ cũ để `referral_id AUTOINCREMENT`
    #: không UNIQUE gì cả nên một người bị đếm nhiều lần.
    referee_id: Mapped[int] = user_fk_column(primary_key=True)
    referrer_id: Mapped[int] = user_fk_column(nullable=False)

    #: NULL = đã ghi nhận nhưng chưa đủ điều kiện (chưa xác minh xong, hoặc
    #: `risk_score` vượt `referral.max_risk_score`). Chỉ dòng có `qualified_at`
    #: mới được tính vào mốc thưởng.
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Ảnh chụp tín hiệu rủi ro tại đúng thời điểm chốt quan hệ. Lịch sử đầy đủ
    #: nằm ở `identity_signals` / `verification_events`; ba cột này để điều tra
    #: một dòng referral cụ thể mà không phải join ngược thời gian.
    ip_hash: Mapped[str | None] = mapped_column(Text)
    device_hash: Mapped[str | None] = mapped_column(Text)
    risk_score: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (
        #: Chống tự mời chính mình ở tầng DB. Hệ cũ chỉ có một dòng
        #: `if ref_id != user_id` trong Python — mọi đường ghi khác đều lọt.
        CheckConstraint("referrer_id <> referee_id", name="no_self_referral"),
        CheckConstraint(
            "risk_score IS NULL OR risk_score BETWEEN 0 AND 100",
            name="risk_score_range",
        ),
        #: Partial index: câu hỏi duy nhất hệ thống hỏi bảng này là "người X đã
        #: mời được bao nhiêu người ĐẠT CHUẨN", nên không index dòng chưa đạt.
        Index(
            "ix_referrals_referrer",
            "referrer_id",
            postgresql_where=text("qualified_at IS NOT NULL"),
        ),
    )


class ReferralReward(Base, TimestampedMixin):
    """Mốc thưởng mời bạn đã phát cho một người.

    Khoá chính tổ hợp `(user_id, tier_no)` là hàng rào chặn phát trùng mốc:
    logic phát tính `tier_dat_duoc = LEAST(qualified / interval, max_claims)` rồi
    phát bù **mọi tier chưa có bản ghi** (dùng `>=`, không dùng `% 5 == 0`).
    Hệ cũ dùng phép chia dư nên khi số đếm nhảy 4→6 (do referral bị đếm đôi),
    người mời **không bao giờ chạm mốc 5 và mất code vĩnh viễn**.
    """

    __tablename__ = "referral_rewards"

    user_id: Mapped[int] = user_fk_column(primary_key=True)
    tier_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    #: NULL trong khoảnh khắc giữa lúc chốt mốc và lúc grant tương ứng được tạo;
    #: chốt mốc trước là cách để hai worker song song không cùng phát một mốc.
    code_grant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("code_grants.grant_id", ondelete="RESTRICT"),
    )

    __table_args__ = (
        #: Trần 10 mốc khớp với `referral.max_claims`; vượt ngưỡng là lỗi logic,
        #: không phải cấu hình hợp lệ.
        CheckConstraint("tier_no BETWEEN 1 AND 10", name="tier_no_range"),
    )


class Campaign(Base):
    """Tham số chiến dịch mời bạn — dữ liệu, không phải hằng số trong code.

    Hệ cũ để interval / mệnh giá / số lần tối đa là hằng số Python trong
    `config.py`: muốn đổi phải `nano` trên VPS rồi restart bot, mà restart thì
    mất sạch trạng thái RAM. Ở đây admin đổi bằng `/chiendich`, đọc qua cache 60s.

    `starts_at` / `ends_at` **để trống lúc seed** — nạp sẵn một chiến dịch đã hết
    hạn vào hệ chưa mở là tự gieo lỗi. Trạng thái `chưa mở / đang chạy /
    đã kết thúc` suy từ ba cột `starts_at`, `ends_at`, `is_active`, và luồng phát
    thưởng chỉ phát khi `đang chạy` (hết hạn = ngừng phát, không chỉ ngừng hiển thị).
    """

    __tablename__ = "campaigns"

    campaign_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    interval_people: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_value_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_claims: Mapped[int] = mapped_column(Integer, nullable=False)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        #: Mẫu số của `qualified / interval_people` — bằng 0 là lỗi chia cho 0
        #: ngay giữa luồng phát thưởng.
        CheckConstraint("interval_people > 0", name="interval_people_positive"),
        CheckConstraint("reward_value_vnd > 0", name="reward_value_positive"),
        #: Trần 10 phải khớp `ck_referral_rewards_tier_no_range`; đặt
        #: `max_claims = 12` bằng lệnh admin sẽ làm mốc 11 và 12 bị DB từ chối
        #: giữa chừng — chặn ngay tại chỗ cấu hình thay vì lúc phát.
        CheckConstraint("max_claims BETWEEN 1 AND 10", name="max_claims_range"),
        #: `/chiendich extend` không được lùi hạn về trước ngày bắt đầu.
        CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
            name="window_order",
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Nhóm 4 — Điểm danh & điểm
# ─────────────────────────────────────────────────────────────────────────────


class Checkin(Base, TimestampedMixin):
    """Một lượt điểm danh. Bảng mới, hệ cũ không có.

    Khoá chính tổ hợp `(user_id, business_date)` **chính là** ràng buộc
    "một ngày một lần". Hệ cũ kiểm bằng `if last_checkin_date == today` trong
    Python rồi UPDATE bằng giá trị tuyệt đối → bấm nhanh hai lần là cộng 4.000đ.
    """

    __tablename__ = "checkins"

    user_id: Mapped[int] = user_fk_column(primary_key=True)

    #: Ngày NGHIỆP VỤ theo giờ Việt Nam — `(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`,
    #: không phải giờ máy chủ. Hệ cũ dùng `datetime.now().date()` naive nên người
    #: điểm danh lúc 23h30 bị tính sang hôm sau và đứt streak oan. Trong Python
    #: chỉ được lấy giá trị này qua `core/clock.py: business_date()`.
    business_date: Mapped[date] = mapped_column(Date, primary_key=True)

    #: Giá trị thật lấy từ `settings['checkin.points_per_day']`; DEFAULT ở đây
    #: chỉ là lưới an toàn cho câu INSERT không truyền cột.
    points_delta: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("2000")
    )
    streak_after: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("points_delta > 0", name="points_delta_positive"),
        CheckConstraint("streak_after >= 1", name="streak_after_positive"),
    )


class PointsLedgerEntry(Base, TimestampedMixin):
    """Sổ cái điểm — chỉ ghi thêm, không sửa không xoá.

    Số dư là tổng của sổ cái. `users.points_balance` chỉ là bản tổng hợp để hiển
    thị và phải được cập nhật bằng ghi **tương đối có điều kiện**
    (`SET points_balance = points_balance - $1 WHERE points_balance >= $1`),
    không bao giờ bằng giá trị tuyệt đối: hệ cũ đọc số dư rồi ghi đè, nên hai
    coroutine cùng đọc 20.000, mỗi bên trừ 10.000 và cùng ghi 10.000 → user lấy
    2 code mà chỉ mất 10.000 điểm.
    """

    __tablename__ = "points_ledger"

    entry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = user_fk_column()

    #: Dương = cộng, âm = trừ. Đảo bút toán là ghi thêm dòng `reversal`, tuyệt
    #: đối không sửa dòng cũ.
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    #: Khoá chống ghi trùng, tất định theo nghiệp vụ (ví dụ ngày điểm danh, hoặc
    #: `grant_key` của lượt đổi điểm). **NOT NULL là cố ý**: trong Postgres hai
    #: giá trị NULL là khác nhau, nên một `ref_key` cho phép NULL sẽ khiến
    #: `UNIQUE` bên dưới im lặng không chặn gì — đúng cái lỗ hổng bảng này sinh ra
    #: để bịt.
    ref_key: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "reason IN ('checkin', 'redeem', 'admin_adjust', 'reversal')",
            name="reason_valid",
        ),
        #: Vừa là hàng rào idempotency, vừa là index phục vụ việc cộng số dư từ
        #: sổ cái (`user_id` là cột dẫn đầu) — không cần index riêng cho `user_id`.
        UniqueConstraint(
            "user_id",
            "reason",
            "ref_key",
            name="uq_points_ledger_user_id_reason_ref_key",
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Nhóm 5 — Event
# ─────────────────────────────────────────────────────────────────────────────


class Event(Base, TimestampedMixin):
    """Một đợt event đập hộp.

    ⚠️ `created_at` ở đây **KHÔNG phải** gốc tính cửa sổ 10 phút. Hệ cũ tính cửa
    sổ từ đúng cột này — một mốc toàn cục — trong khi giao 300k tin mất khoảng
    3 giờ, nên chỉ ~5,6% người nhận đầu tiên còn trong cửa sổ và **~94% bị ép
    `empty` mà không ai biết**. Ở hệ mới cửa sổ tính theo `sent_at` của TỪNG
    người nhận trong `broadcast_targets`:

        now() <= COALESCE(bt.sent_at, e.created_at) + interval '10 minutes'

    `created_at` chỉ còn là mốc dự phòng khi không có `sent_at`, và mỗi lượt tham
    gia ghi lại mình đã dùng gốc nào qua `event_participations.window_source`.
    """

    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    #: `user_id` Telegram của admin chạy lệnh. Không đặt FK — cùng quy ước với
    #: `codes.created_by` và `admin_users.added_by`.
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Ảnh của event lấy từ kho `media_assets` thay vì dict RAM `cached_photos`
    #: của hệ cũ (mất sạch mỗi lần restart).
    media_asset_key: Mapped[str | None] = mapped_column(
        Text, ForeignKey("media_assets.asset_key", ondelete="RESTRICT")
    )
    caption: Mapped[str | None] = mapped_column(Text)

    #: Đợt broadcast đã phát event này; NULL cho tới khi job được tạo.
    job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("broadcast_jobs.job_id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("event_type IN ('dap_hop')", name="event_type_valid"),
        #: Mỗi lượt bấm "đập hộp" phải tìm event đang mở; partial index để không
        #: quét các đợt đã đóng.
        Index(
            "ix_events_active",
            "created_at",
            postgresql_where=text("is_active"),
        ),
    )


class EventParticipation(Base):
    """Một lượt mở hộp của một người trong một event.

    Khoá chính tổ hợp `(user_id, event_id)` chặn mở hộp hai lần ngay tại DB.
    """

    __tablename__ = "event_participations"

    user_id: Mapped[int] = user_fk_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("events.event_id", ondelete="RESTRICT"),
        primary_key=True,
    )

    result: Mapped[str] = mapped_column(Text, nullable=False)

    #: Chỉ có giá trị khi trúng; `empty` và `out_of_stock` không sinh grant nào.
    code_grant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("code_grants.grant_id", ondelete="RESTRICT")
    )

    #: Gốc thời gian đã dùng để tính cửa sổ 10 phút cho ĐÚNG lượt này:
    #: `per_recipient` = `broadcast_targets.sent_at` (đường đúng),
    #: `event_created` = `events.created_at` (đường dự phòng, hành vi hệ cũ).
    #: Ghi lại để đo được thay đổi này làm chi phí code phát ra tăng bao nhiêu —
    #: con số chủ bot phải thấy trước khi bật.
    window_source: Mapped[str] = mapped_column(Text, nullable=False)

    participated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "result IN ('empty', '5k', '10k', '20k', '50k', '88k', 'out_of_stock')",
            name="result_valid",
        ),
        CheckConstraint(
            "window_source IN ('per_recipient', 'event_created')",
            name="window_source_valid",
        ),
        #: Trần chi mỗi đợt (`event.budget_cap_vnd`) phải cộng theo từng event;
        #: khoá chính dẫn đầu bằng `user_id` nên không phục vụ được truy vấn đó.
        Index("ix_event_participations_event", "event_id", "result"),
    )


class ShareEventConfig(Base, UpdatedAtMixin):
    """Cấu hình event chia sẻ — đúng MỘT dòng, giữ nguyên thiết kế của hệ cũ.

    Luật "mỗi người nhận event chia sẻ đúng 1 lần trọn đời" **không** nằm ở đây
    mà ở `uq_grant_once_semantic` với `grant_type = 'share_event'`, cùng chỗ với
    mọi luật một-lần khác (bảng `event_share_claims` cũ vì thế biến mất).

    Tên bảng là `share_event_config`, không phải `share_event`: tên cũ trùng đúng
    từng ký tự với giá trị `grant_type='share_event'`, hai thứ khác hẳn nhau đứng
    cạnh nhau trong cùng một câu SQL là công thức gây nhầm lúc 3h sáng.
    """

    __tablename__ = "share_event_config"

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False, server_default=text("1")
    )

    image_file_id: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    share_link: Mapped[str | None] = mapped_column(Text)

    #: NULL ở dòng seed mặc định — chưa admin nào sửa. Không FK, cùng quy ước với
    #: `events.created_by`.
    updated_by: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        #: Chặn cứng dòng thứ hai: cấu hình một-dòng mà có hai dòng thì mọi
        #: `SELECT ... LIMIT 1` trở thành xổ số.
        CheckConstraint("id = 1", name="singleton"),
    )
