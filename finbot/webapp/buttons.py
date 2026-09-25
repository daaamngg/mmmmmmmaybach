"""Кнопки, которые открывают мини-приложение из чата с ботом."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, MenuButtonDefault, MenuButtonWebApp, WebAppInfo

from ..context import App

log = logging.getLogger(__name__)

MENU_TEXT = "Приложение"


def app_button(app: App, text: str = "📱 Открыть приложение") -> InlineKeyboardButton | None:
    """Кнопка под сообщением. None — пока приложение не настроено или ещё не доступно по HTTPS."""
    if app.webapp_url is None:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=app.webapp_url))


async def ensure_menu_button(bot: Bot, app: App) -> None:
    """Кнопка «Приложение» слева от поля ввода (только в чате владельца).

    Если приложение выключили, возвращаем обычную кнопку меню с командами.
    """
    owner, url = app.owner_id, app.webapp_url
    if not owner:
        return
    try:
        if url is not None:
            await bot.set_chat_menu_button(
                chat_id=owner, menu_button=MenuButtonWebApp(text=MENU_TEXT, web_app=WebAppInfo(url=url))
            )
        elif not app.config.webapp_url:
            current = await bot.get_chat_menu_button(chat_id=owner)
            if isinstance(current, MenuButtonWebApp):
                await bot.set_chat_menu_button(chat_id=owner, menu_button=MenuButtonDefault())
    except Exception as e:  # кнопка — удобство: из-за неё не должен ломаться /start
        log.warning("Не удалось обновить кнопку мини-приложения: %s", e)
