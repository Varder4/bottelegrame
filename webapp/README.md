# Mini App xác minh

Màn hình captcha toán học của bot, theo `docs/ke-hoach-v2/13-dac-ta-bot-moi.md` §13.2.2.
Máy chủ tương ứng: `src/televip/apps/web/miniapp.py`.

| File | Vai trò |
|---|---|
| `index.html` | Khung trang. Nạp `telegram-web-app.js`, `style.css`, `app.js`. |
| `app.js` | Toàn bộ logic: xin đề, gửi đáp án, hiển thị trạng thái, đóng Mini App. |
| `style.css` | Giao diện theo biến theme Telegram (`--tg-theme-*`), hợp cả nền sáng lẫn tối. |

Không thư viện ngoài, không CDN nào ngoài `https://telegram.org/js/telegram-web-app.js`.

## Đáp án nằm ở máy chủ, không nằm trong JS

Máy chủ sinh phép tính bằng `secrets`, giữ đáp án trong Redis và chấm bằng `GETDEL`
(đọc-và-xoá nguyên tử). `app.js` chỉ hiển thị chuỗi đề bài nhận được và gửi con số người
dùng gõ đi — mở "View source" không thấy được đáp án.

Bot cũ (`webapp/verify.html:137-163` của repo cũ) sinh phép tính bằng `Math.random()`
trong JS, giữ đáp án trong biến, tự chấm ở client rồi gửi lên cờ `math_correct: true`;
`api/app.py:338-343` **không đọc trường đó ở bất kỳ dòng nào**. Lớp chống bot ấy gây ma
sát cho 100% người thật và chặn được 0% bot.

`tests/test_webapp_assets.py` khoá tính chất này lại: test đỏ nếu `app.js` sinh phép
tính ở client hoặc nạp thêm tài nguyên ngoài.

## Hợp đồng API

### `POST /api/challenge`

Gọi **không kèm body** — `create_challenge()` không nhận tham số.

```jsonc
// 200
{ "challenge_id": "9f3a…-uuid", "question": "1 + 9 = ?" }
```

Trang chấp nhận cả `"1 + 9 = ?"` lẫn biểu thức trần `"1 + 9"`; phần ` = ?` được dựng lại
ở client nên không bao giờ ra `1 + 9 = ? = ?`.

> Endpoint này cố ý không nhận `initData`, và đó là lựa chọn đúng:
> `Telegram.WebApp.initData` là một chuỗi **cố định suốt phiên Mini App**, nên nếu nó
> cũng tiêu vé chống-phát-lại thì `/api/verify` ngay sau đó luôn ăn `replay`.

### `POST /api/verify`

```jsonc
// request — `answer` là SỐ (VerifyIn khai `answer: int`)
{ "init_data": "<Telegram.WebApp.initData>", "challenge_id": "9f3a…", "answer": 10 }

// 200 — xác minh xong
{ "ok": true }

// 200 — đã xác minh từ trước (KHÔNG phải lỗi)
{ "ok": false, "error": "already_verified", "message": "…" }

// lỗi
{ "ok": false, "error": "<mã>", "message": "…" }
```

Trang coi `ok === true` **và** `already_verified` đều là "xong": hiện màn hình thành
công rồi gọi `Telegram.WebApp.close()` sau 1,5 giây.

### Mã lỗi

Trang đọc `error` trong body; thiếu `error` thì suy ra từ HTTP status
(400/422 → `invalid_request`, 401 → `unauthorized`, 403 → `invalid_signature`,
429 → `rate_limited`, ≥500 → `server_error`).

| `error` | HTTP | Trang hiển thị | Form |
|---|---|---|---|
| `already_verified` | 200 | `✅ Bạn đã xác thực rồi!` → đóng app | — |
| `wrong_answer` | 400 | `✗ Sai rồi!` + `❌ Kết quả không đúng, thử lại nhé!` | mở, **kèm đề mới** |
| `challenge_expired` | 400 | Báo hết hạn rồi **tự xin đề mới** | mở |
| `invalid_signature` | 403 | Đóng Mini App rồi mở lại từ nút trong bot | khoá |
| `expired`, `replay` | 403 | Phiên xác minh đã hết hạn, mở lại Mini App | khoá |
| `rate_limited` | 429 | Câu đếm ngược của máy chủ (hoặc `retry_after` giây) | mở |
| `server_error` | ≥500 | Hệ thống đang bận, thử lại sau ít phút | mở |
| `network_error` | — | Không kết nối được máy chủ | nút **Thử lại** |

