"""Данные inline-кнопок (упаковываются в ≤ 64 байта)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="n"):
    """Переходы между разделами: balance, goals, calendar, stats, settings, history,
    help, motivation, want, expense, income, cancel, noop, newgoal, wallet."""

    to: str


class TxCb(CallbackData, prefix="t"):
    """Действия с записью: undo, cat, setcat, flip, pct, setpct, back."""

    action: str
    id: int
    arg: str = ""


class GoalCb(CallbackData, prefix="g"):
    action: str
    id: int = 0
    arg: str = ""
    idx: int = 0  # номер показанного фото в карусели


class CalCb(CallbackData, prefix="c"):
    """m — месяц, d — день, add — добавить в день, del — список на удаление."""

    action: str
    y: int = 0
    m: int = 0
    d: int = 0
    arg: str = ""


class DelCb(CallbackData, prefix="x"):
    """Удаление записи из списка с подтверждением.

    ctx: «h» — из истории, «YYYYMMDD» — из дня календаря.
    """

    action: str  # ask | ok
    id: int
    ctx: str


class StatCb(CallbackData, prefix="s"):
    y: int
    m: int


class WishCb(CallbackData, prefix="w"):
    action: str  # no | wait | buy | save
    id: int


class SetCb(CallbackData, prefix="o"):
    action: str
    arg: str = ""
