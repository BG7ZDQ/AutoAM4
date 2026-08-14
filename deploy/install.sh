#!/usr/bin/env bash
# AM4 机队中心 Linux 一键安装脚本（Ubuntu / Debian）
# 用法：sudo bash deploy/install.sh
#
# 功能：
#   1. 交互询问安装目录、服务用户、域名、HTTPS 方式、游戏账号（可选）等参数
#   2. 自动生成 .env 配置、初始化令牌、会话密钥与服务令牌
#   3. 创建虚拟环境并安装依赖
#   4. 生成 Nginx 反向代理配置，支持 Let's Encrypt / 自签名 / 仅 HTTP
#   5. 写入 systemd 单元并开机自启
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

if [[ $EUID -ne 0 ]]; then
  echo "请以 root 运行：sudo bash deploy/install.sh" >&2
  exit 1
fi
if [[ ! -f "$REPO_DIR/src/server.py" ]]; then
  echo "找不到项目源码（src/server.py），请在仓库根目录运行本脚本。" >&2
  exit 1
fi

# ---------------------------------------------------------------- 工具函数
gen_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

ask() { # ask 变量名 提示 [默认值]
  local var="$1" prompt="$2" default="${3:-}" value=""
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value
    value="${value:-$default}"
  else
    read -r -p "$prompt: " value
  fi
  printf -v "$var" '%s' "$value"
}

ask_secret() { # ask_secret 变量名 提示
  local var="$1" prompt="$2" value=""
  read -r -s -p "$prompt: " value
  echo
  printf -v "$var" '%s' "$value"
}

# ---------------------------------------------------------------- 交互参数
APP_DIR="/opt/am4/app"
SVC_USER="am4"
DOMAIN=""
ADMIN_EMAIL=""
HTTPS_MODE="certbot"
AM4_EMAIL=""
AM4_PASSWORD=""
ADMIN_USER=""
ADMIN_PASS=""
PROTECTED=""
SSE_CLIENTS=6
MAX_LOOPS=3
PORT=5000

