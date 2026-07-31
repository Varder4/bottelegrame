"""Bộ đo tải — tìm lỗi khi chưa ai mất tiền.

    python -m scripts.loadtest              chạy tất cả kịch bản, quy mô mặc định
    python -m scripts.loadtest --users 2000 --codes 500
    python -m scripts.loadtest --only kho   chỉ một kịch bản

**Chạy trên database RIÊNG `televip_load`.** Không đụng `televip` (dev) và không đụng
`televip_test` (bộ kiểm thử). Script tự tạo, tự migrate, và tự xoá sạch dữ liệu trước mỗi
lần chạy — nhưng KHÔNG tự xoá database, để còn mổ xẻ sau khi có kết quả lạ.

## Nó đo cái gì, và vì sao đo cái đó

Bốn con số dưới đây là bốn chỗ hệ cũ gãy dưới tải, không phải bốn chỉ số cho đẹp:

1. **Kho code dưới tranh chấp.** N người cùng giành M mã phải ra **đúng min(N, M)** mã,
   **không mã nào phát hai lần**. Hệ cũ kiểm bằng `SELECT COUNT(*)` rồi mới cấp, và khe
   giữa hai câu đó đủ rộng để 226 tài khoản ôm 544 mã thừa.

2. **Trần ngân sách event.** Trần là trần **cứng** hay chỉ là một con số trong cấu hình —
   đo bằng cách cho hàng trăm lượt đập hộp chạy cùng lúc rồi cộng lại.

3. **Xô token 30 tin/giây.** Đo tốc độ THẬT. Hệ cũ ngủ cứng 1 giây sau mỗi lô 30 tin nên
   vừa chậm hơn hạn mức (12-23 tin/giây) vừa vẫn ăn 429 (100 tin/giây tức thời trong 0,3
   giây đầu mỗi chu kỳ).

4. **Pool kết nối.** `db_pool_size` mặc định 10 và `max_overflow=0` — vượt là **chờ**. Đo
   xem bao nhiêu lượt đồng thời thì độ trễ bắt đầu vọt lên vì xếp hàng lấy kết nối.

## Luật của bộ đo

- **Bất biến quan trọng hơn tốc độ.** Mỗi kịch bản kết thúc bằng một lần đối soát bốn
  nguồn (`codes` / `code_grants` / `code_ledger` / bộ đếm `users`). Nhanh mà lệch sổ thì
  vẫn là TRƯỢT.
- **Không có ngưỡng đậu/rớt cho tốc độ.** In số ra để người đọc quyết định; một con số
  cứng ở đây sẽ thành lời hứa mà phần cứng của máy khác không giữ được.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BASE_DSN = os.environ.get(
    "TELEVIP_LOAD_BASE_DSN", "postgresql://televip:televip_dev_only@127.0.0.1:5433"
)
LOAD_DB = os.environ.get("TELEVIP_LOAD_DB", "televip_load")

#: Hàng rào chống chạy nhầm vào database thật. Tên phải kết thúc bằng `_load`.
if not LOAD_DB.endswith("_load"):
    raise SystemExit(f"TELEVIP_LOAD_DB phải kết thúc bằng '_load', nhận {LOAD_DB!r}")

SQLA_DSN = f"postgresql+asyncpg://{BASE_DSN.split('://', 1)[1]}/{LOAD_DB}"
RAW_DSN = f"{BASE_DSN}/{LOAD_DB}"

REDIS_URL = os.environ.get("TELEVIP_LOAD_REDIS_URL", "redis://127.0.0.1:6380/14")


# ── Kết quả ─────────────────────────────────────────────────────────


@dataclass
class KetQua:
    ten: str
    so_luot: int = 0
    thanh_cong: int = 0
    do_tre_ms: list[float] = field(default_factory=list)
    giay: float = 0.0
    ghi_chu: list[str] = field(default_factory=list)
    vi_pham: list[str] = field(default_factory=list)

    def them(self, ms: float, *, ok: bool) -> None:
        self.so_luot += 1
        self.thanh_cong += int(ok)
        self.do_tre_ms.append(ms)

    def _pct(self, p: float) -> float:
        if not self.do_tre_ms:
            return 0.0
        xep = sorted(self.do_tre_ms)
        return xep[min(len(xep) - 1, int(len(xep) * p))]

    def in_ra(self) -> None:
        dau = "❌ TRƯỢT" if self.vi_pham else "✅ ĐẠT"
        tps = self.so_luot / self.giay if self.giay > 0 else 0
        print(f"\n{dau}  {self.ten}")
        print(f"   {self.so_luot:,} lượt · {self.giay:.2f}s · {tps:,.0f} lượt/giây")
        if self.do_tre_ms:
            print(
                f"   độ trễ: trung vị {statistics.median(self.do_tre_ms):.0f}ms · "
                f"p95 {self._pct(0.95):.0f}ms · p99 {self._pct(0.99):.0f}ms · "
                f"tệ nhất {max(self.do_tre_ms):.0f}ms"
            )
        for dong in self.ghi_chu:
            print(f"   · {dong}")
        for dong in self.vi_pham:
            print(f"   ❌ {dong}")


# ── Hạ tầng ─────────────────────────────────────────────────────────


async def _tao_db_neu_thieu() -> None:
    conn = await asyncpg.connect(f"{BASE_DSN}/postgres")
    try:
        co = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", LOAD_DB)
        if not co:
            await conn.execute(f'CREATE DATABASE "{LOAD_DB}"')
            print(f"đã tạo database {LOAD_DB}")
    finally:
        await conn.close()


def _migrate() -> None:
    env = {**os.environ, "TELEVIP_DATABASE_URL": SQLA_DSN}
    ket_qua = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    if ket_qua.returncode != 0:
        raise SystemExit(f"alembic upgrade head hỏng:\n{ket_qua.stderr[-2000:]}")


_BANG_XOA = """
TRUNCATE TABLE
    code_ledger, code_grants, codes, idempotency_records, code_pool_stats,
    referral_rewards, referrals, referral_intents, campaigns,
    checkins, points_ledger, event_participations, events,
    group_memberships, membership_events, membership_mismatch,
    broadcast_targets, broadcast_jobs, outbox_messages,
    audit_log, system_stats, user_stats,
    identity_clusters, cluster_stats, risk_assessments, fraud_cases,
    verification_events, signal_owners, identity_signals,
    user_bans, users
