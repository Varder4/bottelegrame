/** Cấu hình Tailwind cho panel quản trị.
 *
 * Chỉ quét thư mục template của panel: Tailwind sinh CSS theo class NÓ TÌM THẤY, nên bỏ
 * sót đường dẫn ở đây nghĩa là một trang mất sạch định dạng mà không có lỗi nào báo.
 */
module.exports = {
  content: ["../../src/televip/apps/adminweb/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
