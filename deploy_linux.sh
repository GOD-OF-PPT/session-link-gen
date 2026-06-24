#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy Session Link Generator on a Linux server.
# It installs Python dependencies, creates a gunicorn systemd service,
# and exposes the app directly at http://SERVER_IP:PORT.

APP_NAME="${APP_NAME:-session-link-gen}"
SERVICE_NAME="${SERVICE_NAME:-session-link-gen}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5000}"
WORKERS="${WORKERS:-4}"
TIMEOUT="${TIMEOUT:-180}"
GENERATE_RETRIES="${GENERATE_RETRIES:-20}"
MAX_GENERATE_RETRIES="${MAX_GENERATE_RETRIES:-100}"
GENERATE_RETRY_DELAY="${GENERATE_RETRY_DELAY:-0.5}"

log() {
  printf '[%s] %s\n' "$APP_NAME" "$*"
}

fail() {
  printf '[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "Need root permission for systemd. Install sudo or run this script as root."
  fi
}

need_file() {
  [ -f "$APP_DIR/$1" ] || fail "Missing $APP_DIR/$1. Run this script from the project directory."
}

need_file "app.py"
need_file "core.py"
need_file "requirements.txt"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  fail "Python not found: $PYTHON_BIN"
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required")
print("Python", sys.version.split()[0])
PY

log "App dir: $APP_DIR"
log "Bind: $HOST:$PORT"
log "Workers: $WORKERS"

log "Creating virtualenv..."
"$PYTHON_BIN" -m venv "$VENV_DIR" || {
  fail "Failed to create venv. On Debian/Ubuntu, install it first: sudo apt install python3-venv"
}

log "Installing dependencies..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt" gunicorn

log "Checking Python imports..."
"$VENV_DIR/bin/python" - <<'PY'
import flask, requests, curl_cffi
import app
print("imports ok")
PY

if ! command -v systemctl >/dev/null 2>&1; then
  fail "systemctl not found. This script targets systemd-based Linux servers."
fi

if [ "$(id -u)" -eq 0 ]; then
  SERVICE_USER="${SERVICE_USER:-root}"
else
  SERVICE_USER="${SERVICE_USER:-$(id -un)}"
fi

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

log "Writing systemd service: $SERVICE_FILE"
run_root tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Session Link Generator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=PORT=$PORT
Environment=GENERATE_RETRIES=$GENERATE_RETRIES
Environment=MAX_GENERATE_RETRIES=$MAX_GENERATE_RETRIES
Environment=GENERATE_RETRY_DELAY=$GENERATE_RETRY_DELAY
ExecStart=$VENV_DIR/bin/gunicorn -w $WORKERS --timeout $TIMEOUT -b $HOST:$PORT app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

log "Starting service..."
run_root systemctl daemon-reload
run_root systemctl enable "$SERVICE_NAME"
run_root systemctl restart "$SERVICE_NAME"

sleep 2
if run_root systemctl is-active --quiet "$SERVICE_NAME"; then
  log "Service is running."
else
  run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  fail "Service failed to start. Check logs: journalctl -u $SERVICE_NAME -e"
fi

if command -v curl >/dev/null 2>&1; then
  log "Health check..."
  curl -fsS "http://127.0.0.1:$PORT/api/health" || {
    printf '\n' >&2
    fail "Health check failed"
  }
  printf '\n'
fi

SERVER_IP="${SERVER_IP:-}"
if [ -z "$SERVER_IP" ] && command -v hostname >/dev/null 2>&1; then
  SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [ -z "$SERVER_IP" ]; then
  SERVER_IP="<server-ip>"
fi

cat <<EOF

Deploy complete.

Access:
  http://$SERVER_IP:$PORT

Service commands:
  sudo systemctl status $SERVICE_NAME
  sudo systemctl restart $SERVICE_NAME
  sudo journalctl -u $SERVICE_NAME -f

If the page cannot be opened from your computer, allow TCP port $PORT in:
  1. the cloud provider security group
  2. the server firewall, for example: sudo ufw allow $PORT/tcp
EOF
