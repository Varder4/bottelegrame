# Triển khai HK79 lên VPS Ubuntu

Tài liệu bàn giao. Đọc hết **mục 0** trước khi gõ lệnh đầu tiên — trong đó có bốn cái bẫy
mà nếu vấp phải thì triệu chứng không nói gì về nguyên nhân.

- **Đích:** Ubuntu 22.04/24.04, 2 vCPU / 4 GB trở lên.
- **Kiến trúc:** Postgres + Redis cài **native** (không Docker). Ứng dụng chạy từ một bản
  `git clone` với venv riêng, do **systemd** trông.
- **Ba tiến trình:** `televip-worker` (bot) · `televip-miniapp` (:8099) · `televip-panel` (:8100).
  Nginx đứng trước hai cái sau; worker không nghe cổng nào.

---

## 0. Bốn cái bẫy — đọc trước khi làm

**① Chỉ được chạy MỘT tiến trình bot.**
Bot dùng polling. Hai bản cùng gọi `getUpdates` thì Telegram trả
`Conflict: terminated by other getUpdates request` cho **cả hai** và bot chết hẳn. Đừng
nhân bản `televip-worker.service`, đừng chạy tay `python -m televip.apps.worker.main` trong
khi service đang bật. Kiểm: `systemctl is-active televip-worker` và `pgrep -af worker.main`.

**② nginx PHẢI đặt `X-Forwarded-Proto`.**
Thiếu dòng đó thì ứng dụng tưởng mình đang chạy HTTP còn trình duyệt gửi HTTPS. Tầng web
kiểm `Origin` của mọi request GHI phải khớp `base_url`, nên **mọi nút ghi trên panel** —
sửa câu chữ, thu hồi mã, gửi bản tin, đổi cấu hình — đều trả *"trang không tồn tại"*, và
**không có thông báo lỗi nào**. Dòng đó đã có sẵn trong `nginx-televip.conf`; đừng bỏ.

**③ `TELEVIP_ENV=production`.**
Quên thì không lỗi gì báo, nhưng cookie đăng nhập panel mất cờ `Secure` → đăng nhập xong
quay lại vẫn thấy trang đăng nhập, im lặng; và sơ đồ API của Mini App mở ra internet.

**④ Cài bằng `pip install -e .`, không phải `pip install .`.**
`pyproject.toml` khai gói theo `packages.find`, nên bản cài thường **bỏ lại** thư mục
`webapp/` (Mini App) — nó nằm ở gốc repo, ngoài package. Chạy tại chỗ (`-e`) là cách đơn
giản và chắc chắn nhất. Các script bên dưới đã dùng `-e`.

---

## 1. Chuẩn bị máy

```bash
sudo apt update && sudo apt install -y \
    python3.12 python3.12-venv python3-pip \
    postgresql postgresql-contrib redis-server \
    nginx git certbot python3-certbot-nginx

sudo adduser --system --group --home /opt/televip televip
```

## 2. Postgres

```bash
sudo -u postgres psql <<'SQL'
CREATE USER televip WITH PASSWORD 'DOI_MAT_KHAU_NAY';
-- LC_COLLATE = 'C' bắt buộc: so sánh chuỗi không phụ thuộc locale máy, giống hệt dev.
CREATE DATABASE televip OWNER televip
    ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
SQL
```

Rồi sửa `/etc/postgresql/16/main/postgresql.conf` (nấc 2 vCPU / 4 GB):

```
shared_buffers = 1GB
effective_cache_size = 3GB
synchronous_commit = on
max_connections = 200
```

`sudo systemctl restart postgresql`

## 3. Redis

Sửa `/etc/redis/redis.conf`:

```
maxmemory 512mb
maxmemory-policy noeviction
appendonly yes
```

> `noeviction` chứ **không** `allkeys-lru`. Redis giữ khoá chống-gửi-trùng và token bucket;
> khoá đó bị đuổi ra khỏi bộ nhớ nghĩa là **phát trùng mã**.

`sudo systemctl restart redis-server`

## 4. Mã nguồn

```bash
sudo -u televip git clone <URL_REPO> /opt/televip
cd /opt/televip
sudo -u televip python3.12 -m venv .venv
sudo -u televip .venv/bin/pip install -U pip
sudo -u televip .venv/bin/pip install -e .      # ← `-e`, xem bẫy ④
```

## 5. Cấu hình

```bash
sudo -u televip cp .env.example .env
sudo -u televip nano .env
sudo chmod 600 /opt/televip/.env
```

Bắt buộc điền: `TELEVIP_BOT_TOKEN`, `TELEVIP_DATABASE_URL`, `TELEVIP_REDIS_URL`,
`TELEVIP_ADMIN_GROUP_ID`, và `TELEVIP_ENV=production`. Đặt `TELEVIP_DB_POOL_SIZE=20`.

