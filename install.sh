#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_PATH="$SERVICE_DIR/jiankong.service"
mkdir -p "$SERVICE_DIR"

if [[ ! -s "$BASE_DIR/control-token" ]]; then
  umask 077
  python3 - <<'PY' > "$BASE_DIR/control-token"
import secrets
print(secrets.token_urlsafe(36))
PY
fi
chmod 600 "$BASE_DIR/control-token"

sed -e "s#__INSTALL_DIR__#$BASE_DIR#g" -e "s#__PYTHON_BIN__#$(command -v python3)#g" \
  "$BASE_DIR/jiankong.service" > "$SERVICE_PATH"
systemctl --user daemon-reload
systemctl --user enable --now jiankong.service
printf 'Installed jiankong.service from %s\n' "$BASE_DIR"