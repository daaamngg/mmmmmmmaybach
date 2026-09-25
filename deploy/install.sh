#!/usr/bin/env bash
# Установка бота на сервер (Ubuntu 22.04+ / Debian 12+) одной командой. От root:
#
#   curl -fsSL https://raw.githubusercontent.com/daaamngg/mmmmmmmaybach/HEAD/deploy/install.sh | bash
#
# Настройки можно передать заранее, тогда скрипт не будет их спрашивать:
#   ... | OWNER_ID=123456789 TIMEZONE=Europe/Moscow bash
#
# Повторный запуск безопасен: обновит код, а данные и настройки оставит как есть.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/daaamngg/mmmmmmmaybach.git}"
BRANCH="${BRANCH:-}"                  # пусто — основная ветка репозитория
APP_DIR="${APP_DIR:-/opt/finbot}"     # код (обновляется)
DATA_ROOT="/var/lib/finbot"           # данные: база и фото (не трогаются при обновлении)
DATA_DIR="$DATA_ROOT/data"
ENV_FILE="/etc/finbot.env"            # настройки и токен
SERVICE="finbot"
BOT_USER="finbot"

say()  { printf '\033[1;32m▶\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

# ask ИМЯ "Вопрос" [по умолчанию] [secret] — берёт значение из окружения или спрашивает.
ask() {
    local name="$1" prompt="$2" default="${3:-}" secret="${4:-}" answer=""
    if [ -n "${!name:-}" ]; then
        return 0
    fi
    if ! { exec 3</dev/tty; } 2>/dev/null; then
        [ -n "$default" ] || die "Нет терминала для вопроса «$prompt». Передай $name=... в окружении."
        printf -v "$name" '%s' "$default"
        return 0
    fi
    if [ -n "$secret" ]; then
        read -r -s -p "$prompt: " answer <&3
        echo
    else
        read -r -p "$prompt${default:+ [$default]}: " answer <&3
    fi
    exec 3<&-
    printf -v "$name" '%s' "${answer:-$default}"
}

# set_env КЛЮЧ ЗНАЧЕНИЕ — заменяет или добавляет строку в файле настроек.
set_env() {
    local tmp
    tmp="$(mktemp)"
    K="$1" V="$2" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
        index($0, k "=") == 1 { print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }' "$ENV_FILE" >"$tmp"
    cat "$tmp" >"$ENV_FILE"
    rm -f "$tmp"
}

valid_token() { [[ "$1" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; }

# ── проверки ──
[ "$(id -u)" -eq 0 ] || die "Запусти от root (или добавь sudo перед bash)."
command -v apt-get >/dev/null || die "Нужна Ubuntu 22.04+ или Debian 12+ (с apt-get)."

say "Ставлю системные пакеты (git, Python)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq git python3 python3-venv ca-certificates >/dev/null

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Нужен Python 3.10+, а в системе $(python3 -V). Возьми сервер с Ubuntu 22.04/24.04 или Debian 12."

id -u "$BOT_USER" >/dev/null 2>&1 \
    || useradd --system --home-dir "$DATA_ROOT" --create-home --shell /usr/sbin/nologin "$BOT_USER"

# ── код ──
if [ -d "$APP_DIR/.git" ]; then
    if [ -z "${SKIP_FETCH:-}" ]; then
        say "Обновляю код в $APP_DIR…"
        branch="${BRANCH:-$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)}"
        git -C "$APP_DIR" fetch -q --depth 1 origin "$branch"
        git -C "$APP_DIR" reset -q --hard FETCH_HEAD
    fi
else
    say "Скачиваю бота в $APP_DIR…"
    git clone -q --depth 1 ${BRANCH:+--branch "$BRANCH"} "$REPO_URL" "$APP_DIR"
fi

say "Ставлю зависимости Python…"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip >/dev/null
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
# Код бота только для чтения (сервис не может его менять) — компилируем заранее.
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/finbot" "$APP_DIR/run.py" >/dev/null

# ── настройки ──
new_config=""
if [ ! -f "$ENV_FILE" ]; then
    new_config=1
    echo
    say "Настройка. Токен — от @BotFather, твой ID — от @userinfobot."
    while :; do
        ask BOT_TOKEN "Вставь токен бота (ввод скрыт)" "" secret
        valid_token "$BOT_TOKEN" && break
        warn "Это не похоже на токен (нужно вида 1234567890:AAH…). Попробуй ещё раз."
        BOT_TOKEN=""
    done
    echo "  ✓ токен принят (бот id ${BOT_TOKEN%%:*})"
    while :; do
        ask OWNER_ID "Твой Telegram ID (Enter — бот станет твоим по первому /start)" "-"
        [ "$OWNER_ID" = "-" ] && OWNER_ID=""
        [[ -z "$OWNER_ID" || "$OWNER_ID" =~ ^[0-9]+$ ]] && break
        warn "ID — это только цифры."
        OWNER_ID=""
    done
    ask TIMEZONE "Часовой пояс для напоминаний" "Europe/Moscow"
    umask 077
    cat >"$ENV_FILE" <<EOF
# Настройки бота. После изменения: finbot restart
BOT_TOKEN=$BOT_TOKEN
OWNER_ID=$OWNER_ID
TIMEZONE=$TIMEZONE
DATA_DIR=$DATA_DIR
# Нейросеть для категорий (необязательно) — проще всего через: finbot ai
AI_API_KEY=${AI_API_KEY:-}
EOF
    umask 022
else
    say "Настройки уже есть ($ENV_FILE) — оставляю."
    # Явно переданные значения обновляем (например, BOT_TOKEN=… после перевыпуска).
    for key in BOT_TOKEN OWNER_ID TIMEZONE AI_API_KEY; do
        if [ -n "${!key:-}" ]; then
            [ "$key" != BOT_TOKEN ] || valid_token "$BOT_TOKEN" || die "BOT_TOKEN не похож на токен."
            set_env "$key" "${!key}"
        fi
    done
fi
chmod 600 "$ENV_FILE"

tz="$(grep -E '^TIMEZONE=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -n "$tz" ] && ! "$APP_DIR/.venv/bin/python" -c "import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])" "$tz" 2>/dev/null; then
    die "Неизвестный часовой пояс «$tz». Исправь TIMEZONE в $ENV_FILE (например, Europe/Moscow) и запусти снова."
fi

mkdir -p "$DATA_DIR"
chown -R "$BOT_USER:$BOT_USER" "$DATA_ROOT"
chmod 700 "$DATA_ROOT"
install -m 755 "$APP_DIR/deploy/finbot" /usr/local/bin/finbot

# ── автозапуск ──
if [ ! -d /run/systemd/system ]; then
    warn "systemd не найден (контейнер/WSL?) — автозапуск не настроен. Запустить вручную:"
    echo "    bash -c 'set -a; . $ENV_FILE; set +a; exec setpriv --reuid=$BOT_USER --regid=$BOT_USER --init-groups $APP_DIR/.venv/bin/python $APP_DIR/run.py'"
    exit 0
fi

cat >/etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Maybach — личный финансовый бот в Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
Group=$BOT_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py
Restart=always
RestartSec=5
# 2 — ошибка настройки (например, неверный токен): крутить перезапуски бессмысленно.
RestartPreventExitStatus=2
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_ROOT

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q "$SERVICE"
since="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart "$SERVICE"

say "Запускаю и проверяю связь с Telegram…"
started=""
for _ in $(seq 1 30); do
    sleep 1
    if journalctl -u "$SERVICE" --since "$since" -q --no-pager 2>/dev/null | grep -q "запущен"; then
        started=1
        break
    fi
    [ "$(systemctl is-active "$SERVICE")" != failed ] || break
done

if [ -n "$started" ]; then
    echo
    say "Готово! Бот работает 24/7 и сам поднимется после перезагрузки сервера."
    [ -z "$new_config" ] || echo "   Открой бота в Telegram и отправь /start."
    echo
    echo "   Управление: finbot status | logs | restart | update | token | ai | backup | import"
    exit 0
fi

echo
warn "Бот не подтвердил запуск. Последние строки лога:"
journalctl -u "$SERVICE" --since "$since" -n 25 --no-pager || true
echo
warn "Если там «Telegram не принял токен» — выполни: finbot token"
warn "Если «Нет связи с Telegram» — у сервера нет доступа к api.telegram.org (сервер в РФ?)."
warn "Нужен сервер за границей (например, Нидерланды) или PROXY в $ENV_FILE."
exit 1