## 6. Tạo bảng

```bash
cd /opt/televip
sudo -u televip .venv/bin/python -m alembic upgrade head
```

Câu này tạo bảng **và** seed cấu hình mặc định. Chạy lại nhiều lần vô hại.

## 7. Bật ba tiến trình

```bash
sudo cp deploy/televip-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now televip-worker televip-miniapp televip-panel
sudo systemctl status televip-worker --no-pager
```

## 8. Nginx + TLS

```bash
sudo cp deploy/nginx-televip.conf /etc/nginx/sites-available/televip
sudo sed -i 's/MIENCUABAN.COM/ten-mien-that.com/g' /etc/nginx/sites-available/televip
sudo ln -sf /etc/nginx/sites-available/televip /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d ten-mien-that.com -d panel.ten-mien-that.com
```

## 9. Sao lưu

```bash
sudo cp deploy/sao-luu.sh /usr/local/bin/televip-sao-luu
sudo chmod +x /usr/local/bin/televip-sao-luu
sudo crontab -e     # thêm:  0 3 * * * /usr/local/bin/televip-sao-luu >> /var/log/televip-saoluu.log 2>&1
```

---

## 10. SAU KHI DỰNG XONG — ba việc trong panel

Vào `https://panel.ten-mien-that.com`, đăng nhập, rồi:

**① Đổi `webapp.url` sang tên miền thật.** Màn *Cấu hình* → khoá `webapp.url` → đặt
`https://ten-mien-that.com`.

> Đây là việc **quan trọng nhất** sau khi dựng. Giá trị đang nằm trong database là một
> đường hầm tạm của máy dev. Sai khoá này = nút "Xác minh ngay" mở một trang chết = **không
> ai xác minh được = không ai nhận được mã nào**. Hiệu lực trong 60 giây, không cần khởi
> động lại gì.

**② Kiểm dải đỏ trên trang chủ panel.** Nếu có dòng *"N kênh bắt buộc đang lỗi"* thì bot
chưa được làm **quản trị viên** trong những kênh đó. Chưa sửa thì không ai nhận được mã.

**③ Nạp mã vào kho.** Màn *Kho code* → ô "Nạp mã vào kho".

---

## 11. Kiểm tra đã sống

```bash
systemctl is-active televip-worker televip-miniapp televip-panel   # cả ba: active
journalctl -u televip-worker -n 50 --no-pager                      # có dòng "dang_lang_nghe"
curl -sI https://ten-mien-that.com | head -1                       # 200
curl -sI https://panel.ten-mien-that.com/dangnhap | head -1        # 200
```

Rồi mở Telegram, gõ `/start` cho bot: phải ra lời chào + bàn phím, và ngay dưới là màn
xác thực.

## 12. Xem log

```bash
journalctl -u televip-worker -f
journalctl -u televip-panel -f
```

## 13. Cập nhật mã mới

```bash
cd /opt/televip
sudo -u televip git pull
sudo -u televip .venv/bin/pip install -e .
sudo -u televip .venv/bin/python -m alembic upgrade head
sudo systemctl restart televip-worker televip-miniapp televip-panel
```

---

## 14. Việc CHƯA làm — bàn giao trung thực

Những thứ dưới đây **đã biết là còn thiếu**, không phải chưa phát hiện. Chúng không chặn
việc dựng máy, nhưng ai tiếp nhận nên biết:

| Việc | Hậu quả nếu bỏ qua |
|---|---|
| Người bị `/ban` vẫn rút được mã | Lệnh ban chỉ ghi vào bảng `user_bans`; không handler phát mã nào đọc bảng đó. Ban hiện chỉ loại khỏi danh sách bắn tin. |
| Trần gửi tin không tự phục hồi | Mỗi lần Telegram phạt 429, trần tụt 30 → 18 → … và **không có gì nâng lại**. Sau vài lần, một đợt bắn tin 19.000 người mất 40 phút thay vì 11. Gỡ tạm bằng cách xoá khoá tương ứng trong Redis. |
| Không có màn quản lý nhóm/kênh bắt buộc | Thêm/bớt kênh phải sửa thẳng bảng `required_chats` trong database. |
| `/help` đăng ký trong menu nhưng chưa nối handler | Bấm vào bot im lặng. |
| Chưa có lưới hứng lỗi chung | Lỗi lọt ra khỏi một nút thì người dùng không thấy gì cả. |
| Nút "Điểm Danh" và "Đổi CODE" đang tạm ẩn | Có chủ ý, theo yêu cầu chủ bot. Bật lại: bỏ nhãn khỏi `keyboards.AN_TAM_THOI`. |
