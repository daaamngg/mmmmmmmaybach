"""Настройки запуска из файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

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
            "2) Открой файл .env и вставь его в строку BOT_TOKEN=..."
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
    return Config(bot_token=token, owner_id=owner_id, data_dir=data_dir, timezone=tz, proxy=proxy)
