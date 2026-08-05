#!/usr/bin/env bash
# Sao lưu database HK79. Chạy hằng ngày qua cron.
#
# Cài:
#   sudo cp deploy/sao-luu.sh /usr/local/bin/televip-sao-luu
#   sudo chmod +x /usr/local/bin/televip-sao-luu
#   sudo crontab -e
#   # rồi thêm dòng (3h sáng mỗi ngày):
#   0 3 * * * /usr/local/bin/televip-sao-luu >> /var/log/televip-saoluu.log 2>&1
#
# VÌ SAO CẦN: database này giữ KHO MÃ CHƯA PHÁT và SỔ CÁI chống phát trùng.
# Mất bảng sổ cái thì mọi mốc mời bạn đã phát sẽ được phát LẠI TỪ ĐẦU cho toàn
# bộ người dùng — đúng loại thất thoát mà cả hệ thống này sinh ra để chặn.

set -euo pipefail

THU_MUC="${TELEVIP_BACKUP_DIR:-/var/backups/televip}"
GIU_NGAY="${TELEVIP_BACKUP_KEEP_DAYS:-14}"
DB_NAME="${TELEVIP_DB_NAME:-televip}"
DB_USER="${TELEVIP_DB_USER:-televip}"

mkdir -p "$THU_MUC"
TEN="$THU_MUC/televip-$(date +%Y%m%d-%H%M%S).sql.gz"

# `--clean --if-exists` để bản sao lưu tự phục hồi được lên một database đã có dữ liệu.
pg_dump --username="$DB_USER" --dbname="$DB_NAME" --clean --if-exists \
    | gzip -9 > "$TEN"

# Sao lưu 0 byte là sao lưu KHÔNG CÓ GÌ — thà nổ ngay còn hơn phát hiện lúc cần phục hồi.
KICH_THUOC=$(stat -c%s "$TEN")
if [ "$KICH_THUOC" -lt 1024 ]; then
    echo "LỖI: bản sao lưu chỉ $KICH_THUOC byte — nghi ngờ hỏng: $TEN" >&2
    exit 1
fi

echo "$(date '+%F %T')  đã lưu  $TEN  ($((KICH_THUOC / 1024)) KB)"

# Dọn bản cũ.
find "$THU_MUC" -name 'televip-*.sql.gz' -mtime "+$GIU_NGAY" -delete

# ── Phục hồi (khi cần) ────────────────────────────────────────────────
#   systemctl stop televip-worker televip-miniapp televip-panel
#   gunzip -c /var/backups/televip/televip-XXXX.sql.gz | psql -U televip -d televip
#   systemctl start televip-worker televip-miniapp televip-panel
