#!/usr/bin/env bash
# نصب / به‌روزرسانی مانیتور به‌عنوان سرویس systemd روی Debian/Ubuntu
#
#   sudo bash deploy/install_service.sh
#
# اجرای دوباره امن است: کد به‌روز می‌شود ولی تنظیمات و داده‌های سرور حفظ می‌شوند.
# برای جایگزین کردن تنظیمات سرور با فایل همین پوشه:  sudo FORCE_CONFIG=1 bash deploy/install_service.sh
set -euo pipefail

APP_DIR="/opt/monitor"
SERVICE_USER="monitorsvc"
SERVICE_NAME="monitor-service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$APP_DIR/venv/bin/python"

if [[ $EUID -ne 0 ]]; then
  echo "لطفاً با دسترسی root اجرا کنید:  sudo bash $0" >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "این اسکریپت برای Debian/Ubuntu (apt) نوشته شده است." >&2
  exit 1
fi

echo "==> [1/8] نصب پیش‌نیازهای سیستم (python3، venv)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates >/dev/null

echo "==> [2/8] کاربر سیستمی ایزوله ($SERVICE_USER، بدون امکان ورود)"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> [3/8] کپی برنامه به $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/webmonitor"
cp -r "$SRC_DIR/webmonitor" "$SRC_DIR/monitor_system.py" "$SRC_DIR/requirements.txt" "$SRC_DIR/README.md" "$APP_DIR/"
if [[ ! -f "$APP_DIR/monitor-config.json" || "${FORCE_CONFIG:-0}" == "1" ]]; then
  cp "$SRC_DIR/monitor-config.json" "$APP_DIR/"
else
  echo "    فایل monitor-config.json سرور حفظ شد (برای جایگزینی: FORCE_CONFIG=1)"
fi

echo "==> [4/8] محیط مجازی پایتون و کتابخانه‌ها"
[[ -x "$PY" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> [5/8] کتابخانه‌های سیستمی لازم برای مرورگر"
"$APP_DIR/venv/bin/patchright" install-deps chromium

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$APP_DIR/monitor-config.json"   # contains the bot token

echo "==> [6/8] نصب مرورگر برای کاربر سرویس"
# Installed into $APP_DIR/.cache (the service user's home), where the service can find it.
runuser -u "$SERVICE_USER" -- env HOME="$APP_DIR" "$APP_DIR/venv/bin/patchright" install chromium
if [[ "$(dpkg --print-architecture)" == "amd64" ]]; then
  # Real Google Chrome is the least detectable option; Chromium is used if this fails.
  "$APP_DIR/venv/bin/patchright" install chrome || echo "    Google Chrome نصب نشد؛ از Chromium استفاده می‌شود."
fi

echo "==> [7/8] بررسی فایل تنظیمات"
if ! runuser -u "$SERVICE_USER" -- env HOME="$APP_DIR" "$PY" "$APP_DIR/monitor_system.py" validate; then
  echo "!! فایل تنظیمات خطا دارد. اصلاح کنید و دوباره اجرا کنید:  sudo nano $APP_DIR/monitor-config.json" >&2
  exit 1
fi
if grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$APP_DIR/monitor-config.json" && ! grep -qs "TELEGRAM_BOT_TOKEN=" "$APP_DIR/.env"; then
  echo "!! توکن ربات تلگرام هنوز تنظیم نشده است؛ پیام‌ها فقط در لاگ نوشته می‌شوند."
  echo "   ویرایش: sudo nano $APP_DIR/monitor-config.json   سپس: sudo systemctl restart $SERVICE_NAME"
fi

echo "==> [8/8] نصب و اجرای سرویس systemd"
install -m 644 "$SRC_DIR/deploy/monitor-service.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"
sleep 5
systemctl --no-pager --lines=20 status "$SERVICE_NAME" || true

cat <<EOF

✅ نصب کامل شد.
   لاگ زنده:                 journalctl -u $SERVICE_NAME -f
   وضعیت سرویس:              systemctl status $SERVICE_NAME
   ری‌استارت بعد از تغییر تنظیمات:  sudo systemctl restart $SERVICE_NAME
   اجرای دستورات برنامه:      sudo -u $SERVICE_USER $PY $APP_DIR/monitor_system.py history
EOF
