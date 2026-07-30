"""Kiểm tra tài sản tĩnh của Mini App (`webapp/`).

Dự án không có test runner JavaScript, và thêm hẳn một cái chỉ để chạy vài phép kiểm
là không đáng. Nhưng ba tính chất của thư mục `webapp/` là **luật**, không phải chuyện
thẩm mỹ, nên chúng được khoá bằng test Python đọc thẳng file:

1. **Đáp án không nằm trong JavaScript.** `webapp/verify.html:137-163` của bot cũ sinh
   phép tính bằng `Math.random()`, giữ đáp án đúng trong một biến JS rồi tự chấm ở
   client — trong khi `api/app.py:338-343` không đọc cờ `math_correct` ở bất kỳ dòng
   nào. Xem source là biết đáp án. Test này làm cho việc vô tình dựng lại kiểu đó thành
   một lỗi đỏ chứ không phải một nhận xét trong code review.
2. **Đúng một tài nguyên ngoài.** Chỉ `telegram-web-app.js`. Mỗi CDN thêm vào là thêm
   một bên thứ ba chạy được JavaScript trên màn hình xác minh.
3. **Câu chữ trạng thái đúng như đặc tả** (§13.2.2), giống cách `domain/texts.py` giữ
   câu chữ phía bot — bot cũ có ba biến thể của cùng một nhãn nút vì mỗi nơi gõ lại.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1] / "webapp"

#: Ba file được phục vụ cho trình duyệt. README không nằm trong danh sách này vì nó là
#: tài liệu, có quyền chứa URL ví dụ.
SERVED_FILES = ("index.html", "app.js", "style.css")

TELEGRAM_SDK_URL = "https://telegram.org/js/telegram-web-app.js"


def read(name: str) -> str:
    return (WEBAPP_DIR / name).read_text(encoding="utf-8")


def strip_comments(js: str) -> str:
    """Bỏ chú thích khỏi JS để chỉ soi phần chạy thật.

    Cần thiết vì chính `app.js` có nhắc `Math.random()` và `math_correct` trong khối
    chú thích giải thích bot cũ đã sai ở đâu — nhắc trong chú thích thì được, dùng
    trong code thì không.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", js, flags=re.MULTILINE)


@pytest.fixture(scope="module")
def app_js() -> str:
    return read("app.js")


@pytest.fixture(scope="module")
def app_js_code(app_js: str) -> str:
    return strip_comments(app_js)


@pytest.fixture(scope="module")
def index_html() -> str:
    return read("index.html")


# ── Có mặt đủ file ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", [*SERVED_FILES, "README.md"])
def test_file_ton_tai(name: str) -> None:
    assert (WEBAPP_DIR / name).is_file(), f"thiếu webapp/{name}"


def test_index_tro_toi_dung_hai_file_cuc_bo(index_html: str) -> None:
    assert './style.css"' in index_html
    assert './app.js"' in index_html


# ── 1. Đáp án không nằm trong JavaScript ────────────────────────────


@pytest.mark.parametrize(
    "cam",
    [
        "Math.random",  # sinh đề ở client — đúng cách bot cũ làm
        "math_correct",  # cờ tự chấm mà server cũ không bao giờ đọc
        "eval(",
        "new Function",
    ],
)
def test_app_js_khong_tu_sinh_hay_tu_cham_de(app_js_code: str, cam: str) -> None:
    assert cam not in app_js_code, (
        f"`{cam}` xuất hiện trong app.js. Phép tính phải do máy chủ sinh và chấm; "
        "đưa đáp án về client là dựng lại đúng lỗ hổng của bot cũ."
    )


def test_app_js_gui_dap_an_len_may_chu(app_js_code: str) -> None:
    """Đáp án phải đi qua mạng — bằng chứng là client không tự kết luận đúng/sai."""
    assert "/api/challenge" in app_js_code
    assert "/api/verify" in app_js_code
    assert "challenge_id" in app_js_code
    assert "init_data" in app_js_code


def test_app_js_doc_ket_qua_tu_may_chu(app_js_code: str) -> None:
    """Kết luận đúng/sai đọc từ cờ `ok` của máy chủ, không tự tính ở client."""
    assert "data.ok === false" in app_js_code


def test_challenge_khong_gui_init_data(app_js_code: str) -> None:
    """`POST /api/challenge` phải gọi rỗng.

    `miniapp.py::create_challenge()` không nhận tham số, và đó là lựa chọn đúng:
    `Telegram.WebApp.initData` là một chuỗi CỐ ĐỊNH suốt phiên Mini App, nên nếu endpoint
    này cũng tiêu vé chống-phát-lại thì `/api/verify` ngay sau đó luôn ăn `replay`.
    """
    assert "postJson('/api/challenge', null)" in app_js_code


