/** Cấu hình Tailwind cho panel quản trị.
 *
 * Bảng màu `primary` chép đúng từ source tham chiếu (BotTelegramCSKH) — thang sky blue của
 * Tailwind. Giữ nguyên số hiệu để mọi lớp `primary-600`, `primary-50`… trong template có
 * cùng nghĩa với source đó, và người đã quen giao diện kia không phải học lại gì.
 *
 * `content` chỉ quét thư mục template của panel: Tailwind sinh CSS theo class NÓ TÌM THẤY,
 * nên bỏ sót đường dẫn ở đây nghĩa là một trang mất sạch định dạng mà không có lỗi nào báo.
 */
module.exports = {
  content: ["../../src/televip/apps/adminweb/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        primary: {
          50: "#f0f9ff",
          100: "#e0f2fe",
          200: "#bae6fd",
          300: "#7dd3fc",
          400: "#38bdf8",
          500: "#0ea5e9",
          600: "#0284c7",
          700: "#0369a1",
          800: "#075985",
          900: "#0c4a6e",
        },
      },
    },
  },
  plugins: [],
};
