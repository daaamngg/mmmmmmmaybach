#!/usr/bin/env bash
# Мини-приложение: HTTPS-адрес для Telegram через Caddy (сертификат он получает и продлевает сам).
#
# Адрес по умолчанию — бесплатный <ip-сервера>.sslip.io (всегда указывает на IP этого сервера)
# и запасной <ip-сервера>.nip.io: у бесплатных доменов общий на всех лимит сертификатов
# Let's Encrypt, и если он временно исчерпан у одного, бот возьмёт другой.
# Свой домен: WEBAPP_DOMAIN=fin.example.com (A-запись домена должна указывать на сервер).
# Почта для центра сертификации (необязательно, включает запасной ZeroSSL): WEBAPP_EMAIL=…
#
# Вызывается установщиком и командой «finbot webapp on». Бота не перезапускает.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/finbot.env}"
CADDYFILE="/etc/caddy/Caddyfile"
MARK="# finbot"

say()  { printf '\033[1;32m▶\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
# Ошибка здесь не должна ломать установку бота: он работает и без мини-приложения.
fail() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; exit 1; }

set_env() {
    local tmp
    tmp="$(mktemp)"
    K="$1" V="$2" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
        index($0, k "=") == 1 { print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }' "$ENV_FILE" >"$tmp"
    cat "$tmp" >"$ENV_FILE"
    rm -f "$tmp"
    chmod 600 "$ENV_FILE"
}

get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }

is_public_ipv4() {
    local ip="$1" a b
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    a="${BASH_REMATCH[1]}"
    b="${BASH_REMATCH[2]}"
    local c
    c="$(cut -d. -f3 <<<"$ip")"
    case "$a" in
        0 | 10 | 127) return 1 ;;
        100) if [ "$b" -ge 64 ] && [ "$b" -le 127 ]; then return 1; fi ;;
        169) if [ "$b" -eq 254 ]; then return 1; fi ;;
        172) if [ "$b" -ge 16 ] && [ "$b" -le 31 ]; then return 1; fi ;;
        192) if [ "$b" -eq 168 ] || { [ "$b" -eq 0 ] && [ "$c" -eq 2 ]; }; then return 1; fi ;;
        198) if [ "$b" -eq 18 ] || [ "$b" -eq 19 ] || { [ "$b" -eq 51 ] && [ "$c" -eq 100 ]; }; then return 1; fi ;;
        203) if [ "$b" -eq 0 ] && [ "$c" -eq 113 ]; then return 1; fi ;;
    esac
    [ "$a" -lt 224 ]
}

detect_ip() {
    local ip url
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1) }' | head -n 1)"
    if is_public_ipv4 "$ip"; then
        echo "$ip"
        return 0
    fi
    # За NAT (часть облаков): спрашиваем внешний адрес у сервиса.
    for url in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
        ip="$(curl -4 -fsS --max-time 6 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
        if is_public_ipv4 "$ip"; then
            echo "$ip"
            return 0
        fi
    done
    return 1
}

port_free() { ! ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }

install_caddy() {
    command -v caddy >/dev/null 2>&1 && return 0
    say "Ставлю Caddy — он сам получает и продлевает HTTPS-сертификат…"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null 2>&1 || true
    if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
        # В Ubuntu 22.04 Caddy нет среди стандартных пакетов — берём официальный репозиторий Caddy.
        apt-get install -y -qq curl gnupg ca-certificates >/dev/null
        curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
            gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
            >/etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq >/dev/null
        apt-get install -y -qq caddy >/dev/null
    fi
    command -v caddy >/dev/null 2>&1
}

open_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
        say "Открыл порты 80 и 443 в ufw."
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd -q --permanent --add-service=http --add-service=https && firewall-cmd -q --reload
        say "Открыл порты 80 и 443 в firewalld."
    fi
}

[ "$(id -u)" -eq 0 ] || fail "Запусти от root."
[ -f "$ENV_FILE" ] || fail "Нет файла настроек $ENV_FILE — сначала установи бота."
command -v ss >/dev/null 2>&1 || apt-get install -y -qq iproute2 >/dev/null 2>&1 || true

