"""Ảnh do quản trị tải lên — phòng chờ giữa panel web và Telegram.

Tiến trình web **không có `telegram.Bot`**, nên nó không tải ảnh lên được. Nó ghi bytes vào
`media_uploads`; một job trong tiến trình bot nhặt lên, gửi một lần vào nhóm admin, lấy
`file_id`, rồi ghi vào `media_assets`. Mọi lần gửi sau đó dùng lại chuỗi `file_id` đó.

Đây đúng khuôn **web ghi ý định, bot thực thi** đã chạy cho bắn tin văn bản.

## Vì sao chỉ tải lên MỘT lần

Hệ cũ mở lại `images/2.jpg` (727.592 byte) cho **từng** người nhận: 13,9 GB băng thông cho
một đợt 19.151 người. Với `file_id`, Telegram chỉ copy tham chiếu và tổng băng thông là 0.

Hàng rào chống tái diễn **đã có sẵn** ở `outbox_worker`: nó ép `isinstance(photo, str)` và
ném nếu không phải. Nghĩa là dù ai đó sau này viết sai, hệ thống **không thể** quay về
upload-mỗi-người — nó chết ầm ĩ ở dòng đầu tiên thay vì âm thầm đốt 13,9 GB.

## Ba hàng rào của file này

1. **Cỡ tối đa 5 MB** — Telegram cho 10 MB, lấy một nửa để còn biên.
2. **Định dạng theo BYTE ĐẦU TỆP**, không theo phần mở rộng và không theo `Content-Type`
   do trình duyệt khai. Cả hai thứ sau đều do người gửi tự đặt.
3. **Vân tay `sha256`** — cùng một tấm ảnh tải lại lần hai trả `file_id` cũ ngay, không
   gọi Telegram lần nào.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from televip.core.logging import get_logger

log = get_logger(__name__)

#: Trần một tấm ảnh. Telegram cho 10 MB với `sendPhoto` tải lên; lấy 5 để còn biên, và vì
#: một ảnh bắn tin không có lý do gì lớn hơn.
MAX_ANH_BYTES: Final = 5 * 1024 * 1024

#: Số lượt thử tải lên trước khi bỏ cuộc. Cùng con số với hàng đợi tin.
MAX_LUOT_THU: Final = 5

#: Thời gian giữ chỗ của một lượt xử lý. Tiến trình chết giữa chừng thì hàng quay lại hàng
#: đợi sau ngần này, không kẹt vĩnh viễn.
LEASE_GIAY: Final = 120

#: Nhận dạng theo **byte đầu tệp**, không theo phần mở rộng và không theo `Content-Type`:
#: cả hai thứ đó do người gửi tự đặt. Chỉ ba định dạng Telegram nhận cho `sendPhoto`.
_CHU_KY: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # còn kiểm thêm "WEBP" ở byte 8-12
)


class AnhKhongHopLe(ValueError):
    """Tệp tải lên không dùng được. Câu chữ trong `args[0]` là câu để hiện cho người dùng."""


def _kieu_anh(du_lieu: bytes) -> str:
    """Kiểu MIME đọc từ byte đầu tệp. Ném `AnhKhongHopLe` nếu không nhận ra.

    Không tin phần mở rộng: một tệp `.jpg` chứa HTML vẫn là HTML, và Telegram sẽ từ chối
    cả đợt sau khi ta đã dựng xong tệp đích.
    """
    for chu_ky, mime in _CHU_KY:
        if du_lieu.startswith(chu_ky):
            if mime == "image/webp" and du_lieu[8:12] != b"WEBP":
                continue
            return mime
    raise AnhKhongHopLe("Chỉ nhận ảnh JPEG, PNG hoặc WebP.")


@dataclass(frozen=True, slots=True)
class KetQuaTaiLen:
    """`file_id` là `None` khi ảnh còn đang chờ bot tải lên."""

    upload_id: int
    sha256: str
    state: str
    file_id: str | None
    asset_key: str | None

    @property
    def san_sang(self) -> bool:
        return self.file_id is not None


_SQL_ASSET_THEO_SHA = "SELECT asset_key, file_id FROM media_assets WHERE sha256 = :sha LIMIT 1"

_SQL_ASSET_THEO_KEY = "SELECT file_id FROM media_assets WHERE asset_key = :key"

_SQL_TAO_UPLOAD = """
INSERT INTO media_uploads (du_lieu, ten_tep, kieu_mime, so_byte, sha256, state,
                           asset_key, created_by)
     VALUES (:du_lieu, :ten_tep, :mime, :so_byte, :sha, :state, :asset_key, :by)
  RETURNING upload_id
