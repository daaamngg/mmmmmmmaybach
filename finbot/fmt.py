"""Форматирование: даты по-русски, прогресс-бары, склонения."""

from __future__ import annotations

import calendar
import html
from datetime import date

MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]  # fmt: skip
MONTHS_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip
MONTHS_PREP = [
    "", "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
]  # fmt: skip
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def esc(text: object) -> str:
    """Экранирование пользовательского текста для HTML-режима Telegram."""
    return html.escape(str(text), quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def fmt_day(d: date, today: date | None = None) -> str:
    """«5 сентября» (с годом, если год не текущий)."""
    s = f"{d.day} {MONTHS_GEN[d.month]}"
    if today is None or d.year != today.year:
        s += f" {d.year}"
    return s


def fmt_day_rel(d: date, today: date) -> str:
    """«сегодня», «вчера» или «5 сентября»."""
    delta = (today - d).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    if delta == 2:
        return "позавчера"
    return fmt_day(d, today)


def month_title(y: int, m: int) -> str:
    return f"{MONTHS[m]} {y}"


def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def bar(fraction: float, width: int = 10) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    if fraction > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def pct(fraction: float) -> str:
    """0.00123 → «0,1%», 0.5 → «50%», 0.0001 → «<0,1%»."""
    p = max(0.0, fraction) * 100
    if p == 0:
        return "0%"
    if p < 0.1:
        return "<0,1%"
    if p < 10:
        return f"{p:.1f}".replace(".", ",").replace(",0", "") + "%"
    return f"{p:.0f}%"


def duration(days: float) -> str:
    """Срок по-человечески: «12 дней», «5 мес.», «2 г. 3 мес.»."""
    d = max(1, int(round(days)))
    if d < 45:
        return f"{d} {plural(d, 'день', 'дня', 'дней')}"
    months = int(round(d / 30.44))
    if months < 12:
        return f"{months} мес."
    years, months = divmod(months, 12)
    s = f"{years} {plural(years, 'год', 'года', 'лет')}"
    if months:
        s += f" {months} мес."
    return s


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"
