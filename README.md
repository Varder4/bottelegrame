# TeleVip Bot

Bot Telegram phát gift code. Xây mới hoàn toàn — không dùng lại code hay dữ liệu của bot cũ.

Bot: [@maymansedenvoitoi6868_bot](https://t.me/maymansedenvoitoi6868_bot)

## Chạy trên máy dev (Windows)

```bash
# 1. Hạ tầng
docker compose -f docker-compose.dev.yml up -d

# 2. Môi trường Python
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# 3. Cấu hình — sao chép rồi điền token thật
cp .env.example .env

# 4. Dựng schema
PYTHONPATH=src .venv/Scripts/python.exe -m alembic upgrade head

# 5. Test
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q
```

## Trạng thái

| Phần | Trạng thái |
|---|---|
| Hạ tầng dev (Postgres 16 + Redis 7.2) | ✅ |
| Tầng nền (config, clock, logging, errors, ids) | ✅ |
| Schema — 40 bảng, migration hai chiều | ✅ |
| Cấp code nguyên tử + sổ cái | ✅ |
| Test chống phát trùng | ✅ 6/6 |
| Tầng Telegram (handlers, keyboards, sender) | ⬜ |
| Mini App xác minh + HMAC initData | ⬜ |
| Engine broadcast (outbox) | ⬜ |
| Lệnh admin | ⬜ |
| Chống gian lận / KYC | ⬜ |

Lộ trình đầy đủ: [`../docs/ke-hoach-v2/14-lo-trinh-xay-moi.md`](../docs/ke-hoach-v2/14-lo-trinh-xay-moi.md)
Đặc tả chức năng: [`../docs/ke-hoach-v2/13-dac-ta-bot-moi.md`](../docs/ke-hoach-v2/13-dac-ta-bot-moi.md)

## Ba luật không được phá

1. **Mọi luồng phát code đi qua `services/code_issuance.py`.** Không có đường thứ hai.
   Hệ cũ có bốn nơi tự cấp code và cả bốn đều sai theo cùng một kiểu.
2. **Con số nghiệp vụ nằm trong bảng `settings`, không nằm trong code.** Mệnh giá, tỉ lệ
   trúng, mốc thưởng, trần ngân sách — đổi bằng lệnh admin, không phải bằng deploy.
3. **`ADMIN_GROUP_ID` chỉ để nhận cảnh báo.** Phân quyền đọc từ `admin_users` +
   `admin_permissions`. Ở hệ cũ, ai vào được nhóm admin là có toàn quyền.

## Cấu trúc

```
src/televip/
├── core/       # config, clock (giờ VN), logging (che token), errors, ids
├── db/         # models (40 bảng), engine, migrations
├── domain/     # quy tắc nghiệp vụ thuần, không đụng DB/mạng
├── cache/      # Redis: rate limit, chống spam, khoá, idempotency
├── telegram/   # sender, keyboards, router
├── services/   # code_issuance, referral, checkin, membership, broadcast
└── apps/       # web (webhook + Mini App API), worker, scheduler
```
