"""Порядок подключения роутеров важен: меню первым (кнопки меню работают из любого
состояния), разбор «быстрого ввода» и заглушки — последними."""

from __future__ import annotations

from aiogram import Router


def routers() -> list[Router]:
    from . import calendar, entry, fallback, goals, history, menu, settings, stats, wish

    return [
        menu.router,
        goals.router,
        calendar.router,
        history.router,
        stats.router,
        wish.router,
        settings.router,
        entry.router,
        fallback.router,
    ]
