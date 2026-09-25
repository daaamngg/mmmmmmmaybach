"""Главное меню, команды и переходы. Работают из любого состояния (сбрасывают ввод)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from .. import keyboards as k
from .. import motivation, texts
from ..callbacks import Nav
from ..context import App
from ..db import Database
from ..ui import Event, render
from . import calendar, common, entry, goals, history, settings, stats, wish

router = Router(name="menu")


async def open_section(section: str, event: Event, state: FSMContext, db: Database, app: App) -> bool:
    """Открывает раздел по имени. False — если такого раздела нет."""
    if section == "balance":
        await common.show_dashboard(event, db)
    elif section == "goals":
        await goals.show_goals(event, db)
    elif section == "calendar":
        await calendar.show_month(event, db)
    elif section == "stats":
        await stats.show_stats(event, db)
    elif section == "history":
        await history.show_history(event, db)
    elif section == "settings":
        await settings.show_settings(event, db, app)
    elif section == "help":
        await common.show_help(event, db)
    elif section == "motivation":
        await common.show_motivation(event, db)
    elif section == "export":
        await stats.send_export(event, db)
    elif section == "want":
        await wish.start_wish(event, state)
    elif section == "expense":
        await entry.start_entry(event, state, db, "expense")
    elif section == "income":
        await entry.start_entry(event, state, db, "income")
    elif section == "newgoal":
        await goals.start_new_goal(event, state)
    elif section == "wallet":
        await settings.start_wallet(event, state, db)
    else:
        return False
    return True


@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    if await db.count_tx() == 0 and not await db.goals(None):
        await message.answer(texts.WELCOME, reply_markup=k.main_menu())
        await common.show_onboarding(message)
        return
    await message.answer("🔥 На связи. Stay hard.", reply_markup=k.main_menu())
    await common.show_dashboard(message, db)


COMMAND_SECTIONS = {
    "balance": "balance",
    "goals": "goals",
    "calendar": "calendar",
    "stats": "stats",
    "history": "history",
    "settings": "settings",
    "help": "help",
    "motivation": "motivation",
    "export": "export",
    "want": "want",
}

BUTTON_SECTIONS = {
    k.MENU_EXPENSE: "expense",
    k.MENU_INCOME: "income",
    k.MENU_GOALS: "goals",
    k.MENU_CALENDAR: "calendar",
    k.MENU_BALANCE: "balance",
    k.MENU_STATS: "stats",
    k.MENU_WANT: "want",
    k.MENU_MOTIVATION: "motivation",
}


@router.message(Command(*COMMAND_SECTIONS), StateFilter("*"))
async def cmd_section(message: Message, state: FSMContext, db: Database, app: App) -> None:
    command = (message.text or "").split()[0].lstrip("/").split("@")[0].lower()
    await state.clear()
    await open_section(COMMAND_SECTIONS[command], message, state, db, app)


@router.message(F.text.in_(BUTTON_SECTIONS), StateFilter("*"))
async def menu_button(message: Message, state: FSMContext, db: Database, app: App) -> None:
    await state.clear()
    await open_section(BUTTON_SECTIONS[message.text or ""], message, state, db, app)


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    await message.answer("❌ Отменено." if had_state else "Нечего отменять.", reply_markup=k.main_menu())


@router.message(F.text.startswith("/"), StateFilter("*"))
async def unknown_command(message: Message) -> None:
    await message.answer("🤨 Не знаю такой команды. Список — /help")


@router.callback_query(Nav.filter(F.to == "noop"))
async def cb_noop(cb: CallbackQuery) -> None:
    """Кнопки-подписи (дни недели, номер месяца) — ничего не делают."""


@router.callback_query(Nav.filter(F.to == "cancel"), StateFilter("*"))
async def cb_cancel(cb: CallbackQuery, state: FSMContext, callback_answer: CallbackAnswer) -> None:
    await state.clear()
    callback_answer.text = "Отменено"
    await render(cb, "❌ Отменено.")


@router.callback_query(Nav.filter(F.to == "nospend"))
async def cb_no_spend(cb: CallbackQuery, db: Database) -> None:
    await render(cb, "✅ <b>День без трат.</b>\n\n" + motivation.quote("no_spend", db.settings.harsh))


@router.callback_query(Nav.filter(), StateFilter("*"))
async def cb_nav(cb: CallbackQuery, callback_data: Nav, state: FSMContext, db: Database, app: App) -> None:
    await state.clear()
    await open_section(callback_data.to, cb, state, db, app)
