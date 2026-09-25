"""Главное меню и общие клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .callbacks import Nav
from .ui import btn, kb

MENU_EXPENSE = "💸 Расход"
MENU_INCOME = "💰 Доход"
MENU_GOALS = "🎯 Цели"
MENU_CALENDAR = "📅 Календарь"
MENU_BALANCE = "💼 Баланс"
MENU_STATS = "📊 Статистика"
MENU_WANT = "🛑 Хочу купить"
MENU_MOTIVATION = "🔥 Мотивация"

MENU_BUTTONS = (
    MENU_EXPENSE,
    MENU_INCOME,
    MENU_GOALS,
    MENU_CALENDAR,
    MENU_BALANCE,
    MENU_STATS,
    MENU_WANT,
    MENU_MOTIVATION,
)


def main_menu() -> ReplyKeyboardMarkup:
    rows = [MENU_BUTTONS[i : i + 2] for i in range(0, len(MENU_BUTTONS), 2)]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="350 шаурма  ·  +2000 зп",
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return kb([btn("❌ Отмена", Nav(to="cancel"))])
