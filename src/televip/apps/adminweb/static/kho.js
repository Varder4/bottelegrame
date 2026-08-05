// Lọc ô "Mệnh giá" theo "Loại mã" đang chọn trên màn hình kho.
//
// Thuần TIỆN NGHI. Máy chủ vẫn kiểm lại cả hai bằng `stock.kiem_lo_nap()`, nên tệp này
// không tải được, tải chậm, hay bị chặn thì lô sai mệnh giá vẫn bị từ chối — chỉ là người
// vận hành biết muộn hơn một nhịp. Đừng chuyển hàng rào nào vào đây.
//
// Danh sách mệnh giá dựng sẵn trong HTML kèm `data-loai`; ở đây chỉ ẩn/hiện.
(function () {
  "use strict";

  var oLoai = document.getElementById("loai");
  var oMenhGia = document.getElementById("menh_gia");
  if (!oLoai || !oMenhGia) return;

  // Giữ bản gốc: `<option>` bị gỡ khỏi DOM thì không lấy lại được khi đổi loại.
  var tatCa = Array.prototype.slice.call(oMenhGia.options);

  function loc() {
    var loai = oLoai.value;
    var conLai = tatCa.filter(function (o) {
      return o.dataset.loai === loai;
    });

    oMenhGia.textContent = "";
    conLai.forEach(function (o) {
      oMenhGia.appendChild(o);
    });

    // Loại không có mệnh giá nào hợp lệ là cấu hình hỏng, không phải trạng thái bình
    // thường — nói ra thay vì để ô rỗng im lặng.
    if (conLai.length === 0) {
      var trong = document.createElement("option");
      trong.textContent = "(loại này chưa có mệnh giá nào được cấu hình)";
      trong.disabled = true;
      oMenhGia.appendChild(trong);
    }
  }

  oLoai.addEventListener("change", loc);
  loc();
})();