Hai mã dành sẵn cho khối chấm điểm rủi ro: `risk_blocked` và `user_banned` (403). Câu
hiển thị cố ý **chung chung** theo §13.2.2 — không nêu IP, không nêu @username của ai
khác — kèm nút `💬 Liên hệ CSKH` nếu body có `support_link` (URL http/https).

### Vì sao sai một lần là phải thay đề

`_consume_challenge()` dùng `GETDEL`, tức đề bị huỷ **dù đúng hay sai** (một đề = một
lần đoán). Cho nên sau mỗi câu trả lời sai, `app.js` xin ngay một đề mới thay vì cho gõ
lại vào `challenge_id` đã chết. Hằng `MAX_WRONG = 3` không còn dùng để thay đề nữa mà để
cảnh báo trước hạn mức `verify.max_attempts_per_min` (mặc định 5 lượt/phút) của máy chủ,
tránh cú 429 rơi xuống bất ngờ.

Nếu máy chủ gửi kèm đề mới ngay trong body lỗi (`{"challenge": {...}}`), trang dùng luôn
và bỏ qua một vòng mạng.

## Chạy thử cục bộ

Cách gọn nhất là để chính FastAPI phục vụ thư mục này — khi đó `televip-api-base` để
**rỗng** (cùng origin) và không cần CORS:

```python
app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")
```

Muốn xem riêng giao diện, không cần API:

```powershell
cd c:\Users\AdminPC\Documents\BOT_Telegram\televip\webapp
c:\Users\AdminPC\Documents\BOT_Telegram\televip\.venv\Scripts\python.exe -m http.server 8080
```

Mở `http://127.0.0.1:8080`. Ngoài Telegram, trang hiện băng *"Đang mở ngoài Telegram —
chỉ xem được giao diện"*, vẫn vẽ đủ mọi trạng thái nhưng `/api/verify` sẽ bị từ chối vì
`initData` rỗng — đúng như thiết kế.

Nếu API chạy ở cổng khác (ví dụ `8000`), sửa thẻ meta trong `index.html`:

```html
<meta name="televip-api-base" content="http://127.0.0.1:8000">
```

và bật CORS cho `http://127.0.0.1:8080` ở phía API. **Nhớ trả `content` về rỗng trước
khi deploy.**

Telegram chỉ mở Mini App qua **HTTPS**, nên để thử trong app thật cần một tunnel
(cloudflared / ngrok) trỏ vào cổng trên, rồi dùng URL https đó ở bước dưới.

## Trỏ BotFather → Menu Button

1. Chat với [@BotFather](https://t.me/BotFather) → `/mybots` → chọn bot.
2. **Bot Settings → Menu Button → Configure menu button**.
3. Dán URL https của Mini App (ví dụ `https://ten-mien.example/webapp/`).
4. Nhập nhãn nút, ví dụ `🤖 Xác minh`.

Nhanh hơn: `/setmenubutton` rồi làm theo hai bước hỏi URL và nhãn.

Đường đi chính thức trong bot **không phải** menu button mà là nút inline
`🤖 Xác minh ngay` (§13.2.2), lấy URL từ khoá cấu hình `webapp.url` trong bảng
`settings`. Đổi URL thì đổi ở đó — menu button chỉ là lối vào phụ.

> `Telegram.WebApp.sendData()` **không** được dùng ở đây, và cũng không dùng được: nó
> chỉ chạy với `KeyboardButton(web_app=…)`, còn mọi nút xác minh trong §13.3.3 đều là
> inline. Đường đưa người dùng sang BƯỚC 2 là `/api/verify` bước 7 — đẩy tin qua
> `outbox_messages` (§13.2.15).
