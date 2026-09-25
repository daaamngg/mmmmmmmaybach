"""Показ экранов: редактирование «на месте», фото, вопросы, фоновые задачи."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.callback_answer import CallbackAnswer, CallbackAnswerMiddleware

log = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

Event = Message | CallbackQuery


# ─────────────────────────── клавиатуры ───────────────────────────


def btn(text: str, cb: CallbackData | str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb if isinstance(cb, str) else cb.pack())


def kb(*rows: Sequence[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    """Клавиатура из рядов; пустые ряды и None пропускаются."""
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def chunks(items: Sequence[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


# ─────────────────────────── показ экранов ───────────────────────────


@dataclass
class PhotoSource:
    """Фото для экрана: file_id Telegram + локальная копия на случай, если file_id устарел."""

    file_id: str
    unique_id: str | None = None
    local_path: str | None = None
    row_id: int | None = None  # id фото в базе
    refreshed_id: str | None = None  # новый file_id, если пришлось загрузить с диска


def fit(text: str, limit: int) -> str:
    """Обрезает длинный HTML-текст по целым строкам, чтобы не порвать теги."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    while len(lines) > 1 and len("\n".join(lines)) > limit - 2:
        lines.pop()
    out = "\n".join(lines)
    return (out if len(out) <= limit - 2 else out[: limit - 2]) + "\n…"


def _not_modified(e: TelegramBadRequest) -> bool:
    return "not modified" in e.message.lower()


def chat_id_of(event: Event) -> int:
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            return event.message.chat.id
        return event.from_user.id
    return event.chat.id


async def _try_edit(
    msg: Message, text: str, markup: InlineKeyboardMarkup | None, photo: PhotoSource | None
) -> Message | None:
    try:
        if photo is None and not msg.photo and msg.text is not None:
            res = await msg.edit_text(fit(text, TEXT_LIMIT), reply_markup=markup)
            return res if isinstance(res, Message) else msg
        if photo is not None and msg.photo:
            caption = fit(text, CAPTION_LIMIT)
            if photo.unique_id and msg.photo[-1].file_unique_id == photo.unique_id:
                res = await msg.edit_caption(caption=caption, reply_markup=markup)
            else:
                res = await msg.edit_media(InputMediaPhoto(media=photo.file_id, caption=caption), reply_markup=markup)
            return res if isinstance(res, Message) else msg
    except TelegramBadRequest as e:
        if _not_modified(e):
            return msg
        log.debug("Edit failed, sending a new message: %s", e.message)
    return None


async def send_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    photo: PhotoSource | None = None,
) -> Message:
    if photo is None:
        return await bot.send_message(chat_id, fit(text, TEXT_LIMIT), reply_markup=markup)
    caption = fit(text, CAPTION_LIMIT)
    try:
        return await bot.send_photo(chat_id, photo.file_id, caption=caption, reply_markup=markup)
    except TelegramBadRequest as e:
        if not (photo.local_path and Path(photo.local_path).is_file()):
            log.warning("Photo is unavailable (%s), showing text instead", e.message)
            return await bot.send_message(chat_id, fit(text, TEXT_LIMIT), reply_markup=markup)
    sent = await bot.send_photo(chat_id, FSInputFile(photo.local_path), caption=caption, reply_markup=markup)
    if sent.photo:
        photo.refreshed_id = sent.photo[-1].file_id
    return sent


async def render(
    event: Event,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    photo: PhotoSource | None = None,
) -> Message:
    """Показывает экран.

    Из кнопки — редактирует текущее сообщение (быстро, чат не засоряется);
    если тип не совпадает (текст ↔ фото) — заменяет сообщение новым.
    Из текстового сообщения — отправляет новое.
    """
    bot = event.bot
    assert bot is not None
    if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
        edited = await _try_edit(event.message, text, markup, photo)
        if edited is not None:
            return edited
        with suppress(TelegramAPIError):
            await event.message.delete()
    return await send_screen(bot, chat_id_of(event), text, markup, photo)


# ─────────────────────────── вопросы (FSM) ───────────────────────────


async def ask(
    event: Event,
    state: FSMContext,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    replace: bool = False,
) -> Message:
    """Задаёт вопрос и запоминает сообщение, чтобы после ответа убрать у него кнопки."""
    previous = (await state.get_data()).get("prompt_id")
    bot = event.bot
    if (
        previous
        and bot is not None
        and not (
            replace and isinstance(event, CallbackQuery) and event.message and event.message.message_id == previous
        )
    ):
        spawn(_drop_markup(bot, chat_id_of(event), previous))
    if replace and isinstance(event, CallbackQuery):
        msg = await render(event, text, markup)
    else:
        assert bot is not None
        msg = await bot.send_message(chat_id_of(event), text, reply_markup=markup)
    await state.update_data(prompt_id=msg.message_id)
    return msg


async def finish(event: Event, state: FSMContext) -> dict[str, Any]:
    """Завершает диалог: сбрасывает состояние и убирает кнопки у вопроса. Возвращает данные."""
    data = await state.get_data()
    await state.clear()
    prompt_id = data.get("prompt_id")
    bot = event.bot
    # Если нажали кнопку прямо на вопросе — это сообщение сейчас перерисуют, не трогаем.
    on_prompt = isinstance(event, CallbackQuery) and event.message is not None and event.message.message_id == prompt_id
    if prompt_id and bot is not None and not on_prompt:
        spawn(_drop_markup(bot, chat_id_of(event), prompt_id))
    return data


async def _drop_markup(bot: Bot, chat_id: int, message_id: int) -> None:
    with suppress(TelegramAPIError):
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)


# ─────────────────────────── фоновые задачи ───────────────────────────

_tasks: set[asyncio.Task[Any]] = set()


async def _guard(coro: Coroutine[Any, Any, Any]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Background task failed")


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Запускает задачу в фоне, не теряя ссылку на неё и логируя ошибки."""
    task = asyncio.create_task(_guard(coro))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain() -> None:
    """Дождаться всех фоновых задач (для тестов и остановки)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


class Debouncer:
    """Откладывает действие, пока поток событий не затихнет (например, альбом фото)."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._pending: dict[Hashable, asyncio.Task[Any]] = {}

    def schedule(self, key: Hashable, action: Callable[[], Awaitable[Any]]) -> None:
        old = self._pending.get(key)
        if old is not None and not old.done():
            old.cancel()
        self._pending[key] = spawn(self._run(key, action))

    async def _run(self, key: Hashable, action: Callable[[], Awaitable[Any]]) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return
        if self._pending.get(key) is asyncio.current_task():
            del self._pending[key]
        await action()


class SafeCallbackAnswer(CallbackAnswerMiddleware):
    """Автоответ на нажатие кнопки, который не падает на «протухших» кнопках."""

    async def answer(self, event: CallbackQuery, callback_answer: CallbackAnswer) -> None:  # type: ignore[override]
        text = callback_answer.text
        if text and len(text) > 200:  # лимит Telegram для всплывающего ответа
            callback_answer.text = text[:199] + "…"
        try:
            await super().answer(event, callback_answer)
        except TelegramAPIError as e:
            log.debug("Callback answer failed: %s", e)