"""


async def xin_tai_anh(
    db: AsyncSession, *, du_lieu: bytes, ten_tep: str, created_by: int
) -> KetQuaTaiLen:
    """Nhận một tấm ảnh từ panel. **Không gọi Telegram** — chỉ ghi vào phòng chờ.

    Trả về ngay với `file_id` nếu đúng tấm ảnh này đã từng được tải lên: `sha256` là vân
    tay nội dung, nên tải lại cùng một tệp không tốn thêm một lượt gọi API nào.

    Ném:
        AnhKhongHopLe: rỗng, quá cỡ, hoặc không phải ảnh.
    """
    if not du_lieu:
        raise AnhKhongHopLe("Tệp rỗng.")
    if len(du_lieu) > MAX_ANH_BYTES:
        raise AnhKhongHopLe(
            f"Ảnh nặng {len(du_lieu) // 1024} KB, vượt trần {MAX_ANH_BYTES // 1024 // 1024} MB."
        )

    mime = _kieu_anh(du_lieu)
    sha = hashlib.sha256(du_lieu).hexdigest()

    # Đã có ảnh này rồi ⇒ trả `file_id` ngay. Vẫn ghi một hàng để có vết kiểm toán "ai đưa
    # ảnh nào vào hệ lúc nào", nhưng KHÔNG giữ bytes và KHÔNG xếp hàng chờ.
    da_co = (await db.execute(text(_SQL_ASSET_THEO_SHA), {"sha": sha})).one_or_none()
    if da_co is not None:
        upload_id = int(
            (
                await db.execute(
                    text(_SQL_TAO_UPLOAD),
                    {
                        "du_lieu": None,
                        "ten_tep": ten_tep,
                        "mime": mime,
                        "so_byte": len(du_lieu),
                        "sha": sha,
                        "state": "done",
                        "asset_key": da_co.asset_key,
                        "by": created_by,
                    },
                )
            ).scalar_one()
        )
        log.info("anh_da_co_san", upload_id=upload_id, sha=sha[:12], by=created_by)
        return KetQuaTaiLen(
            upload_id=upload_id,
            sha256=sha,
            state="done",
            file_id=da_co.file_id,
            asset_key=da_co.asset_key,
        )

    upload_id = int(
        (
            await db.execute(
                text(_SQL_TAO_UPLOAD),
                {
                    "du_lieu": du_lieu,
                    "ten_tep": ten_tep,
                    "mime": mime,
                    "so_byte": len(du_lieu),
                    "sha": sha,
                    "state": "pending",
                    "asset_key": None,
                    "by": created_by,
                },
            )
        ).scalar_one()
    )
    log.info("anh_vao_hang_cho", upload_id=upload_id, sha=sha[:12], so_byte=len(du_lieu))
    return KetQuaTaiLen(
        upload_id=upload_id, sha256=sha, state="pending", file_id=None, asset_key=None
    )


_SQL_TRANG_THAI = """
SELECT u.upload_id, u.sha256, u.state, u.asset_key, u.last_error, a.file_id
  FROM media_uploads u
  LEFT JOIN media_assets a ON a.asset_key = u.asset_key
 WHERE u.upload_id = :uid
"""


@dataclass(frozen=True, slots=True)
class TrangThaiAnh:
    upload_id: int
    sha256: str
    state: str
    file_id: str | None
    asset_key: str | None
    last_error: str | None

    @property
    def san_sang(self) -> bool:
        return self.state == "done" and self.file_id is not None


async def trang_thai(db: AsyncSession, upload_id: int) -> TrangThaiAnh | None:
    """Ảnh này đã có `file_id` chưa. `None` khi không có hàng nào."""
    row = (await db.execute(text(_SQL_TRANG_THAI), {"uid": upload_id})).one_or_none()
    if row is None:
        return None
    return TrangThaiAnh(
        upload_id=row.upload_id,
        sha256=row.sha256,
        state=row.state,
        file_id=row.file_id,
        asset_key=row.asset_key,
        last_error=row.last_error,
    )


async def file_id_cua(db: AsyncSession, asset_key: str) -> str | None:
    """`file_id` của một ảnh đã sẵn sàng. **Đường DUY NHẤT** lấy `photo` cho payload.

    Đọc từ `media_assets` — bảng chỉ có hàng khi `file_id` đã tồn tại thật. Nghĩa là "chỉ
    tạo được nháp khi ảnh đã sẵn sàng" là một hàng rào ở tầng DỮ LIỆU, không phải một cái
    nút bị vô hiệu bằng JavaScript.
    """
    return (await db.execute(text(_SQL_ASSET_THEO_KEY), {"key": asset_key})).scalar_one_or_none()


# ── Phía tiến trình bot ─────────────────────────────────────────────

#: Nhặt việc theo đúng khuôn hàng đợi tin: `FOR UPDATE SKIP LOCKED` để hai tiến trình không
#: tranh cùng một hàng, cộng `lease_until` để một tiến trình chết không giữ hàng vĩnh viễn.
_SQL_NHAN_VIEC = """
UPDATE media_uploads
   SET lease_until = now() + make_interval(secs => :lease),
       attempts    = attempts + 1
 WHERE upload_id IN (
       SELECT upload_id FROM media_uploads
        WHERE state = 'pending'
          AND visible_at <= now()
          AND (lease_until IS NULL OR lease_until < now())
        ORDER BY visible_at
         FOR UPDATE SKIP LOCKED
        LIMIT :lim
 )