# ── 2. Đúng một tài nguyên ngoài ────────────────────────────────────


@pytest.mark.parametrize("name", SERVED_FILES)
def test_chi_mot_tai_nguyen_ngoai(name: str) -> None:
    urls = set(re.findall(r"https?://[^\s\"'<>()]+", read(name)))
    assert urls <= {TELEGRAM_SDK_URL}, (
        f"webapp/{name} nạp tài nguyên ngoài lạ: {sorted(urls - {TELEGRAM_SDK_URL})}"
    )


def test_index_nap_sdk_telegram_dong_bo(index_html: str) -> None:
    # Phải đồng bộ và đứng trước app.js, nếu không `window.Telegram` chưa tồn tại lúc
    # app.js chạy.
    sdk = index_html.index(TELEGRAM_SDK_URL)
    app = index_html.index("./app.js")
    assert sdk < app
    tag = index_html[index_html.rindex("<script", 0, sdk) : sdk]
    assert "defer" not in tag and "async" not in tag


# ── 3. Câu chữ và vòng đời theo §13.2.2 ─────────────────────────────


@pytest.mark.parametrize(
    "cau",
    [
        "✗ Sai rồi!",
        "❌ Kết quả không đúng, thử lại nhé!",
        "✅ Đúng rồi!",
        "🎉 Kết quả đúng, bạn đã xác minh thành công!",
    ],
)
def test_cau_chu_trang_thai(app_js: str, cau: str) -> None:
    assert cau in app_js


def test_vong_doi_telegram(app_js: str) -> None:
    for goi in ("tg.ready()", "tg.expand()", "tg.close()"):
        assert goi in app_js, f"thiếu {goi}"


def test_dong_mini_app_sau_khoang_mot_giay_ruoi(app_js: str) -> None:
    assert "CLOSE_DELAY_MS = 1500" in app_js


def test_nguong_canh_bao_sai_lien_tiep(app_js: str) -> None:
    assert "MAX_WRONG = 3" in app_js


def test_sai_dap_an_thi_xin_de_moi(app_js_code: str) -> None:
    """Sai là phải thay đề, không cho gõ lại vào đề cũ.

    `miniapp.py::_consume_challenge()` chấm bằng `GETDEL` — đề bị huỷ dù đúng hay sai,
    nên `challenge_id` vừa dùng đã chết. Gửi lại nó chỉ nhận về `challenge_expired`.
    """
    onwrong = re.search(r"function onWrong\(payload\) \{(.*?)\n  \}", app_js_code, re.DOTALL)
    assert onwrong is not None
    assert "loadChallenge(" in onwrong.group(1)


def test_moi_ma_loi_deu_co_cau_tieng_viet(app_js_code: str) -> None:
    """Mọi mã lỗi trang biết nhận diện đều phải có câu hiển thị tương ứng.

    Đây là yêu cầu "thông báo lỗi rõ ràng tiếng Việt cho từng mã lỗi": thêm một mã vào
    `statusToCode` hay `FATAL_CODES` mà quên câu chữ thì người dùng nhận được câu chung
    chung "lỗi không xác định".
    """
    block = re.search(r"var ERROR_TEXT = \{(.*?)\n  \};", app_js_code, re.DOTALL)
    assert block is not None, "không tìm thấy bảng ERROR_TEXT trong app.js"
    co_cau = set(re.findall(r"^\s{4}(\w+):", block.group(1), re.MULTILINE))

    fatal = re.search(r"var FATAL_CODES = \[(.*?)\];", app_js_code, re.DOTALL)
    assert fatal is not None, "không tìm thấy FATAL_CODES trong app.js"

    tu_http = set(re.findall(r"return '(\w+)';", app_js_code))
    tu_fatal = set(re.findall(r"'(\w+)'", fatal.group(1)))
    can_co = tu_http | tu_fatal

    assert can_co <= co_cau, f"mã lỗi chưa có câu tiếng Việt: {sorted(can_co - co_cau)}"


# ── Giao diện theo theme Telegram ───────────────────────────────────


def test_css_dung_bien_theme_telegram() -> None:
    css = read("style.css")
    for bien in (
        "--tg-theme-bg-color",
        "--tg-theme-text-color",
        "--tg-theme-hint-color",
        "--tg-theme-button-color",
        "--tg-theme-button-text-color",
        "--tg-theme-secondary-bg-color",
    ):
        assert bien in css, f"thiếu biến theme {bien}"
    # Có nhánh nền tối cho trường hợp mở ngoài Telegram (không có biến theme nào).
    assert "prefers-color-scheme: dark" in css
