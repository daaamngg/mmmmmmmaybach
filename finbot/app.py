"""Сборка и запуск бота."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommandScopeAllPrivateChats, ErrorEvent

from . import clock, handlers, texts, ui
from .config import Config, ConfigError, load_config
from .context import App
from .db import Database
from .middlewares import OwnerOnly
from .scheduler import Scheduler

log = logging.getLogger("finbot")


def setup_logging(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(config.log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file], force=True)
    # Служебный шум aiogram (по строке на каждое обновление) не нужен.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.middlewares").setLevel(logging.WARNING)


async def on_error(event: ErrorEvent, app: App) -> bool:
    log.error("Ошибка при обработке обновления", exc_info=event.exception)
    bot = event.update.bot
    owner = app.owner_id
    if bot is not None and owner:
        with suppress(Exception):
            await bot.send_message(
                owner, "⚠️ Что-то пошло не так. Ошибка записана в лог (data/bot.log). Попробуй ещё раз."
            )
    return True


def build_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), app=app, db=app.db)
    dp.update.outer_middleware(OwnerOnly())
    dp.callback_query.middleware(ui.SafeCallbackAnswer())
    dp.include_routers(*handlers.routers())
    dp.errors.register(on_error)
    return dp


def make_bot(config: Config) -> Bot:
    session = None
    if config.proxy:
        try:
            session = AiohttpSession(proxy=config.proxy)
        except ImportError as e:
            raise ConfigError("Для PROXY нужен пакет aiohttp-socks: pip install aiohttp-socks") from e
    return Bot(
        config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


async def main() -> None:
    config = load_config()
    setup_logging(config)
    clock.set_timezone(config.timezone)

    db = Database(config.db_path)
    await db.open()
    app = App(config=config, db=db)
    bot = make_bot(config)
    dp = build_dispatcher(app)
    scheduler = Scheduler(bot, app)
    tasks: list[asyncio.Task[None]] = []

    async def on_startup() -> None:
        await bot.set_my_commands(texts.COMMANDS, scope=BotCommandScopeAllPrivateChats())
        me = await bot.get_me()
        log.info("Бот @%s запущен. Данные: %s", me.username, config.data_dir)
        if app.owner_id is None:
            log.warning("Владелец ещё не задан: открой @%s в Telegram и нажми /start", me.username)
        tasks.append(asyncio.create_task(scheduler.run()))

    async def on_shutdown() -> None:
        for task in tasks:
            task.cancel()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ui.drain(), timeout=5)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    try:
        # Обновления обрабатываются строго по очереди: бот личный, а так порядок записей
        # всегда совпадает с порядком сообщений и не бывает гонок (например, при привязке владельца).
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_as_tasks=False)
    finally:
        await bot.session.close()
        await db.close()
        log.info("Бот остановлен")