ask APP_DIR "应用安装目录" "$APP_DIR"
if [[ "$APP_DIR" != /* ]]; then
  echo "应用安装目录必须是绝对路径（如 /opt/am4/app）" >&2
  exit 1
fi
BASE_DIR="$(dirname "$APP_DIR")"
ask SVC_USER "系统服务用户" "$SVC_USER"
if ! [[ "$SVC_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "服务用户名只能包含小写字母、数字、_、-，且以字母或 _ 开头" >&2
  exit 1
fi

while [[ -z "$DOMAIN" ]]; do
  ask DOMAIN "面板域名（如 am4.example.com）"
  if [[ ! "$DOMAIN" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]+$ || "$DOMAIN" != *.* ]]; then
    echo "域名格式不正确" >&2
    DOMAIN=""
  fi
done

echo
echo "HTTPS 方式："
echo "  1) Let's Encrypt（certbot，需要域名已解析到本机）"
echo "  2) 自签名证书（浏览器会提示不安全）"
echo "  3) 仅 HTTP（仅建议内网使用）"
read -r -p "请选择 [1]: " https_choice
case "${https_choice:-1}" in
  1) HTTPS_MODE="certbot" ;;
  2) HTTPS_MODE="selfsigned" ;;
  3) HTTPS_MODE="http" ;;
  *) HTTPS_MODE="certbot" ;;
esac
if [[ "$HTTPS_MODE" == "certbot" ]]; then
  ask ADMIN_EMAIL "证书续期通知邮箱（可留空）"
fi

echo
ask AM4_EMAIL "AM4 游戏账号邮箱（可留空，仅使用面板功能）"
if [[ -n "$AM4_EMAIL" ]]; then
  while [[ -z "$AM4_PASSWORD" ]]; do
    ask_secret AM4_PASSWORD "AM4 游戏账号密码"
  done
fi

read -r -p "是否现在自动创建管理员账号？[y/N]: " want_admin
if [[ "${want_admin,,}" == "y" ]]; then
  while true; do
    ask ADMIN_USER "管理员用户名（2~8 位字母/数字，可含空格 _ /，头尾勿空格）"
    if [[ ${#ADMIN_USER} -lt 2 || ${#ADMIN_USER} -gt 8 ]] \
       || [[ "$ADMIN_USER" != "$(echo "$ADMIN_USER" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')" ]] \
       || ! grep -Eq '^[A-Za-z0-9 _/]+$' <<<"$ADMIN_USER"; then
      echo "用户名不符合规则" >&2
    else
      break
    fi
  done
  while true; do
    ask_secret ADMIN_PASS "管理员密码（至少 6 位）"
    if [[ ${#ADMIN_PASS} -lt 6 ]]; then
      echo "密码至少 6 位" >&2
    else
      break
    fi
  done
fi

ask PROTECTED "受保护账号（逗号分隔，可留空）"
ask SSE_CLIENTS "SSE 实时连接数上限" "$SSE_CLIENTS"
ask MAX_LOOPS "全局并发循环数上限" "$MAX_LOOPS"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "端口必须是 1024~65535 的整数" >&2
  exit 1
fi
if ! [[ "$SSE_CLIENTS" =~ ^[0-9]+$ ]] || (( SSE_CLIENTS < 1 || SSE_CLIENTS > 1000 )); then
  echo "SSE 连接数必须是 1~1000 的整数" >&2
  exit 1
fi
if ! [[ "$MAX_LOOPS" =~ ^[0-9]+$ ]] || (( MAX_LOOPS < 1 || MAX_LOOPS > 100 )); then
  echo "并发循环数必须是 1~100 的整数" >&2
  exit 1
fi

SETUP_TOKEN="$(gen_token)"
COOKIE_SECURE=0
if [[ "$HTTPS_MODE" != "http" ]]; then
  COOKIE_SECURE=1
fi

echo
echo "即将安装："
echo "  目录:      $APP_DIR（服务用户 $SVC_USER）"
echo "  域名:      $DOMAIN（HTTPS 方式 $HTTPS_MODE）"
echo "  游戏账号:  ${AM4_EMAIL:-（未配置，仅面板）}"
echo "  管理员:    ${ADMIN_USER:-（稍后通过 /setup 创建）}"
read -r -p "确认开始？[y/N]: " confirm
if [[ "${confirm,,}" != "y" ]]; then
  echo "已取消"
  exit 0
fi

# ---------------------------------------------------------------- 依赖检查
MISSING=()
for cmd in python3 curl tar nginx; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done
python3 -m venv --help >/dev/null 2>&1 || MISSING+=("python3-venv")
if [[ "$HTTPS_MODE" == "certbot" ]] && ! command -v certbot >/dev/null 2>&1; then
  MISSING+=("certbot" "python3-certbot-nginx")
fi
if [[ "$HTTPS_MODE" == "selfsigned" ]] && ! command -v openssl >/dev/null 2>&1; then
  MISSING+=("openssl")
fi
if [[ ${#MISSING[@]} -gt 0 ]]; then
  if [[ -f /etc/debian_version ]]; then
    read -r -p "缺少依赖（${MISSING[*]}），是否用 apt 安装？[y/N]: " want_apt
    if [[ "${want_apt,,}" != "y" ]]; then
      echo "请先安装依赖后重试" >&2
      exit 1
    fi
    apt-get update
    apt-get install -y "${MISSING[@]}"
  else
    echo "请先手动安装依赖：${MISSING[*]}" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------- 目录与用户
if [[ -e "$APP_DIR" ]] && [[ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
  echo "目标目录已存在且非空：$APP_DIR。请先备份并移除后重试。" >&2
  exit 1
fi
mkdir -p "$BASE_DIR" "$APP_DIR" "$BASE_DIR/tmp"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home "$BASE_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

# ---------------------------------------------------------------- 复制源码
# 排除开发态与机密数据：数据库、输出、本地 .env 与令牌都在服务器上重新生成。
tar -C "$REPO_DIR" \
  --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='.env' --exclude='deploy/.env.production' \
  --exclude='data' --exclude='outputs' \
  --exclude='src/.csrf_token' --exclude='src/.session_secret' --exclude='src/.service_token' \
  -cf - . | tar -C "$APP_DIR" -xf -

# ---------------------------------------------------------------- 密钥与令牌
umask 077
mkdir -p "$APP_DIR/src" "$APP_DIR/data"
# 已有密钥不覆盖：升级时保留会话与服务令牌，避免把在线用户全部登出
[[ -s "$APP_DIR/src/.session_secret" ]] || printf '%s\n' "$(gen_token)" > "$APP_DIR/src/.session_secret"
[[ -s "$APP_DIR/src/.service_token" ]] || printf '%s\n' "$(gen_token)" > "$APP_DIR/src/.service_token"

# 用 printf 逐行写入：值只做一次 shell 展开，$、反引号等字符按原样落盘，
# 避免密码/输入中的 $(...) 被再次求值造成命令注入。
{
  printf '%s\n' "# 由 deploy/install.sh 生成；包含凭据与令牌，仅服务用户可读"
  if [[ -n "$AM4_EMAIL" ]]; then
    printf '%s\n' "" "# ---- AM4 游戏账号 ----" \
      "AM4_EMAIL=$AM4_EMAIL" "AM4_PASSWORD=$AM4_PASSWORD"
  fi
  printf '%s\n' "" "# ---- 运营参数（可在面板「设置」按账号覆盖）----" \
    "AM4_COST_INDEX=200" "AM4_MIN_FUEL=200000" \
    "AM4_CASH_RESERVE=5000000" "AM4_MAX_RESOURCE_SPEND=25000000" \
    "AM4_FUEL_BUY_BELOW=500" "AM4_CO2_BUY_BELOW=125" \
    "AM4_MIN_A_CHECK_HOURS=5" "AM4_MAX_WEAR_FOR_TAKEOFF=80" \
    "AM4_AUTO_MARKETING=1" "AM4_AUTO_BUY_FUEL=1" \
    "AM4_AUTO_BUY_CO2=1" "AM4_AUTO_TAKEOFF=1" \
    "" "# ---- 服务与安全 ----" \
    "AM4_COOKIE_SECURE=$COOKIE_SECURE" "AM4_TRUST_PROXY=1" \
    "AM4_MAX_CONCURRENT_LOOPS=$MAX_LOOPS" "AM4_MAX_SSE_CLIENTS=$SSE_CLIENTS" \
    "AM4_DEBUG_TEMPLATES=0" "AM4_DISABLE_SCHEDULER=0" \
    "AM4_PROTECTED_ACCOUNTS=$PROTECTED" \
    "" "# ---- 面板初始化 ----" \
    "AM4_SETUP_TOKEN=$SETUP_TOKEN"
  if [[ -n "$ADMIN_USER" ]]; then
    printf '%s\n' "AM4_ADMIN_USERNAME=$ADMIN_USER" "AM4_ADMIN_PASSWORD=$ADMIN_PASS"
  fi
} > "$APP_DIR/.env"
umask 022

chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data" "$BASE_DIR/tmp"
chown "$SVC_USER":"$SVC_USER" "$BASE_DIR/tmp"
chmod 600 "$APP_DIR/.env" "$APP_DIR/src/.session_secret" "$APP_DIR/src/.service_token"

# ---------------------------------------------------------------- Python 环境
echo "创建虚拟环境并安装依赖……"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR/.venv"

# ---------------------------------------------------------------- Nginx
write_proxy_block() {
  cat <<'NGINXPROXY'
        proxy_pass http://127.0.0.1:PORT_PLACEHOLDER;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1d;
        proxy_send_timeout 1d;
NGINXPROXY
}

if [[ -d /etc/nginx/sites-available ]]; then
  NGINX_CONF="/etc/nginx/sites-available/am4.conf"
else
  NGINX_CONF="/etc/nginx/conf.d/am4.conf"
fi

write_http_server() {
  {
    echo "server {"
    echo "    listen 80;"
    echo "    listen [::]:80;"
    echo "    server_name $DOMAIN;"
    echo "    location / {"
    write_proxy_block | sed "s/PORT_PLACEHOLDER/$PORT/"
    echo "    }"
    echo "}"
  } > "$NGINX_CONF"
}

write_https_selfsigned() {
  mkdir -p /etc/ssl/am4
  chmod 700 /etc/ssl/am4
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "/etc/ssl/am4/$DOMAIN.key" -out "/etc/ssl/am4/$DOMAIN.crt" \
    -subj "/CN=$DOMAIN" -addext "subjectAltName=DNS:$DOMAIN" >/dev/null 2>&1
  chmod 600 "/etc/ssl/am4/$DOMAIN.key"
  {
    echo "server {"
    echo "    listen 80;"
    echo "    listen [::]:80;"
    echo "    server_name $DOMAIN;"
    echo "    return 301 https://\$host\$request_uri;"
    echo "}"
    echo "server {"
    echo "    listen 443 ssl http2;"
    echo "    listen [::]:443 ssl http2;"
    echo "    server_name $DOMAIN;"
    echo "    ssl_certificate /etc/ssl/am4/$DOMAIN.crt;"
    echo "    ssl_certificate_key /etc/ssl/am4/$DOMAIN.key;"
    echo "    ssl_protocols TLSv1.2 TLSv1.3;"
    echo "    location / {"
    write_proxy_block | sed "s/PORT_PLACEHOLDER/$PORT/"
    echo "    }"
    echo "}"
  } > "$NGINX_CONF"
}

case "$HTTPS_MODE" in
  http)
    write_http_server
    ;;
  selfsigned)
    write_https_selfsigned
    ;;
  certbot)
    write_http_server
    ;;
esac

if [[ "$NGINX_CONF" == /etc/nginx/sites-available/* ]]; then
  ln -sfn "$NGINX_CONF" /etc/nginx/sites-enabled/am4.conf
fi
nginx -t
systemctl reload nginx || systemctl restart nginx

if [[ "$HTTPS_MODE" == "certbot" ]]; then
  echo "申请 Let's Encrypt 证书……"
  CERTBOT_ARGS=(--nginx -d "$DOMAIN" --redirect -n --agree-tos)
  if [[ -n "$ADMIN_EMAIL" ]]; then
    CERTBOT_ARGS+=(-m "$ADMIN_EMAIL")
  else
    CERTBOT_ARGS+=(--register-unsafely-without-email)
  fi
  if ! certbot "${CERTBOT_ARGS[@]}"; then
    # 证书失败但服务仍要启动：回退 Secure Cookie 标记，否则 HTTPS-only Cookie
    # 在 HTTP 下不会发送，登录将完全不可用。
    COOKIE_SECURE=0
    sed -i 's/^AM4_COOKIE_SECURE=.*/AM4_COOKIE_SECURE=0/' "$APP_DIR/.env"
    echo "⚠ 证书签发失败，面板暂时以 HTTP 提供（已回退会话 Cookie 为兼容 HTTP）；" \
      "请确认域名解析后运行：certbot --nginx -d $DOMAIN，随后把 AM4_COOKIE_SECURE 改回 1" >&2
  fi
fi

# ---------------------------------------------------------------- systemd
{
  printf '%s\n' \
    "[Unit]" \
    "Description=AM4 fleet dashboard and scheduler" \
    "After=network-online.target" \
    "Wants=network-online.target" \
    "" \
    "[Service]" \
    "Type=simple" \
    "User=$SVC_USER" \
    "Group=$SVC_USER" \
    "WorkingDirectory=$APP_DIR" \
    "Environment=AM4_PORT=$PORT" \
    "Environment=TEMP=$BASE_DIR/tmp" \
    "Environment=PYTHONUNBUFFERED=1" \
    "Environment=AM4_COOKIE_SECURE=$COOKIE_SECURE" \
    "Environment=AM4_MAX_SSE_CLIENTS=$SSE_CLIENTS" \
    "Environment=AM4_TRUST_PROXY=1" \
    "ExecStart=$APP_DIR/.venv/bin/gunicorn --chdir $APP_DIR/src --bind 127.0.0.1:$PORT --workers 1 --threads 16 --timeout 900 --access-logfile - --error-logfile - server:app" \
    "ExecStartPost=$APP_DIR/.venv/bin/python $APP_DIR/deploy/start_loop.py" \
    "Restart=on-failure" \
    "RestartSec=10" \
    "TimeoutStartSec=45" \
    "TimeoutStopSec=30" \
    "KillMode=control-group" \
    "NoNewPrivileges=true" \
    "PrivateTmp=true" \
    "ProtectSystem=full" \
    "ReadWritePaths=$BASE_DIR" \
    "" \
    "[Install]" \
    "WantedBy=multi-user.target"
} > /etc/systemd/system/am4.service

systemctl daemon-reload
systemctl enable --now am4.service

# ---------------------------------------------------------------- 防火墙
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  echo "已放行 80/443 端口（ufw）"
fi

# ---------------------------------------------------------------- 完成
echo
echo "安装完成。"
if [[ "$HTTPS_MODE" == "http" ]]; then
  echo "面板地址: http://$DOMAIN"
else
  echo "面板地址: https://$DOMAIN"
fi
echo "查看状态: systemctl status am4"
echo "查看日志: journalctl -u am4 -f"
if [[ -n "$ADMIN_USER" ]]; then
  echo "管理员已按 .env 配置自动创建，可直接登录。"
else
  echo "首次访问 /setup 创建管理员；初始化令牌在 $APP_DIR/.env 的 AM4_SETUP_TOKEN。"
fi
