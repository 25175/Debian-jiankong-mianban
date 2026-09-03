#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TOKEN_PATH="$BASE_DIR/control-token"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_PATH="$SERVICE_DIR/jiankong.service"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/jiankong"
STATE_KEY="$(printf '%s' "$BASE_DIR" | cksum | cut -d' ' -f1)"
STATE_PATH="$STATE_DIR/$STATE_KEY.state"

token_fingerprint() {
  [[ -s "$TOKEN_PATH" ]] || return 0
  cksum < "$TOKEN_PATH" | cut -d' ' -f1
}

repository_token_fingerprint() {
  if command -v git >/dev/null 2>&1 && git -C "$BASE_DIR" cat-file -e HEAD:control-token 2>/dev/null; then
    git -C "$BASE_DIR" show HEAD:control-token 2>/dev/null | cksum | cut -d' ' -f1
  fi
}

print_token() {
  chmod 600 "$TOKEN_PATH"
  printf '\n控制令牌（请保存，仅显示本次）：\n%s\n\n' "$(cat "$TOKEN_PATH")"
}

write_default_token() {
  mkdir -p "$STATE_DIR"
  umask 077
  # Fresh installs use the requested default. It remains local-only (600).
  printf '1\n' > "$TOKEN_PATH"
  chmod 600 "$TOKEN_PATH"
  printf 'token_fingerprint=%s\n' "$(token_fingerprint)" > "$STATE_PATH"
  print_token
}

generate_token() {
  mkdir -p "$STATE_DIR"
  umask 077
  python3 - <<'PY' > "$TOKEN_PATH"
import secrets
print(secrets.token_urlsafe(36))
PY
  chmod 600 "$TOKEN_PATH"
  printf 'token_fingerprint=%s\n' "$(token_fingerprint)" > "$STATE_PATH"
  print_token
}

ensure_first_use_token() {
  if [[ ! -s "$TOKEN_PATH" ]]; then
    printf '未发现控制令牌，使用默认控制令牌。\n'
    write_default_token
  elif [[ ! -s "$STATE_PATH" && "$(token_fingerprint)" == "$(repository_token_fingerprint)" ]]; then
    printf '检测到仓库自带令牌；这是本机首次安装，将使用默认控制令牌。\n'
    write_default_token
  elif [[ ! -s "$STATE_PATH" ]]; then
    mkdir -p "$STATE_DIR"
    printf '检测到已有本机令牌；保留现有令牌并建立本机状态标记。\n'
    printf 'token_fingerprint=%s\n' "$(token_fingerprint)" > "$STATE_PATH"
    chmod 600 "$TOKEN_PATH"
  else
    chmod 600 "$TOKEN_PATH"
    printf '已保留本机现有控制令牌，不会自动重新生成。\n'
  fi
}

install_service() {
  command -v python3 >/dev/null 2>&1 || { printf '错误：未找到 python3。\n' >&2; return 1; }
  command -v systemctl >/dev/null 2>&1 || { printf '错误：未找到 systemctl；此安装器需要 Debian/systemd。\n' >&2; return 1; }
  mkdir -p "$SERVICE_DIR"
  python3 - "$BASE_DIR" "$(command -v python3)" "$BASE_DIR/jiankong.service" "$SERVICE_PATH" <<'PY'
from pathlib import Path
import sys
base, python_bin, template, destination = sys.argv[1:]
text = Path(template).read_text()
text = text.replace('__INSTALL_DIR__', base).replace('__PYTHON_BIN__', python_bin)
Path(destination).write_text(text)
PY
  chmod 644 "$SERVICE_PATH"
  systemctl --user daemon-reload
  systemctl --user enable --now jiankong.service
  printf '已安装并启动 jiankong.service：%s\n' "$SERVICE_PATH"
}

restart_service() {
  command -v systemctl >/dev/null 2>&1 || { printf '错误：未找到 systemctl。\n' >&2; return 1; }
  systemctl --user restart jiankong.service
  printf '已重启 jiankong.service。\n'
}

show_status() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user status jiankong.service --no-pager || true
  else
    printf '未找到 systemctl；当前不是可执行 systemd 管理的环境。\n'
  fi
}

regenerate_token() {
  if [[ "${1:-}" != "--yes" ]]; then
    printf '这会立即使旧令牌失效，并重启后才对服务生效。确认重新生成？[y/N] '
    read -r answer
    [[ "$answer" =~ ^[Yy]$ ]] || { printf '已取消，旧令牌未改变。\n'; return 0; }
  fi
  generate_token
  if [[ -f "$SERVICE_PATH" ]]; then
    restart_service
  else
    printf '服务尚未安装；令牌已生成，安装服务后生效。\n'
  fi
}

usage() {
  cat <<'EOF'
用法：
  ./install.sh                         首次生成/显示令牌并进入菜单
  ./install.sh --install               保留现有令牌，安装或更新并启动服务
  ./install.sh --show-token            查看当前令牌
  ./install.sh --regenerate             确认后重新生成并显示令牌
  ./install.sh --regenerate --yes       非交互重新生成并显示令牌
  ./install.sh --restart               重启服务
  ./install.sh --status                查看服务状态
EOF
}

menu() {
  while true; do
    printf '\nDebian 服务监控面板管理\n'
    printf '目录：%s\n' "$BASE_DIR"
    printf '1) 安装/更新并启动服务（不改变现有令牌）\n'
    printf '2) 查看当前令牌\n'
    printf '3) 重新生成令牌（旧令牌立即失效）\n'
    printf '4) 重启服务\n'
    printf '5) 查看服务状态\n'
    printf '0) 退出\n'
    printf '请选择：[0-5] '
    read -r choice
    case "$choice" in
      1) install_service ;;
      2) print_token ;;
      3) regenerate_token ;;
      4) restart_service ;;
      5) show_status ;;
      0) return 0 ;;
      *) printf '无效选择。\n' ;;
    esac
  done
}

case "${1:-}" in
  "") ensure_first_use_token; menu ;;
  --install) ensure_first_use_token; install_service ;;
  --show-token) [[ -s "$TOKEN_PATH" ]] || { printf '错误：control-token 不存在。\n' >&2; exit 1; }; print_token ;;
  --regenerate) regenerate_token "${2:-}" ;;
  --restart) restart_service ;;
  --status) show_status ;;
  --help|-h) usage ;;
  *) usage; exit 2 ;;
esac