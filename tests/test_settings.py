"""Cấu hình nghiệp vụ: dữ liệu seed, đọc có kiểu, và đường ghi có sổ kiểm toán.

Test chạy trên **chính hằng số của migration `0002_seed_config`**, không chép lại con số
nào sang đây. `conftest` TRUNCATE mọi bảng trước từng test nên dữ liệu do `alembic upgrade`
nạp không còn; fixture dưới đây nạp lại đúng bộ đó. Hệ quả là nếu ai sửa seed mà làm sai
kiểu hay sai tổng trọng số, test này đỏ — chứ không phải phát hiện lúc bot đã chạy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.errors import ConfigError
from televip.db.models.codes import GRANT_TYPES, ONCE_PER_LIFE_GRANT_TYPES
from televip.db.models.delivery import Setting, SettingsAudit
from televip.services import settings_service


def _load_migration() -> Any:
    """Nạp file migration như một module thường.

    Tên file bắt đầu bằng chữ số nên không `import` trực tiếp được; đi đường `importlib`
    để test đọc được **đúng** hằng số mà production sẽ nạp, thay vì một bản sao.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "televip"
        / "db"
        / "migrations"
        / "versions"
        / "0002_seed_config.py"
    )
    spec = importlib.util.spec_from_file_location("televip_migration_0002", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()


@pytest_asyncio.fixture
async def config_db(db: AsyncSession):
    """Database có đúng bộ cấu hình seed, và cache của service đã nạp nó."""
    await db.execute(insert(Setting), [dict(row) for row in MIGRATION.SETTINGS_SEED])
    await db.commit()
    await settings_service.refresh(db=db)
    yield db
    # Cache là biến toàn cục của module: không dọn thì test sau đọc phải dữ liệu test trước.
    settings_service.invalidate()


# ── Dữ liệu seed ────────────────────────────────────────────────────


def test_seed_khong_co_khoa_trung() -> None:
    keys = [row["key"] for row in MIGRATION.SETTINGS_SEED]
    assert len(keys) == len(set(keys))
    # 45 khoá của §13.6.2-13.6.8, đúng như danh sách nhóm ở `07-db.md` §7.2.
    assert len(keys) == 45


def test_grant_types_seed_khop_hang_so_cua_model() -> None:
    """Seed và `CHECK` của `code_grants.grant_type` phải là cùng một tập.

    Đây là cổng mà `07-db.md` §7.2 nhóm 2 điểm (d) yêu cầu: thêm một loại phát mà quên
    seed từ điển thì đỏ ở đây, chứ không im lặng tới lúc phát code.
    """
    seeded = tuple(row["code"] for row in MIGRATION.GRANT_TYPES_SEED)
    assert set(seeded) == set(GRANT_TYPES)

    once = {row["code"] for row in MIGRATION.GRANT_TYPES_SEED if row["once_per_life"]}
    assert once == set(ONCE_PER_LIFE_GRANT_TYPES)


def test_prize_table_tong_trong_so_dung_10000() -> None:
    """Tổng trọng số phải đúng 10.000 basis point — nếu không, vòng quay lệch âm thầm."""
    table = next(
        row["value"] for row in MIGRATION.SETTINGS_SEED if row["key"] == "event.prize_table"
    )
    assert sum(tier["weight_bp"] for tier in table) == 10_000
    assert len(table) == 6
    # Ba mệnh giá cao PHẢI có trọng số khác 0: hệ cũ quảng cáo chúng với xác suất trúng
    # bằng 0, và đó là lỗi nặng nhất trong danh sách bài học (§13.5 dòng E).
    high = {tier["value_vnd"]: tier["weight_bp"] for tier in table}
    assert high[20_000] > 0 and high[50_000] > 0 and high[88_000] > 0


async def test_prize_table_doc_qua_service_van_dung_tong(config_db: AsyncSession) -> None:
    table = await settings_service.get_list("event.prize_table", db=config_db)
    assert sum(tier["weight_bp"] for tier in table) == 10_000


# ── Đọc có kiểu ─────────────────────────────────────────────────────


async def test_doc_khoa_da_seed_ra_dung_kieu(config_db: AsyncSession) -> None:
    get_int = settings_service.get_int
    assert await get_int("code.tanthu_value_vnd", db=config_db) == 10_000
    assert await get_int("referral.interval", db=config_db) == 5
    assert await get_int("referral.max_claims", db=config_db) == 10
    assert await get_int("referral.reward_value_vnd", db=config_db) == 10_000
    assert await get_int("event.budget_cap_vnd", db=config_db) == 12_000_000
    assert await get_int("event.window_minutes", db=config_db) == 10
    assert await get_int("cooldown.checkin", db=config_db) == 7

    assert await settings_service.get_bool("referral.require_verified", db=config_db) is True
    assert await settings_service.get_bool("verify.require_username", db=config_db) is False
    assert await settings_service.get_str("risk.mode", db=config_db) == "shadow"
    assert await settings_service.get_float("broadcast.rate_per_sec", db=config_db) == 30.0
    assert await settings_service.get_list("redeem.tiers", db=config_db) == [10_000, 20_000, 50_000]

    category = await settings_service.get_dict("code.category_values", db=config_db)
    assert category["event"] == [5_000, 10_000, 20_000, 50_000, 88_000]


async def test_get_int_tren_khoa_kieu_chuoi_nem_config_error(config_db: AsyncSession) -> None:
    with pytest.raises(ConfigError):
        await settings_service.get_int("risk.mode", db=config_db)


async def test_get_int_khong_nhan_bool(config_db: AsyncSession) -> None:
    """`bool` là lớp con của `int`: không chặn thì `true` lọt qua thành 1."""
    with pytest.raises(ConfigError):
        await settings_service.get_int("referral.require_verified", db=config_db)


async def test_get_dict_tren_khoa_mang_nem_config_error(config_db: AsyncSession) -> None:
    with pytest.raises(ConfigError):
        await settings_service.get_dict("redeem.tiers", db=config_db)


async def test_thieu_khoa_khong_co_default_thi_nem(config_db: AsyncSession) -> None:
    with pytest.raises(ConfigError):
        await settings_service.get_int("khong.ton.tai", db=config_db)

    assert await settings_service.get_int("khong.ton.tai", 7, db=config_db) == 7
    assert await settings_service.get("khong.ton.tai", db=config_db) is None


# ── Ghi ─────────────────────────────────────────────────────────────


async def test_set_ghi_gia_tri_va_mot_dong_audit(config_db: AsyncSession) -> None:
    await settings_service.set("code.tanthu_value_vnd", 20_000, updated_by=777, db=config_db)
    await config_db.commit()

    row = (
        await config_db.execute(select(Setting).where(Setting.key == "code.tanthu_value_vnd"))
    ).scalar_one()
    assert row.value == 20_000
    assert row.updated_by == 777

    audit = (
        (
            await config_db.execute(
                select(SettingsAudit).where(SettingsAudit.key == "code.tanthu_value_vnd")
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1
    assert audit[0].old_value == 10_000
    assert audit[0].new_value == 20_000
    assert audit[0].changed_by == 777
    assert audit[0].approved_by is None

    # Ghi xong là cache của chính tiến trình này hết hạn — không phải chờ hết 60 giây.
    assert await settings_service.get_int("code.tanthu_value_vnd", db=config_db) == 20_000


async def test_set_ghi_nguoi_duyet_thu_hai(config_db: AsyncSession) -> None:
    await settings_service.set(
        "risk.block_threshold", 90, updated_by=1, approved_by=2, db=config_db
    )
    await config_db.commit()

    audit = (
        await config_db.execute(
            select(SettingsAudit).where(SettingsAudit.key == "risk.block_threshold")
        )
    ).scalar_one()
    assert (audit.changed_by, audit.approved_by) == (1, 2)


async def test_set_tu_choi_khoa_khong_ton_tai(config_db: AsyncSession) -> None:
    with pytest.raises(ConfigError):
        await settings_service.set("khong.ton.tai", 1, updated_by=1, db=config_db)


async def test_set_tu_choi_gia_tri_sai_kieu(config_db: AsyncSession) -> None:
    """Chặn ngay lúc ghi, vì nếu lọt thì MỌI lượt đọc sau đó ném lỗi ở chỗ khác hẳn."""
    with pytest.raises(ConfigError):
        await settings_service.set(
            "event.budget_cap_vnd", "mười hai triệu", updated_by=1, db=config_db
        )
    await config_db.rollback()

    with pytest.raises(ConfigError):
        await settings_service.set("referral.require_verified", 1, updated_by=1, db=config_db)


async def test_set_tu_choi_gia_tri_ngoai_khoang(config_db: AsyncSession) -> None:
    with pytest.raises(ConfigError):
        await settings_service.set("risk.block_threshold", 500, updated_by=1, db=config_db)
    await config_db.rollback()

    with pytest.raises(ConfigError):
        await settings_service.set("cooldown.start", -1, updated_by=1, db=config_db)


async def test_set_khong_ghi_audit_khi_bi_tu_choi(config_db: AsyncSession) -> None:
    with pytest.raises(ConfigError):
        await settings_service.set("risk.block_threshold", 500, updated_by=1, db=config_db)
    await config_db.rollback()

    count = (await config_db.execute(select(SettingsAudit))).scalars().all()
    assert count == []


# ── Cache ───────────────────────────────────────────────────────────


async def test_cache_giu_gia_tri_cu_cho_toi_khi_refresh(config_db: AsyncSession) -> None:
    """Bản chất của độ trễ ≤ 60 giây, viết thành test để nó là hành vi có chủ ý.

    Ghi thẳng vào bảng (mô phỏng một worker KHÁC vừa đổi cấu hình) thì tiến trình này vẫn
    đọc giá trị cũ cho tới lượt nạp lại — đó là cái giá đã chấp nhận để không phải dựng
    pub/sub. Worker tự ghi bằng `set()` thì thấy ngay, vì `set()` xoá cache của nó.
    """
    assert await settings_service.get_int("cooldown.start", db=config_db) == 3

    await config_db.execute(update(Setting).where(Setting.key == "cooldown.start").values(value=99))
    await config_db.commit()

    assert await settings_service.get_int("cooldown.start", db=config_db) == 3

    await settings_service.refresh(db=config_db)
    assert await settings_service.get_int("cooldown.start", db=config_db) == 99


# ── Hàng rào duyệt hai người ────────────────────────────────────────
#
# Mười khoá `sensitive` đang che những con số đắt nhất hệ thống: `event.prize_table` (xác
# suất trúng của tiền thật), `code.tanthu_value_vnd` (mệnh giá × 19.151 người),
# `event.budget_cap_vnd`, và `admin.dual_approval_threshold_vnd` — chính cái ngưỡng bật/tắt
# mọi luật duyệt hai người còn lại.
#
# Cho tới gần đây, câu từ chối duy nhất nằm trong handler Telegram. `settings_service.set()`
# *ghi lại* `approved_by` vào sổ nhưng **không bao giờ đòi** nó. Đứng yên được suốt thời
# gian chỉ có một tầng gọi. Panel web là tầng gọi thứ hai.
#
# Ba bài dưới đây gọi THẲNG service, không đi qua handler nào — đó là điểm của chúng.


async def test_khoa_sensitive_khong_doi_duoc_neu_thieu_nguoi_duyet(
    config_db: AsyncSession,
) -> None:
    await config_db.execute(
        update(Setting).where(Setting.key == "code.tanthu_value_vnd").values(sensitive=True)
    )
    await config_db.commit()
    settings_service.invalidate()

    truoc = await settings_service.get_int("code.tanthu_value_vnd", db=config_db)

    with pytest.raises(settings_service.SensitiveSettingLocked) as loi:
        await settings_service.set(
            "code.tanthu_value_vnd", truoc + 90_000, updated_by=1, db=config_db
        )
    assert loi.value.key == "code.tanthu_value_vnd"

    # Và giá trị KHÔNG đổi — một lời từ chối có ghi log mà vẫn ghi vào bảng thì vô nghĩa.
    settings_service.invalidate()
    assert await settings_service.get_int("code.tanthu_value_vnd", db=config_db) == truoc


async def test_khoa_sensitive_doi_duoc_khi_co_nguoi_duyet(config_db: AsyncSession) -> None:
    """Hàng rào phải MỞ được, nếu không nó chỉ là một khoá chết."""
    await config_db.execute(
        update(Setting).where(Setting.key == "code.tanthu_value_vnd").values(sensitive=True)
    )
    await config_db.commit()

    await settings_service.set(
        "code.tanthu_value_vnd", 25_000, updated_by=111, approved_by=222, db=config_db
    )
    await config_db.commit()
    settings_service.invalidate()

    assert await settings_service.get_int("code.tanthu_value_vnd", db=config_db) == 25_000
    # Chữ ký người duyệt phải vào sổ, nếu không thì "đã duyệt" là một lời nói suông.
    dong = (
        (
            await config_db.execute(
                select(SettingsAudit)
                .where(SettingsAudit.key == "code.tanthu_value_vnd")
                .order_by(SettingsAudit.audit_id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert dong is not None
    assert dong.approved_by == 222
    assert dong.changed_by == 111


async def test_tu_choi_sensitive_TRUOC_khi_kiem_kieu(config_db: AsyncSession) -> None:
    """Lý do từ chối phải là "cần duyệt hai người", không phải "sai kiểu".

    Gõ lại cho đúng kiểu rồi vẫn bị chặn là một vòng lặp không dẫn tới đâu, và người vận
    hành sẽ kết luận panel hỏng.
    """
    await config_db.execute(
        update(Setting).where(Setting.key == "code.tanthu_value_vnd").values(sensitive=True)
    )
    await config_db.commit()

    with pytest.raises(settings_service.SensitiveSettingLocked):
        # Giá trị SAI KIỂU (chuỗi cho khoá money_vnd) — vẫn phải ra lỗi sensitive.
        await settings_service.set(
            "code.tanthu_value_vnd", "mười nghìn", updated_by=1, db=config_db
        )


async def test_moi_khoa_sensitive_trong_seed_deu_bi_hang_rao_che(
    config_db: AsyncSession,
) -> None:
    """Quét TOÀN BỘ khoá `sensitive` đang seed, không chỉ một khoá mẫu.

    Bài kiểm một-khoá-mẫu sẽ vẫn xanh nếu ai đó thêm một khoá tiền mới mà quên bật cờ, hay
    nếu hàng rào chỉ chặn đúng khoá được chọn làm mẫu.
    """
    khoa = list(
        (await config_db.execute(select(Setting.key).where(Setting.sensitive))).scalars().all()
    )
    assert khoa, "seed phải có ít nhất một khoá sensitive — nếu không, hàng rào này vô nghĩa"

    for k in khoa:
        with pytest.raises(settings_service.SensitiveSettingLocked):
            # Giá trị không quan trọng: hàng rào chặn TRƯỚC khi tới bước kiểm kiểu.
            await settings_service.set(k, 1, updated_by=1, db=config_db)


# ── Che khoá bí mật ─────────────────────────────────────────────────


async def test_khoa_khop_mau_bi_mat_thi_gia_tri_KHONG_roi_service(
    config_db: AsyncSession,
) -> None:
    """Bảng `settings` hôm nay chưa có khoá nào khớp mẫu — đó chính là lý do phải kiểm.

    Hàng rào này là hàng rào cho NGÀY MAI: nó tồn tại để một khoá `*.token` thêm vào sau
    này không lặng lẽ hiện nguyên văn. Một hàng rào chưa từng chạy là một hàng rào chưa
    biết có chạy hay không.

    Và trên web nó rộng hơn trên Telegram: HTML nằm trong cache trình duyệt, trong lịch sử,
    trong ảnh chụp màn hình.
    """
    await config_db.execute(
        insert(Setting).values(
            key="thanhtoan.api_token",
            value="bi-mat-that-su",
            value_type="string",
            label_vi="Token cổng thanh toán",
        )
    )
    await config_db.commit()

    dong = await settings_service.get_setting(config_db, "thanhtoan.api_token")
    assert dong is not None
    assert dong.bi_che is True
    assert dong.value is None, "giá trị bí mật KHÔNG được rời khỏi service"

    # Và nó vẫn nằm trong danh sách — che chứ không giấu. Giấu hẳn thì người vận hành
    # không biết khoá đó tồn tại và sẽ đi tìm nó ở chỗ khác.
    tat_ca = await settings_service.list_settings(config_db)
    assert "thanhtoan.api_token" in {d.key for d in tat_ca}
    assert all(d.value is None for d in tat_ca if d.bi_che)


async def test_mau_bi_mat_khop_theo_chuoi_con_khong_phan_biet_hoa_thuong() -> None:
    for key in ("x.api_key", "A.TOKEN", "db.PASSWORD", "hmac_pepper", "auth.private_key"):
        assert settings_service.is_secret(key), key
    for key in ("code.tanthu_value_vnd", "event.prize_table", "leaderboard.top_n"):
        assert not settings_service.is_secret(key), key
