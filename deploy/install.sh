#!/usr/bin/env bash
# TwinStack Gate: installs the gate app on a society's mini PC (Ubuntu 24.04 LTS, no GPU needed).
#
#   sudo SOC_NAME="Green Valley Residencia" SOC_ADMIN_PASSWORD='choose-a-password' bash deploy/install.sh
#
# Optional: SOC_ADMIN_USER (default admin), GATE_PORT (default 8080),
#           TUNNEL_TOKEN (Cloudflare tunnel token, so admin and residents can open the app from outside the society),
#           ANPR_DEVICE=0 on a box with an NVIDIA GPU (installs the CUDA build of PyTorch).
# Running it again updates the code and keeps all data.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/twinstack-gate
ENV=/etc/twinstack-gate.env
PORT="${GATE_PORT:-8080}"
DEVICE="${ANPR_DEVICE:-cpu}"
RELEASE=https://github.com/twinstack-studio/anpr/releases/latest/download

echo "== 1/6 System packages"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-dev build-essential postgresql ffmpeg curl rsync

echo "== 2/6 Service account and database"
id gate >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/twinstack-gate --shell /usr/sbin/nologin gate
sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='gate'" | grep -q 1 || sudo -u postgres createuser gate
sudo -u postgres psql -tAc "select 1 from pg_database where datname='gate'" | grep -q 1 || sudo -u postgres createdb -O gate gate

echo "== 3/6 Application files"
mkdir -p "$APP"
rsync -a --delete --exclude .git --exclude .venv --exclude runtime --exclude datasets --exclude training \
      --exclude models --exclude 'samples/*' --exclude '*.log' "$SRC/" "$APP/"
mkdir -p "$APP/models" "$APP/runtime" "$APP/samples"
chown -R gate:gate "$APP"

echo "== 4/6 Python packages (takes 5-15 minutes the first time)"
sudo -u gate python3 -m venv "$APP/.venv"
PIP="sudo -u gate $APP/.venv/bin/pip install -q"
if [ "$DEVICE" = cpu ]; then
  $PIP torch torchvision --index-url https://download.pytorch.org/whl/cpu
else
  $PIP torch torchvision --index-url https://download.pytorch.org/whl/cu124
fi
$PIP -r "$APP/requirements.txt"
$PIP --no-deps fast-plate-ocr==1.1.0

echo "== 5/6 Models"
fetch() { [ -s "$APP/models/$1" ] || sudo -u gate curl -fL --retry 3 -o "$APP/models/$1" "$2"; }
fetch plate_detector.pt "$RELEASE/plate_detector.pt"
fetch pk_plate_ocr.keras "$RELEASE/pk_plate_ocr.keras"
if [ "$DEVICE" = cpu ]; then
  fetch yolo11n.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
else
  fetch yolo11m.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11m.pt
fi

echo "== 6/6 Settings and service"
if [ ! -f "$ENV" ]; then
  [ -n "${SOC_ADMIN_PASSWORD:-}" ] && [ ${#SOC_ADMIN_PASSWORD} -ge 8 ] || { echo "Set SOC_ADMIN_PASSWORD (8+ characters) for the first install."; exit 1; }
  umask 077
  cat > "$ENV" <<CONF
# TwinStack Gate settings. Restart after changes: sudo systemctl restart twinstack-gate
ANPR_DSN=postgresql:///gate?host=/var/run/postgresql
ANPR_DEVICE=$DEVICE
SOC_DEMO=0
SOC_NAME=${SOC_NAME:-Our Society}
SOC_ADMIN_USER=${SOC_ADMIN_USER:-admin}
SOC_ADMIN_PASSWORD=$SOC_ADMIN_PASSWORD
CONF
  chown root:gate "$ENV"; chmod 640 "$ENV"
fi

cat > /etc/systemd/system/twinstack-gate.service <<UNIT
[Unit]
Description=TwinStack Gate (society gate app)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=gate
WorkingDirectory=$APP
EnvironmentFile=$ENV
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/.venv/bin/uvicorn app.server:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now twinstack-gate
systemctl restart twinstack-gate

if [ -n "${TUNNEL_TOKEN:-}" ]; then
  if ! command -v cloudflared >/dev/null; then
    curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    dpkg -i /tmp/cloudflared.deb
  fi
  cloudflared service install "$TUNNEL_TOKEN" || true
fi

# nightly database backup, 14 days kept
cat > /etc/cron.daily/twinstack-gate-backup <<'CRON'
#!/bin/sh
mkdir -p /var/backups/twinstack-gate && chown postgres /var/backups/twinstack-gate
su postgres -c "pg_dump -Fc gate > /var/backups/twinstack-gate/gate-$(date +%F).dump"
find /var/backups/twinstack-gate -name '*.dump' -mtime +14 -delete
CRON
chmod 755 /etc/cron.daily/twinstack-gate-backup

IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Waiting for the app to start..."
for i in $(seq 1 60); do curl -fs "http://127.0.0.1:$PORT/" >/dev/null && break; sleep 2; done
systemctl is-active twinstack-gate
echo "Open http://$IP:$PORT on the guard PC or any phone on the society's Wi-Fi."
echo "Log in as ${SOC_ADMIN_USER:-admin}, then add houses, vehicles, guards and the gate camera (Cameras page)."
