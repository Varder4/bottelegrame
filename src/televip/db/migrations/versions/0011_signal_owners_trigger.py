"""signal_owners duoc trigger duy tri tu identity_signals

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-30

`signal_owners` la bang CHIEU NGUOC cua `identity_signals`, ton tai de tra loi mot cau
duy nhat: *(IP | thiet bi) nay dang co may tai khoan* — ma khong phai COUNT tren toan
bang o moi luot verify. `07-db.md` §198 khai ro no "duoc trigger duy tri khi
`identity_signals` doi".

Truoc migration nay bang co that nhung **khong co ai ghi vao**: 0 dong, va se mai 0 dong.
Do la cung mot dang loi ma `user_stats` tung mac (bang xep hang rong vinh vien) — chi
khac o cho hau qua nang hon: khong co du lieu thi bon tuan "shadow" ma ca ke hoach chong
gian lan dua vao **khong bao gio bat dau dem**, va `/checkip` khong tra loi duoc gi.

⚠️ **Day la THU THAP DU LIEU, khong phai bat luat.** Khong dong nao trong migration nay
chan mot ai. `04-fraud.md` co y de cac bang luat rong cho toi khi co bon tuan so lieu,
vi bat luat som la chan oan nguoi that — nhung *do dac* thi phai bat dau tu bay gio,
neu khong thi bon tuan do khong bao gio troi qua.

## Vi sao TRIGGER chu khong phai mot cau UPDATE trong code

Bang nay phai dung voi `identity_signals` o MOI duong ghi. Hien chi co mot duong
(`/api/verify`), nhung ke hoach con them `device_hash` va `ua_hash`, va moi duong quen
cap nhat la mot con so sai ma khong ai phat hien — no chi hien ra thanh "IP nay co 1 tai
khoan" trong khi that ra co 40.

## Vi sao dem lai thay vi cong don

Trigger tinh `user_count` bang cach DEM LAI tren dung mot khoa `(signal_type,
signal_value)`. Cong don (`user_count + 1`) nhanh hon nhung troi vinh vien sau bat ky
lan rollback, sua tay, hay xoa nao — va mot bo dem tu tang da troi khoi su that chinh la
loi cua he cu. Cau dem nay chay tren `ix_signals_reverse`, pham vi la mot khoa chu khong
phai ca bang.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FUNCTION = """
CREATE OR REPLACE FUNCTION dong_bo_signal_owners() RETURNS trigger AS $$
DECLARE
    v_type  text;
    v_value text;
    v_count int;
    v_first bigint;
    v_seen  timestamptz;
BEGIN
    -- DELETE khong co NEW; moi truong hop con lai deu co.
    IF (TG_OP = 'DELETE') THEN
        v_type  := OLD.signal_type;
        v_value := OLD.signal_value;
    ELSE
        v_type  := NEW.signal_type;
        v_value := NEW.signal_value;
    END IF;

    SELECT count(DISTINCT user_id),
           min(user_id),
           max(last_seen)
      INTO v_count, v_first, v_seen
      FROM identity_signals
     WHERE signal_type = v_type AND signal_value = v_value;

    IF (v_count = 0) THEN
        -- Tin hieu cuoi cung cua khoa nay vua bi xoa: bo luon dong chieu nguoc, dung de
        -- lai mot dong noi "0 tai khoan" ma /checkip phai loc.
        DELETE FROM signal_owners WHERE signal_type = v_type AND signal_value = v_value;
        RETURN NULL;
    END IF;

    INSERT INTO signal_owners (signal_type, signal_value, user_count, first_user_id, last_seen)
         VALUES (v_type, v_value, v_count, v_first, v_seen)
    ON CONFLICT (signal_type, signal_value) DO UPDATE
            SET user_count    = EXCLUDED.user_count,
                -- `first_user_id` la nguoi DAU TIEN mang tin hieu nay; giu nguyen khi da
                -- co, neu khong no doi nghia thanh "nguoi co id nho nhat con lai".
                first_user_id = COALESCE(signal_owners.first_user_id, EXCLUDED.first_user_id),
                last_seen     = GREATEST(signal_owners.last_seen, EXCLUDED.last_seen);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

#: `AFTER` chu khong `BEFORE`: ham dem lai tren chinh bang vua doi, nen no phai chay khi
#: hang moi da nam trong bang. `FOR EACH ROW` vi mot lenh co the cham nhieu khoa khac nhau.
_TRIGGER = """
CREATE TRIGGER trg_signal_owners
AFTER INSERT OR UPDATE OR DELETE ON identity_signals
FOR EACH ROW EXECUTE FUNCTION dong_bo_signal_owners();
"""

#: Nap lai tu du lieu da co. Khong co buoc nay thi moi tin hieu ghi TRUOC migration nay
#: vinh vien vo hinh voi `/checkip` — va do la toan bo du lieu dang co.
_BACKFILL = """
INSERT INTO signal_owners (signal_type, signal_value, user_count, first_user_id, last_seen)
SELECT signal_type, signal_value, count(DISTINCT user_id), min(user_id), max(last_seen)
  FROM identity_signals
 GROUP BY signal_type, signal_value
ON CONFLICT (signal_type, signal_value) DO UPDATE
        SET user_count    = EXCLUDED.user_count,
            first_user_id = COALESCE(signal_owners.first_user_id, EXCLUDED.first_user_id),
            last_seen     = GREATEST(signal_owners.last_seen, EXCLUDED.last_seen)
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(_FUNCTION))
    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_signal_owners ON identity_signals"))
    conn.execute(sa.text(_TRIGGER))
    conn.execute(sa.text(_BACKFILL))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_signal_owners ON identity_signals"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS dong_bo_signal_owners()"))
    # Bang tro lai rong — dung trang thai truoc migration nay.
    conn.execute(sa.text("DELETE FROM signal_owners"))