RESTART IDENTITY CASCADE
"""


async def _don_sach(factory: Any) -> None:
    async with factory() as s:
        await s.execute(text(_BANG_XOA))
        await s.commit()


async def _seed(factory: Any, *, so_user: int, so_ma: int, menh_gia: int) -> None:
    """Nạp user và kho code bằng COPY-style bulk insert — seed không được là nút cổ chai."""
    async with factory() as s:
        await s.execute(
            text("""
            INSERT INTO grant_types (code, label_vi, once_per_life) VALUES
                ('tanthu','Code tan thu',true), ('referral_milestone','Moc',false),
                ('event_box','Dap hop',false), ('points_redeem','Doi diem',false),
                ('share_event','Chia se',true), ('admin_manual','Trao tay',false)
            ON CONFLICT DO NOTHING
            """)
        )
        await s.execute(
            text("""
            INSERT INTO users (user_id, username, verified_at)
            SELECT g, 'u' || g, now() FROM generate_series(1, :n) AS g
            ON CONFLICT DO NOTHING
            """),
            {"n": so_user},
        )
        for loai in ("tanthu", "event"):
            await s.execute(
                text("""
                INSERT INTO codes (code_value, code_type, value_vnd, status)
                SELECT :loai || '-' || g, :loai, :gia, 'available'
                  FROM generate_series(1, :n) AS g
                ON CONFLICT DO NOTHING
                """),
                {"loai": loai, "gia": menh_gia, "n": so_ma},
            )
        await s.commit()


async def _doi_soat(factory: Any) -> list[str]:
    """Bốn nguồn phải nói cùng một con số. Lệch là TRƯỢT, bất kể tốc độ."""
    async with factory() as s:
        row = (
            await s.execute(
                text("""
                SELECT
                  (SELECT coalesce(sum(value_vnd),0) FROM code_grants
                    WHERE state = 'delivered')                              AS so_phat_hanh,
                  (SELECT coalesce(sum(value_vnd * direction),0)
                     FROM code_ledger)                                      AS so_cai,
                  (SELECT coalesce(sum(total_value_received),0) FROM users) AS bo_dem,
                  (SELECT count(*) FROM codes WHERE status = 'issued')      AS ma_da_phat,
                  (SELECT count(*) FROM code_grants
                    WHERE state = 'delivered')                              AS grant_da_giao,
                  (SELECT count(*) FROM (
                     SELECT code_id FROM code_grants WHERE code_id IS NOT NULL
                      GROUP BY code_id HAVING count(*) > 1) x)              AS ma_phat_trung
                """)
            )
        ).one()

    loi: list[str] = []
    if not (row.so_phat_hanh == row.so_cai == row.bo_dem):
        loi.append(
            f"lệch sổ: phát hành {row.so_phat_hanh:,} · sổ cái {row.so_cai:,} · "
            f"bộ đếm {row.bo_dem:,}"
        )
    if row.ma_da_phat != row.grant_da_giao:
        loi.append(f"kho nói {row.ma_da_phat:,} mã đã phát, sổ nói {row.grant_da_giao:,}")
    if row.ma_phat_trung:
        loi.append(f"CÓ {row.ma_phat_trung} MÃ ĐƯỢC PHÁT CHO NHIỀU NGƯỜI")
    return loi


async def _chay_song_song(
    viec: Callable[[int], Awaitable[bool]], *, so_luot: int, dong_thoi: int, kq: KetQua
) -> None:
    """Chạy `so_luot` lượt với trần `dong_thoi` chạy cùng lúc."""
    cong = asyncio.Semaphore(dong_thoi)

    async def _mot(i: int) -> None:
        async with cong:
            t0 = time.perf_counter()
            try:
                ok = await viec(i)
            except Exception as exc:  # noqa: BLE001 - bộ đo phải ghi lại, không được chết
                kq.ghi_chu.append(f"ngoại lệ: {type(exc).__name__}: {exc}"[:200])
                ok = False
            kq.them((time.perf_counter() - t0) * 1000, ok=ok)

    bat_dau = time.perf_counter()
    await asyncio.gather(*(_mot(i) for i in range(so_luot)))
    kq.giay = time.perf_counter() - bat_dau


# ── Kịch bản ────────────────────────────────────────────────────────


async def kb_kho(factory: Any, *, so_luot: int, so_ma: int, dong_thoi: int) -> KetQua:
    """N người cùng giành M mã. Bất biến: đúng min(N,M) mã, không mã nào phát hai lần."""
    from televip.core.ids import grant_key_tanthu
    from televip.services import code_issuance

    kq = KetQua(f"Kho code dưới tranh chấp ({so_luot} người / {so_ma} mã)")

    async def _giành(i: int) -> bool:
        uid = i + 1
        async with factory() as s, s.begin():
            try:
                grant = await code_issuance.reserve(
                    s,
                    user_id=uid,
                    grant_type="tanthu",
                    grant_key=grant_key_tanthu(uid),
                    code_type="tanthu",
                    value_vnd=10_000,
                )
            except Exception:  # hết kho là kết quả HỢP LỆ, không phải lỗi
                return False
        async with factory() as s, s.begin():
            await code_issuance.mark_delivered(s, grant_id=grant.grant_id)
        return True

    await _chay_song_song(_giành, so_luot=so_luot, dong_thoi=dong_thoi, kq=kq)

    mong_doi = min(so_luot, so_ma)
    kq.ghi_chu.append(f"phát được {kq.thanh_cong:,} mã (mong đợi đúng {mong_doi:,})")
    if kq.thanh_cong != mong_doi:
        kq.vi_pham.append(f"phát {kq.thanh_cong:,} mã, đáng lẽ {mong_doi:,}")
    kq.vi_pham += await _doi_soat(factory)
    return kq


async def kb_tran_event(factory: Any, *, so_luot: int, dong_thoi: int) -> KetQua:
    """Trần ngân sách event dưới tải: tổng đã chi không được vượt quá trần + MỘT giải."""
    from televip.services import event_box, settings_service

    TRAN = 200_000
    GIAI = 10_000

    async with factory() as s:
        for khoa, gia_tri, kieu in (
            (event_box.BUDGET_CAP_KEY, str(TRAN), "money_vnd"),
            (event_box.WINDOW_MINUTES_KEY, "600", "int"),
            (event_box.PRIZE_TABLE_KEY, f'[{{"value_vnd": {GIAI}, "weight_bp": 10000}}]', "json"),
        ):
            await s.execute(
                text("""
                INSERT INTO settings (key, value, value_type, label_vi)
                     VALUES (:k, CAST(:v AS jsonb), :t, :k)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, max_value = NULL
                """),
                {"k": khoa, "v": gia_tri, "t": kieu},
            )
        event_id = await event_box.create_event(s, created_by=1, caption="loadtest")
        await s.execute(
            text("UPDATE events SET is_active = true WHERE event_id = :e"), {"e": event_id}
        )
        await s.commit()
    settings_service.invalidate()

    kq = KetQua(f"Trần ngân sách event ({so_luot} lượt đập cùng lúc, trần {TRAN:,}đ)")

    async def _dap(i: int) -> bool:
        async with factory() as s, s.begin():
            kt = await event_box.open_box(s, event_id=event_id, user_id=i + 1)
        return kt.status == "win"

    await _chay_song_song(_dap, so_luot=so_luot, dong_thoi=dong_thoi, kq=kq)

    async with factory() as s:
        da_chi = await event_box.spent_vnd(s, event_id=event_id)
    kq.ghi_chu.append(f"đã chi {da_chi:,}đ / trần {TRAN:,}đ ({kq.thanh_cong:,} người trúng)")
    # `>= trần mới dừng` là ngữ nghĩa cố ý, nên phần vượt tối đa là ĐÚNG MỘT giải.
    if da_chi > TRAN + GIAI:
        kq.vi_pham.append(f"vượt trần {da_chi - TRAN:,}đ — nhiều hơn một giải, trần là trần MỀM")
    return kq


async def kb_xo_token(*, so_tin: int) -> KetQua:
    """Hai con số khác nhau, và lẫn chúng là đo sai.

    - **Burst**: xô đầy `G_MAX` token lúc khởi động, nên `G_MAX` tin đầu đi gần như tức
      thì. Đó là hợp đồng của token bucket chứ không phải lỗi — nó chính là thứ cho phép
      một đợt khởi động không bị chặn ngay từ tin đầu.
    - **Tốc độ bền vững**: sau khi xô cạn, tốc độ do nhịp nạp lại quyết định. Đây mới là
      con số đem so với trần 30/giây của Telegram, và là con số dùng để ước lượng một đợt
      bắn tin mất bao lâu.

    Bản đo đầu tiên gộp cả hai và ra 37,2 tin/giây trên 90 tin — chính là
    `(30 tức thì + 60 ở nhịp 30/s) / 2,0 giây`. Con số ấy không sai, nhưng nó không trả
    lời câu hỏi nào cả: nó không phải burst, cũng không phải tốc độ bền vững.
    """
    from types import SimpleNamespace

    from televip.cache import ratelimit
    from televip.cache.client import close_redis, get_redis, init_redis

    init_redis(SimpleNamespace(redis_url=REDIS_URL))  # type: ignore[arg-type]
    # Xô phải ở trạng thái ĐẦU TIÊN khi bắt đầu đo — token thừa từ lần chạy trước làm
    # phần burst dài ra và tốc độ bền vững đo được cao hơn sự thật.
    await get_redis().flushdb()

    kq = KetQua(f"Xô token 30 tin/giây ({so_tin} tin sau burst, lane bulk)")
    try:
        # ── Giai đoạn 1: rút cạn burst, KHÔNG tính giờ ──────────────
        t_burst = time.perf_counter()
        for _ in range(ratelimit.G_MAX):
            await ratelimit.acquire("bulk", timeout=30)
        giay_burst = time.perf_counter() - t_burst

        # ── Giai đoạn 2: đo nhịp nạp lại ────────────────────────────
        bat_dau = time.perf_counter()
        for _ in range(so_tin):
            t0 = time.perf_counter()
            ok = await ratelimit.acquire("bulk", timeout=30)
            kq.them((time.perf_counter() - t0) * 1000, ok=ok)
        kq.giay = time.perf_counter() - bat_dau
    finally:
        await close_redis()

    ben_vung = kq.so_luot / kq.giay if kq.giay > 0 else 0
    kq.ghi_chu.append(
        f"burst: {ratelimit.G_MAX} tin đầu trong {giay_burst * 1000:.0f}ms "
        f"(hợp đồng của xô, không phải lỗi)"
    )
    kq.ghi_chu.append(f"tốc độ BỀN VỮNG: {ben_vung:.1f} tin/giây")
    if ben_vung > 0:
        kq.ghi_chu.append(f"→ một đợt 19.151 người mất khoảng {19_151 / ben_vung / 60:.1f} phút")

    if kq.thanh_cong != kq.so_luot:
        kq.vi_pham.append(f"{kq.so_luot - kq.thanh_cong} lượt bị từ chối cấp token")
    # Chỉ tốc độ BỀN VỮNG mới đem so với trần. Vượt là lỗi thật: 429 phạt cả bot chứ
    # không phạt một tin. Nới 10% cho sai số đo trên máy đang bận.
    if ben_vung > ratelimit.G_MAX * 1.1:
        kq.vi_pham.append(f"{ben_vung:.1f} tin/giây bền vững — VƯỢT trần {ratelimit.G_MAX}")
    # Chậm hơn nhiều so với trần cũng là lỗi: hệ cũ chạy 12-23 tin/giây vì ngủ cứng một
    # giây sau mỗi lô, và một đợt 19.151 người khi đó mất gần một tiếng rưỡi.
    if 0 < ben_vung < ratelimit.G_MAX * 0.8:
        kq.vi_pham.append(
            f"{ben_vung:.1f} tin/giây bền vững — chỉ đạt "
            f"{ben_vung / ratelimit.G_MAX * 100:.0f}% hạn mức, đang phí trần"
        )
    return kq


async def kb_pool(factory: Any, *, dong_thoi: int) -> KetQua:
    """Độ trễ khi số lượt đồng thời vượt `db_pool_size` (mặc định 10, `max_overflow=0`)."""
    kq = KetQua(f"Pool kết nối ({dong_thoi} truy vấn đồng thời)")

    async def _doc(_i: int) -> bool:
        async with factory() as s:
            await s.execute(text("SELECT pg_sleep(0.05)"))
        return True

    await _chay_song_song(_doc, so_luot=dong_thoi, dong_thoi=dong_thoi, kq=kq)
    kq.ghi_chu.append(
        "mỗi truy vấn ngủ 50ms; độ trễ vượt nhiều lần 50ms = đang xếp hàng lấy kết nối"
    )
    return kq


# ── CLI ─────────────────────────────────────────────────────────────


async def _chay(args: argparse.Namespace) -> int:
    await _tao_db_neu_thieu()
    _migrate()

    engine = create_async_engine(SQLA_DSN, pool_size=args.pool, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # `db.engine` toàn cục là thứ mà `services/*` dùng khi không được truyền session.
    from televip.db import engine as db_engine

    db_engine.init_engine(
        argparse.Namespace(database_url=SQLA_DSN, db_pool_size=args.pool)  # type: ignore[arg-type]
    )

    ket_qua: list[KetQua] = []
    try:
        if args.only in (None, "kho"):
            await _don_sach(factory)
            await _seed(factory, so_user=args.users, so_ma=args.codes, menh_gia=10_000)
            ket_qua.append(
                await kb_kho(
                    factory, so_luot=args.users, so_ma=args.codes, dong_thoi=args.concurrency
                )
            )

        if args.only in (None, "event"):
            await _don_sach(factory)
            await _seed(factory, so_user=args.users, so_ma=args.codes, menh_gia=10_000)
            ket_qua.append(
                await kb_tran_event(factory, so_luot=args.users, dong_thoi=args.concurrency)
            )

        if args.only in (None, "pool"):
            ket_qua.append(await kb_pool(factory, dong_thoi=args.concurrency))
    finally:
        await engine.dispose()
        await db_engine.dispose_engine()

    if args.only in (None, "bucket"):
        ket_qua.append(await kb_xo_token(so_tin=args.messages))

    print("\n" + "═" * 68)
    for kq in ket_qua:
        kq.in_ra()
    print("\n" + "═" * 68)

    hong = [kq for kq in ket_qua if kq.vi_pham]
    if hong:
        print(f"\n❌ {len(hong)}/{len(ket_qua)} kịch bản TRƯỢT bất biến.")
        return 1
    print(f"\n✅ {len(ket_qua)}/{len(ket_qua)} kịch bản giữ đúng bất biến.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--users", type=int, default=1_000, help="số người dùng mô phỏng")
    p.add_argument("--codes", type=int, default=300, help="số mã mỗi loại trong kho")
    p.add_argument("--concurrency", type=int, default=50, help="số lượt chạy cùng lúc")
    p.add_argument("--messages", type=int, default=90, help="số tin cho kịch bản xô token")
    p.add_argument("--pool", type=int, default=20, help="db_pool_size")
    p.add_argument("--only", choices=["kho", "event", "bucket", "pool"], help="chỉ một kịch bản")
    return asyncio.run(_chay(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
