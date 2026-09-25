"""Запуск бота: python run.py"""

import asyncio
import sys

if sys.version_info < (3, 10):
    sys.exit("Нужен Python 3.10 или новее. Скачай: https://www.python.org/downloads/")

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from finbot.app import main
from finbot.config import ConfigError

if __name__ == "__main__":
    # В консоли Windows со старой кодировкой эмодзи не должны ронять бот.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except ConfigError as e:
        print(f"\n⚠️  {e}\n")
        sys.exit(1)
    except TelegramUnauthorizedError:
        print("\n⚠️  Telegram не принял токен. Проверь BOT_TOKEN в файле .env (его выдаёт @BotFather).\n")
        sys.exit(1)
    except TelegramNetworkError as e:
        print(
            f"\n⚠️  Нет связи с Telegram ({e}).\n    Проверь интернет. Если Telegram не открывается напрямую — укажи PROXY в .env.\n"
        )
        sys.exit(1)
