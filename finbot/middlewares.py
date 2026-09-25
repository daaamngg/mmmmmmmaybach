"""Доступ только для владельца."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, TelegramObject, Update, User

from .context import App

log = logging.getLogger(__name__)


STRANGER_TEXT = (
    "🔒 Это личный бот, он отвечает только своему владельцу.\n\n"
    "Твой Telegram ID: <code>{id}</code>\n\n"
    "Если бот твой — значит, на сервере указан другой ID. Исправь одной командой:\n"
    "<code>finbot owner {id}</code>"
)


class OwnerOnly(BaseMiddleware):
    """Пропускает только личные сообщения владельца.

    Владелец — OWNER_ID из .env. Если он не задан, бота «забирает» себе первый,
    кто отправит /start (ID сохраняется в базе). Остальным бот один раз сообщает их ID —
    если владелец ошибся с OWNER_ID, он сразу увидит, что поправить.
    """

    def __init__(self) -> None:
        self._reported: set[int] = set()
        self._claim_lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        app: App = data["app"]
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if user is None or user.is_bot:
            return None
        if chat is not None and chat.type != "private":
            return None

        owner = app.owner_id
        if owner is None:
            message = event.message if isinstance(event, Update) else None
            if message is None or not (message.text or "").startswith("/start"):
                if message is not None:
                    await message.answer("🔒 Бот ещё ни к кому не привязан. Отправь /start, чтобы он стал твоим.")
                return None
            async with self._claim_lock:  # привязать может только самый первый /start
                if app.owner_id is None:
                    await app.db.update_settings(owner_id=user.id)
                    log.warning("Бот привязан к владельцу: %s (id=%s)", user.full_name, user.id)
            owner = app.owner_id

        if user.id != owner:
            if user.id not in self._reported:
                self._reported.add(user.id)
                log.warning(
                    "Чужой пользователь %s (id=%s) написал боту — игнорирую. "
                    "Если это ты, выполни на сервере: finbot owner %s",
                    user.full_name,
                    user.id,
                    user.id,
                )
                message = event.message if isinstance(event, Update) else None
                if message is not None:
                    with suppress(TelegramAPIError):
                        await message.answer(STRANGER_TEXT.format(id=user.id))
            return None
        return await handler(event, data)
