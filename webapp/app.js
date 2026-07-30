/* ══════════════════════════════════════════════════════════════════
   Mini App xác minh — TELEVIP
   Đặc tả: docs/ke-hoach-v2/13-dac-ta-bot-moi.md §13.2.2
   Máy chủ:  src/televip/apps/web/miniapp.py

   LUẬT SỐ MỘT CỦA FILE NÀY: **đáp án không bao giờ có mặt ở đây.**
   Máy chủ sinh phép tính (POST /api/challenge), giữ đáp án trong Redis và tự chấm
   (POST /api/verify). Trang này chỉ hiển thị chuỗi đề bài nhận được rồi gửi con số
   người dùng gõ đi.

   Bot cũ làm ngược lại: `webapp/verify.html:137-163` sinh phép tính bằng Math.random()
   ngay trong JS, giữ đáp án đúng trong một biến, tự chấm ở client rồi gửi lên cờ
   `math_correct: true` — mà `api/app.py:338-343` không đọc trường đó ở bất kỳ dòng nào.
   Kết quả: lớp chống bot gây ma sát cho 100% người thật và chặn được 0% bot, vì xem
   source (hoặc gọi thẳng API) là biết đáp án.
   ══════════════════════════════════════════════════════════════════ */

'use strict';

(function () {
  var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;

  /**
   * Sai liên tiếp đủ ngần này lần thì cảnh báo thêm về hạn mức thử.
   * Máy chủ chỉ cho `verify.max_attempts_per_min` lượt mỗi phút (mặc định 5); báo trước
   * ở lần thứ 3 để cú 429 không rơi xuống bất ngờ.
   */
  var MAX_WRONG = 3;

  /** Chờ chừng này rồi đóng Mini App, đủ để người dùng đọc câu báo thành công. */
  var CLOSE_DELAY_MS = 1500;

  var el = {
    notice: document.getElementById('notice'),
    quiz: document.querySelector('.quiz'),
    question: document.getElementById('question'),
    form: document.getElementById('form'),
    answer: document.getElementById('answer'),
    submit: document.getElementById('submit'),
    submitLabel: document.getElementById('submitLabel'),
    status: document.getElementById('status'),
    statusTitle: document.getElementById('statusTitle'),
    statusText: document.getElementById('statusText'),
    statusLink: document.getElementById('statusLink')
  };

  var challengeId = null;
  var wrongCount = 0;
  var busy = false;
  var finished = false;
  var idleLabel = 'Kiểm tra';

  // ── Câu chữ ─────────────────────────────────────────────────────

  var TEXT = {
    loading: 'Đang lấy phép tính…',
    idle: 'Nhập kết quả phép tính rồi bấm Kiểm tra.',
    checking: 'Đang kiểm tra…',
    wrongTitle: '✗ Sai rồi!',
    wrongText: '❌ Kết quả không đúng, thử lại nhé!',
    wrongWarn: '\n\n⚠️ Sai nhiều lần liên tiếp sẽ phải chờ một lát mới thử tiếp được.',
    successTitle: '✅ Đúng rồi!',
    successText: '🎉 Kết quả đúng, bạn đã xác minh thành công!',
    alreadyTitle: '✅ Bạn đã xác thực rồi!',
    alreadyText: 'Tài khoản của bạn đã được xác minh trước đó. Quay lại bot để nhận CODE nhé!',
    outsideTelegram: 'Đang mở ngoài Telegram — chỉ xem được giao diện, không xác minh được. ' +
      'Hãy bấm nút xác minh trong bot.',
    needNumber: 'Vui lòng nhập một con số.',
    check: 'Kiểm tra',
    retry: 'Thử lại'
  };

  /**
   * Mã lỗi API → câu tiếng Việt.
   *
   * Bảy mã đầu là mã máy chủ thật sự trả (`_MSG` trong `miniapp.py`); phần còn lại là mã
   * trang tự suy ra khi không có body (`statusToCode`) hoặc khi hỏng mạng. Câu ở đây
   * hướng dẫn hành động cụ thể hơn `message` của máy chủ, nên được ưu tiên; `message`
   * chỉ dùng làm dự phòng cho mã lạ.
   *
   * Lưu ý về `risk_blocked`/`user_banned` (khối chấm điểm rủi ro sẽ dùng): §13.2.2 buộc
   * câu từ chối phải CHUNG CHUNG — không nêu IP, không nêu @username của ai khác.
   */
  var ERROR_TEXT = {
    invalid_signature: 'Phiên xác minh không hợp lệ. Hãy đóng Mini App rồi mở lại từ nút ' +
      'trong bot.',
    expired: 'Phiên xác minh đã hết hạn, mở lại Mini App.',
    replay: 'Phiên xác minh đã hết hạn, mở lại Mini App.',
    challenge_expired: 'Phép tính đã hết hạn. Đang lấy phép tính mới…',
    rate_limited: 'Bạn thao tác hơi nhanh. Vui lòng chờ {seconds} giây rồi thử lại.',
    initdata_missing: 'Không đọc được dữ liệu Telegram. Hãy mở Mini App bằng nút trong bot, ' +
      'đừng mở bằng trình duyệt.',
    invalid_request: 'Dữ liệu gửi lên không hợp lệ. Hãy đóng Mini App rồi mở lại từ nút trong bot.',
    unauthorized: 'Phiên xác minh không hợp lệ hoặc đã hết hạn. Hãy đóng Mini App rồi mở lại ' +
      'từ nút trong bot.',
    risk_blocked: 'Chưa xác minh được tài khoản này. Vui lòng liên hệ CSKH để được hỗ trợ.',
    user_banned: 'Tài khoản đang bị tạm khoá. Vui lòng liên hệ CSKH để được hỗ trợ.',
    server_error: 'Hệ thống đang bận. Vui lòng thử lại sau ít phút.',
    network_error: 'Không kết nối được máy chủ. Kiểm tra kết nối mạng rồi thử lại.',
    unknown_error: 'Có lỗi không xác định. Vui lòng thử lại.'
  };

  /** Lỗi mà bấm lại cũng vô ích: phải mở lại Mini App hoặc liên hệ CSKH. */
  var FATAL_CODES = [
    'invalid_signature', 'expired', 'replay', 'initdata_missing',
    'invalid_request', 'unauthorized', 'risk_blocked', 'user_banned'
  ];

  // ── Lớp gọi API ─────────────────────────────────────────────────

  function ApiError(code, payload) {
    this.name = 'ApiError';
    this.code = code;
    this.payload = payload || {};
    this.message = code;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  var API_BASE = (function () {
    var meta = document.querySelector('meta[name="televip-api-base"]');
    return (meta ? String(meta.content || '') : '').replace(/\/+$/, '');
  })();

  function statusToCode(httpStatus) {
    if (httpStatus === 400 || httpStatus === 422) return 'invalid_request';
    if (httpStatus === 401) return 'unauthorized';
    if (httpStatus === 403) return 'invalid_signature';
    if (httpStatus === 429) return 'rate_limited';
    if (httpStatus >= 500) return 'server_error';
    return 'unknown_error';
  }

  function postJson(path, body) {
    var init = { method: 'POST' };
    if (body) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    return fetch(API_BASE + path, init).catch(function () {
      // fetch chỉ reject khi hỏng mạng/CORS — chưa có response để đọc mã lỗi.
      throw new ApiError('network_error', null);
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        // `already_verified` về HTTP 200 kèm `ok:false`, nên phải xét cả hai điều kiện.
        if (!response.ok || data.ok === false) {
          throw new ApiError(data.error || statusToCode(response.status), data);
        }
        return data;
      });
    });
  }

  function initData() {
    return (tg && tg.initData) ? tg.initData : '';
  }

  // ── Vẽ giao diện ────────────────────────────────────────────────

  function setStatus(kind, title, text) {
    el.status.hidden = false;
    el.status.className = 'status is-' + kind;
    el.statusTitle.textContent = title || '';
    el.statusTitle.hidden = !title;
    el.statusText.textContent = text || '';
  }

  function setSupportLink(url) {
    var ok = typeof url === 'string' && /^https?:\/\//i.test(url);
    el.statusLink.hidden = !ok;
    if (ok) el.statusLink.href = url;
  }

  function setBusy(value) {
    busy = value;
    el.submit.classList.toggle('is-busy', value);
    el.submitLabel.textContent = value ? TEXT.checking : idleLabel;
  }

  function setFormEnabled(enabled) {
    el.answer.disabled = !enabled;
    el.submit.disabled = !enabled;
  }

  /**
   * Máy chủ hiện trả đề đã kèm sẵn ` = ?` (`miniapp.py`), nhưng trang tự dựng lại phần
   * đó từ biểu thức trần để một ngày nào đó máy chủ bỏ đuôi ấy đi thì màn hình vẫn đúng,
   * và để không bao giờ ra `1 + 9 = ? = ?`.
   */
  function renderQuestion(raw) {
    var expr = String(raw == null ? '' : raw).trim().replace(/\s*=\s*\??\s*$/, '');
    return expr + ' = ?';
  }

  function haptic(type) {
    if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) {
      tg.HapticFeedback.notificationOccurred(type);
    }
  }

  function focusAnswer() {
    // Desktop bật được bàn phím ngay; Telegram trên mobile thường chặn autofocus, không sao.
    try {
      el.answer.focus({ preventScroll: true });
    } catch (e) {
      el.answer.focus();
    }
  }

  // ── Luồng ───────────────────────────────────────────────────────

  /**
   * Xin một phép tính mới. `after` (tuỳ chọn) là trạng thái hiển thị sau khi có đề —
   * dùng để giữ nguyên câu "✗ Sai rồi!" trong lúc thay đề ngầm bên dưới.
   *
   * Cố ý KHÔNG gửi `initData`: endpoint này không nhận danh tính (`create_challenge()`
   * không có tham số). Đó là lựa chọn đúng — `initData` là một chuỗi cố định suốt phiên
   * Mini App, tiêu vé chống-phát-lại ở đây thì `/api/verify` ngay sau đó luôn bị từ chối.
   */
  function loadChallenge(after) {
    challengeId = null;
    idleLabel = TEXT.check;
    setFormEnabled(false);
    setSupportLink(null);
    el.quiz.classList.add('is-loading');
    if (!after) {
      el.question.textContent = '…';
      setStatus('loading', '', TEXT.loading);
    }

    return postJson('/api/challenge', null).then(function (data) {
      challengeId = data.challenge_id || null;
      el.quiz.classList.remove('is-loading');
      el.question.textContent = renderQuestion(data.question);
      el.answer.value = '';
      setFormEnabled(true);
      if (after) {
        setStatus(after.kind, after.title, after.text);
      } else {
        setStatus('idle', '', TEXT.idle);
      }
      focusAnswer();
    }).catch(function (err) {
      el.quiz.classList.remove('is-loading');
      el.question.textContent = '—';
      showError(err);
    });
  }

  function onSubmit(event) {
    event.preventDefault();
    if (busy || finished) return;

    // Chưa có đề (lần nạp đầu hỏng) → nút này là nút "Thử lại".
    if (!challengeId) {
      setBusy(true);
      loadChallenge().then(function () {
        setBusy(false);
      });
      return;
    }

    var raw = el.answer.value.trim();
    if (!/^-?\d{1,4}$/.test(raw)) {
      setStatus('error', '', TEXT.needNumber);
      focusAnswer();
      return;
    }

    setBusy(true);
    setFormEnabled(false);
    setStatus('loading', '', TEXT.checking);

    postJson('/api/verify', {
      init_data: initData(),
      challenge_id: challengeId,
      // Máy chủ khai `answer: int`; gửi số để không phụ thuộc vào ép kiểu của pydantic.
      answer: Number(raw)
    }).then(function () {
      onCorrect();
      return null;
    }).catch(function (err) {
      if (err.code === 'already_verified') {
        onAlreadyVerified(err.payload);
        return null;
      }
      if (err.code === 'wrong_answer') {
        return onWrong(err.payload);
      }
      if (err.code === 'challenge_expired') {
        setStatus('error', '', ERROR_TEXT.challenge_expired);
        return loadChallenge();
      }
      showError(err);
      return null;
    }).then(function () {
      setBusy(false);
    });
  }

  function onWrong(payload) {
    wrongCount += 1;
    haptic('error');
    el.answer.value = '';

    var text = TEXT.wrongText + (wrongCount >= MAX_WRONG ? TEXT.wrongWarn : '');
    setStatus('wrong', TEXT.wrongTitle, text);

    // Máy chủ chấm bằng GETDEL: đề bị huỷ dù đúng hay sai (một đề = một lần đoán). Vậy
    // nên `challengeId` hiện tại đã chết — bắt buộc xin đề mới, không thể cho gõ lại vào
    // đề cũ. Nếu máy chủ gửi kèm đề mới trong body thì dùng luôn, đỡ một vòng mạng.
    var next = payload && payload.challenge;
    if (next && next.challenge_id) {
      challengeId = next.challenge_id;
      el.question.textContent = renderQuestion(next.question);
      setFormEnabled(true);
      focusAnswer();
      return null;
    }
    return loadChallenge({ kind: 'wrong', title: TEXT.wrongTitle, text: text });
  }

  function onCorrect() {
    finish(TEXT.successTitle, TEXT.successText);
  }

  function onAlreadyVerified(payload) {
    // HTTP 200 kèm `ok:false`: không phải lỗi của người dùng, chỉ là không có gì đổi.
    finish(TEXT.alreadyTitle, (payload && payload.message) || TEXT.alreadyText);
  }

  function finish(title, text) {
    finished = true;
    wrongCount = 0;
    haptic('success');
    setFormEnabled(false);
    setSupportLink(null);
    setStatus('success', title, text);
    // Đóng Mini App để người dùng quay lại chat. Tin "BƯỚC 2" do máy chủ đẩy qua
    // outbox_messages (§13.2.2 bước 7) — trang này không gửi gì cho bot.
    window.setTimeout(function () {
      if (tg && tg.close) tg.close();
    }, CLOSE_DELAY_MS);
  }

  function showError(err) {
    var code = (err && err.code) ? err.code : 'unknown_error';
    var payload = (err && err.payload) ? err.payload : {};
    var text = ERROR_TEXT[code] || payload.message || ERROR_TEXT.unknown_error;

    if (code === 'rate_limited') {
      // Máy chủ đã đếm ngược sẵn trong `message`; chỉ tự dựng câu khi nó không nói gì.
      text = payload.message || text.replace(
        '{seconds}',
        String(Math.max(1, Math.ceil(Number(payload.retry_after) || 5)))
      );
    }

    haptic('error');
    setStatus('error', '', text);
    setSupportLink(payload.support_link);

    if (FATAL_CODES.indexOf(code) !== -1) {
      idleLabel = TEXT.check;
      setFormEnabled(false);
      return;
    }
    if (challengeId) {
      idleLabel = TEXT.check;
      setFormEnabled(true);
      return;
    }
    // Lỗi tạm thời mà chưa có đề: mở riêng nút để xin lại đề, ô nhập vẫn khoá.
    idleLabel = TEXT.retry;
    el.answer.disabled = true;
    el.submit.disabled = false;
    el.submitLabel.textContent = TEXT.retry;
  }

  // ── Khởi động ───────────────────────────────────────────────────

  function boot() {
    if (tg) {
      tg.ready();
      tg.expand();
    } else {
      el.notice.textContent = TEXT.outsideTelegram;
      el.notice.hidden = false;
    }

    el.form.addEventListener('submit', onSubmit);
    loadChallenge();
  }

  boot();
})();