# ── адрес ──
# Свой домен и почту запоминаем в настройках, чтобы «finbot update» их не сбрасывал.
if [ -n "${WEBAPP_DOMAIN:-}" ]; then set_env WEBAPP_DOMAIN "$WEBAPP_DOMAIN"; else WEBAPP_DOMAIN="$(get_env WEBAPP_DOMAIN)"; fi
if [ -n "${WEBAPP_EMAIL:-}" ]; then set_env WEBAPP_EMAIL "$WEBAPP_EMAIL"; else WEBAPP_EMAIL="$(get_env WEBAPP_EMAIL)"; fi
domain="$(printf '%s' "$WEBAPP_DOMAIN" | tr '[:upper:]' '[:lower:]')"
domain="${domain#https://}"
domain="${domain%%/*}"
if [ -z "$domain" ]; then
    ip="$(detect_ip)" || fail "Не смог узнать внешний IP сервера. Укажи домен сам: finbot webapp on мой.домен"
    domains=("${ip//./-}.sslip.io" "${ip//./-}.nip.io")
else
    domains=("$domain")
    [[ "$domain" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] || fail "«$domain» не похож на домен."
    resolved="$(getent ahostsv4 "$domain" 2>/dev/null | awk 'NR == 1 { print $1 }' || true)"
    ip="$(detect_ip || true)"
    if [ -z "$resolved" ]; then
        warn "Домен $domain пока никуда не указывает — добавь A-запись на IP сервера${ip:+ ($ip)}."
    elif [ -n "$ip" ] && [ "$resolved" != "$ip" ]; then
        warn "Домен $domain указывает на $resolved, а у сервера IP $ip — сертификат не выпустится, пока это не исправишь."
    fi
fi

# ── порты ──
others="$(ss -Hltnp 2>/dev/null | awk '$4 ~ /:(80|443)$/' | grep -v '"caddy"' || true)"
if [ -n "$others" ]; then
    warn "Порты 80/443 уже заняты другой программой (nginx? apache?):"
    printf '%s\n' "$others" | sed 's/^/     /'
    fail "Мини-приложение не настроено автоматически. Направь свой веб-сервер на 127.0.0.1:${WEBAPP_PORT:-8080} и впиши WEBAPP_URL=https://… в $ENV_FILE."
fi

port="$(get_env WEBAPP_PORT)"
if [ -z "$port" ]; then
    for candidate in 8080 8081 18080 28080; do
        if port_free "$candidate"; then
            port="$candidate"
            break
        fi
    done
    [ -n "$port" ] || fail "Не нашёл свободный внутренний порт для мини-приложения."
    set_env WEBAPP_PORT "$port"
fi

# ── Caddy ──
install_caddy || fail "Не получилось установить Caddy — мини-приложение не настроено."
sites="$(printf '%s, ' "${domains[@]}")"
sites="${sites%, }"

if [ -f "$CADDYFILE" ] && ! head -n 1 "$CADDYFILE" | grep -qF "$MARK" && ! grep -q "/usr/share/caddy" "$CADDYFILE"; then
    warn "В $CADDYFILE уже есть твои настройки — не трогаю их. Добавь туда блок и выполни systemctl reload caddy:"
    printf '\n%s {\n\treverse_proxy 127.0.0.1:%s\n}\n\n' "$sites" "$port"
else
    mkdir -p "$(dirname "$CADDYFILE")"
    [ -f "$CADDYFILE" ] && cp "$CADDYFILE" "$CADDYFILE.bak"
    {
        echo "$MARK: файл создан установщиком бота. Добавишь свои сайты — удали эту строку, и установщик его больше не тронет."
        if [ -n "${WEBAPP_EMAIL:-}" ]; then
            printf '{\n\temail %s\n}\n\n' "$WEBAPP_EMAIL"
        fi
        printf '%s {\n\tencode zstd gzip\n\treverse_proxy 127.0.0.1:%s\n}\n' "$sites" "$port"
    } >"$CADDYFILE.new"
    if ! caddy validate --config "$CADDYFILE.new" --adapter caddyfile >/dev/null 2>&1; then
        rm -f "$CADDYFILE.new"
        fail "Caddy не принял настройки для $sites — мини-приложение не настроено."
    fi
    mv "$CADDYFILE.new" "$CADDYFILE"
fi

open_firewall
systemctl enable -q caddy
systemctl reload-or-restart caddy || fail "Caddy не запустился: journalctl -u caddy -n 30"

urls="$(printf 'https://%s,' "${domains[@]}")"
set_env WEBAPP_URL "${urls%,}"
say "Мини-приложение: https://${domains[0]} (сертификат Caddy получит сам, обычно за минуту)."
