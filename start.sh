#!/usr/bin/env bash
# Запуск бота на macOS / Linux: ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[finbot] Нужен Python 3.10 или новее: https://www.python.org/downloads/"
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "[finbot] Создаю виртуальное окружение..."
    "$PY" -m venv .venv
fi

echo "[finbot] Проверяю зависимости..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "[finbot] Создан файл .env. Впиши в него токен бота (строка BOT_TOKEN=...) и запусти ./start.sh ещё раз."
    exit 0
fi

echo "[finbot] Запускаю. Остановить — Ctrl+C."
exec .venv/bin/python run.py
