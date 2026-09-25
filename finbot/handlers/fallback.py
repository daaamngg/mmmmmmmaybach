"""Всё, что не поймали остальные обработчики."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from ..keyboards import cancel_kb

router = Router(name="fallback")


@router.callback_query()
async def stale_button(cb: CallbackQuery, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Эта кнопка устарела. Открой раздел заново через меню."


@router.message()
async def anything_else(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        await message.answer("✍️ Жду ответ текстом. Передумал — жми «Отмена».", reply_markup=cancel_kb())
        return
    await message.answer(
        "Я понимаю текст и фото.\n"
        "• <code>350 шаурма</code> — расход\n"
        "• <code>+2000 зп</code> — доход\n"
        "• фото — прикреплю к цели\n\n"
        "Справка — /help"
    )
