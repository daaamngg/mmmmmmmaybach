"""Настройки запуска из файла .env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .ai import AIConfig, AIConfigError, make_config

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Ошибка в .env — показывается пользователю как есть."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int | None
    data_dir: Path
    timezone: tzinfo | None
    proxy: str | None = None
    ai: AIConfig | None = None
    telegram_api: str | None = None  # свой адрес Bot API (зеркало, если api.telegram.org заблокирован)
    # Публичный https-адрес мини-приложения. Можно несколько через запятую — запасные:
    # бот сам выберет первый, который открывается.
    webapp_url: str | None = None
    webapp_host: str = "127.0.0.1"
    webapp_port: int = 0  # 0 — встроенный сайт мини-приложения выключен

    @property
    def webapp_urls(self) -> tuple[str, ...]:
        return tuple(u for u in re.split(r"[,\s]+", self.webapp_url or "") if u)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finbot.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "bot.log"


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or BASE_DIR / ".env", override=False)

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or ":" not in token:
        raise ConfigError(
            "BOT_TOKEN не задан.\n"
            "1) Создай бота у @BotFather и скопируй токен.\n"
            "2) Открой файл .env и вставь его в строку BOT_TOKEN=...\n"
            "   (на сервере — команда: finbot token)"
        )

    owner_raw = os.getenv("OWNER_ID", "").strip()
    owner_id: int | None = None
    if owner_raw:
        if not owner_raw.isdigit():
            raise ConfigError("OWNER_ID должен быть числом — твоим Telegram ID (узнать: @userinfobot).")
        owner_id = int(owner_raw)

    data_raw = os.getenv("DATA_DIR", "").strip()
    data_dir = Path(data_raw).expanduser() if data_raw else BASE_DIR / "data"
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir

    tz_name = os.getenv("TIMEZONE", "").strip()
    tz: tzinfo | None = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"Неизвестный часовой пояс TIMEZONE={tz_name}. Пример: Europe/Moscow") from e

    proxy = os.getenv("PROXY", "").strip() or None
    try:
        ai = make_config(os.getenv("AI_API_KEY"), os.getenv("AI_BASE_URL"), os.getenv("AI_MODEL"))
    except AIConfigError as e:
        raise ConfigError(str(e)) from e
    telegram_api = os.getenv("TELEGRAM_API_URL", "").strip().rstrip("/") or None
    if telegram_api and not telegram_api.startswith(("http://", "https://")):
        raise ConfigError("TELEGRAM_API_URL должен начинаться с http:// или https://")
    urls = [u.rstrip("/") for u in re.split(r"[,\s]+", os.getenv("WEBAPP_URL", "")) if u]
    for url in urls:
        if not re.fullmatch(r"https://[^\s/?#]+(/[^\s?#]*)?", url):
            raise ConfigError(
                f"WEBAPP_URL: «{url}» — нужен адрес вида https://… (Telegram открывает мини-приложения только по HTTPS)"
            )
    webapp_url = ",".join(urls) or None
    port_raw = os.getenv("WEBAPP_PORT", "").strip()
    if port_raw and not port_raw.isdigit():
        raise ConfigError("WEBAPP_PORT должен быть числом, например 8080")
    webapp_port = int(port_raw) if port_raw else (8080 if webapp_url else 0)
    return Config(
        bot_token=token,
        owner_id=owner_id,
        data_dir=data_dir,
        timezone=tz,
        proxy=proxy,
        ai=ai,
        telegram_api=telegram_api,
        webapp_url=webapp_url,
        webapp_host=os.getenv("WEBAPP_HOST", "").strip() or "127.0.0.1",
        webapp_port=webapp_port,
    )
