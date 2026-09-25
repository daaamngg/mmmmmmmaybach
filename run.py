"""Запуск бота: python run.py"""

import asyncio
import sys

if sys.version_info < (3, 10):
    sys.exit("Нужен Python 3.10 или новее. Скачай: https://www.python.org/downloads/")

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from finbot.app import main
from finbot.config import ConfigError

# Код выхода 2 — ошибка настройки (неверный токен и т. п.): перезапуск не поможет,
# сервис на сервере не будет крутиться в цикле. Код 1 — временная проблема (нет сети).
FATAL = 2

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
        sys.exit(FATAL)
    except TelegramUnauthorizedError:
        print(
            "\n⚠️  Telegram не принял токен. Проверь BOT_TOKEN в файле .env (его выдаёт @BotFather).\n"
            "    На сервере токен меняется командой: finbot token\n"
        )
        sys.exit(FATAL)
    except TelegramNetworkError as e:
        print(
            f"\n⚠️  Нет связи с Telegram ({e}).\n"
            "    Проверь интернет. Из России api.telegram.org часто недоступен — нужен VPN на компьютере,\n"
            "    сервер за границей или PROXY / TELEGRAM_API_URL в настройках (.env, на сервере — /etc/finbot.env).\n"
        )
        sys.exit(1)
