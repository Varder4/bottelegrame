"""seed khoa web.trusted_proxies — thieu no thi moi IP trong so la 127.0.0.1

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-05

`apps/web/miniapp.py:73` khai `SETTING_TRUSTED_PROXIES = "web.trusted_proxies"` va doc no
o moi luot xac minh (`:205`) de biet header `X-Forwarded-For` co dang tin khong. Nhung
**khong migration nao seed khoa do** — `grep -rn trusted_proxies` trong thu muc migrations
tra ve 0 ket qua.

Hau qua, va no im lang hoan toan:

- `settings_service.get_list()` co gia tri mac dinh nen KHONG nem loi; no tra ve danh sach
  rong. Danh sach rong nghia la "khong tin proxy nao".
- Dat nginx truoc ung dung — dieu bat buoc phai lam khi len VPS — thi moi ket noi den tu
  `127.0.0.1`, va vi khong proxy nao duoc tin nen `_client_ip()` ghi dung `127.0.0.1` vao
  `identity_signals` va `verification_events`.
- Khong co dong log nao bao. Bang van day du lieu. Chi la moi dong deu vo dung.

Do la lop chong gian lan **do sai mot cach im lang** — te hon han viec no bao loi, vi
bon tuan so lieu shadow se troi qua roi moi co nguoi phat hien ra ca bon tuan do vo nghia.

Gia tri seed la RONG (`[]`) — dung ngu nghia hien tai va an toan tren may dev khong co
proxy. Luc len VPS, dat bang mot lenh:

    /setcauhinh web.trusted_proxies ["127.0.0.1"]

Seed rong chu khong seed san `127.0.0.1`: tin mot proxy khong ton tai nghia la bat ky ai
cung gia mao duoc `X-Forwarded-For` va tu chon IP ghi vao so.

⚠️ `sensitive = false`, co y. Khoa `sensitive` bi `/setcauhinh` TU CHOI thang
(`handlers/admin/ops.py:400-407`) cho toi khi luong duyet hai nguoi duoc xay — nen danh
dau no `sensitive` se bien cau lenh o tren thanh khong chay duoc, va duong duy nhat con
lai la `psql` go tay tren VPS. Do dung la canh `nano config.py` ma ca thiet ke moi sinh
ra de cham dut. Khoa nay khong phai khoa tien; nguoi co quyen `/setcauhinh` von da nam
nhung thu nguy hiem hon nhieu.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY = "web.trusted_proxies"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("""
        INSERT INTO settings (key, value, value_type, label_vi, sensitive)
             VALUES (:k, CAST(:v AS jsonb), 'json',
                     'Danh sách proxy tin được (đọc X-Forwarded-For). Rỗng = không tin ai',
                     false)
        ON CONFLICT (key) DO NOTHING
        """),
        {"k": KEY, "v": json.dumps([])},
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM settings WHERE key = :k"), {"k": KEY})