RETURNING upload_id, du_lieu, ten_tep, sha256, attempts
"""


@dataclass(frozen=True, slots=True)
class ViecTaiAnh:
    upload_id: int
    du_lieu: bytes
    ten_tep: str
    sha256: str
    attempts: int


async def nhan_viec(db: AsyncSession, *, limit: int = 5) -> list[ViecTaiAnh]:
    """Ảnh đang chờ tải lên. Gọi từ tiến trình BOT."""
    rows = (await db.execute(text(_SQL_NHAN_VIEC), {"lease": LEASE_GIAY, "lim": limit})).all()
    return [
        ViecTaiAnh(
            upload_id=r.upload_id,
            du_lieu=bytes(r.du_lieu or b""),
            ten_tep=r.ten_tep,
            sha256=r.sha256,
            attempts=r.attempts,
        )
        for r in rows
    ]


_SQL_XONG = """
UPDATE media_uploads
   SET state = 'done', asset_key = :key, lease_until = NULL, last_error = NULL,
       -- Bytes hết việc ngay khi có `file_id`. Giữ lại là giữ vài MB trong WAL cho một
       -- thứ không ai đọc nữa.
       du_lieu = NULL
 WHERE upload_id = :uid
"""

_SQL_GHI_ASSET = """
INSERT INTO media_assets (asset_key, sha256, file_id, width, height)
     VALUES (:key, :sha, :file_id, :w, :h)
ON CONFLICT (asset_key) DO NOTHING
"""


async def danh_dau_xong(
    db: AsyncSession,
    *,
    upload_id: int,
    sha256: str,
    file_id: str,
    width: int | None,
    height: int | None,
) -> str:
    """Ghi `file_id` vào danh mục và bỏ bytes — trong CÙNG giao dịch. Trả `asset_key`."""
    key = f"sha256:{sha256}"
    await db.execute(
        text(_SQL_GHI_ASSET),
        {"key": key, "sha": sha256, "file_id": file_id, "w": width, "h": height},
    )
    await db.execute(text(_SQL_XONG), {"uid": upload_id, "key": key})
    log.info("anh_da_co_file_id", upload_id=upload_id, sha=sha256[:12])
    return key


_SQL_HOAN = """
UPDATE media_uploads
   SET lease_until = NULL,
       visible_at  = now() + make_interval(secs => :cho),
       last_error  = :loi
 WHERE upload_id = :uid
"""

_SQL_BO_CUOC = """
UPDATE media_uploads
   SET state = 'failed', lease_until = NULL, last_error = :loi
 WHERE upload_id = :uid
"""


async def danh_dau_hong(
    db: AsyncSession, *, upload_id: int, attempts: int, loi: str, vinh_vien: bool
) -> None:
    """Ghi lỗi. `vinh_vien=True` (ảnh hỏng, bot không ở trong nhóm) thì bỏ cuộc ngay.

    Lỗi **tạm thời** (mạng, 429) thì lùi lại và thử lại, giữ nguyên bytes. Lẫn hai loại
    này là hoặc bỏ cuộc trên một sự cố mạng thoáng qua, hoặc thử lại năm lần một ảnh chắc
    chắn hỏng — và câu hiện cho người vận hành sai theo cùng cách.
    """
    if vinh_vien or attempts >= MAX_LUOT_THU:
        await db.execute(text(_SQL_BO_CUOC), {"uid": upload_id, "loi": loi[:500]})
        log.warning("anh_tai_len_that_bai", upload_id=upload_id, loi=loi[:120])
        return
    cho = min(300, 2**attempts)
    await db.execute(text(_SQL_HOAN), {"uid": upload_id, "cho": cho, "loi": loi[:500]})
    log.info("anh_tai_len_hoan_lai", upload_id=upload_id, lan=attempts, cho_giay=cho)


__all__ = [
    "LEASE_GIAY",
    "MAX_ANH_BYTES",
    "MAX_LUOT_THU",
    "AnhKhongHopLe",
    "KetQuaTaiLen",
    "TrangThaiAnh",
    "ViecTaiAnh",
    "danh_dau_hong",
    "danh_dau_xong",
    "file_id_cua",
    "nhan_viec",
    "trang_thai",
    "xin_tai_anh",
]
