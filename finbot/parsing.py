"""Разбор быстрого ввода: «350 шаурма», «+2000 зп», «вчера такси 500», «12.09 1500 продукты»."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .money import CUR, NUM, SUF, to_minor


@dataclass(frozen=True)
class ParsedEntry:
    amount: int  # в копейках
    sign: str | None  # "+", "-" или None, если знак не указан
    note: str
    day: date | None = None  # если в начале указана дата


_DASHES = str.maketrans({"\u2212": "-", "\u2013": "-", "\u2014": "-"})  # «−», «–», «—» → «-»
_DATE_TOKEN = r"вчера|позавчера|сегодня|\d{1,2}[./]\d{1,2}(?:[./](?:\d{4}|\d{2}))?"
_DATE_PREFIX = re.compile(rf"^(?P<d>{_DATE_TOKEN})\s+(?P<rest>.+)$", re.I | re.S)
# «350 шаурма», «+2к зп», «-500р такси»
_AMOUNT_FIRST = re.compile(
    rf"^(?P<sign>[+-])?\s*(?P<num>{NUM})\s?(?P<suf>{SUF})?\s?(?:{CUR})?(?=\s|$)\s*(?P<note>.*)$",
    re.I | re.S,
)
# «шаурма 350», «зп +50к»
_AMOUNT_LAST = re.compile(
    rf"^(?P<note>.+?)\s+(?P<sign>[+-])?\s*(?P<num>{NUM})\s?(?P<suf>{SUF})?\s?(?:{CUR})?\s*$",
    re.I | re.S,
)
_SPACES = re.compile(r"\s+")
MAX_NOTE = 100


def _clean_note(note: str) -> str:
    note = _SPACES.sub(" ", note).strip(" ,.;:-")
    return note[:MAX_NOTE]


def _parse_core(text: str) -> ParsedEntry | None:
    for rx in (_AMOUNT_FIRST, _AMOUNT_LAST):
        m = rx.match(text)
        if not m:
            continue
        amount = to_minor(m.group("num"), m.group("suf"))
        if amount is None:
            continue
        return ParsedEntry(amount=amount, sign=m.group("sign"), note=_clean_note(m.group("note")))
    return None


def parse_day_token(token: str, today: date) -> date | None:
    t = token.lower()
    if t == "сегодня":
        return today
    if t == "вчера":
        return today - timedelta(days=1)
    if t == "позавчера":
        return today - timedelta(days=2)
    parts = re.split(r"[./]", t)
    try:
        d, m = int(parts[0]), int(parts[1])
        if len(parts) > 2:
            y = int(parts[2])
            if y < 100:
                y += 2000
            return date(y, m, d)
        result = date(today.year, m, d)
    except (ValueError, IndexError):
        return None
    # «28.12» в сентябре — это прошлый декабрь, а не будущий.
    if result > today:
        try:
            result = date(today.year - 1, m, d)
        except ValueError:
            return None
    return result


def parse_entry(text: str, today: date) -> ParsedEntry | None:
    """Разбирает сообщение с суммой. None — если суммы нет."""
    t = (text or "").translate(_DASHES).strip()
    if not t:
        return None
    m = _DATE_PREFIX.match(t)
    if m:
        day = parse_day_token(m.group("d"), today)
        if day is not None:
            entry = _parse_core(m.group("rest").strip())
            if entry is not None:
                return replace(entry, day=day)
    return _parse_core(t)


def parse_time(text: str) -> str | None:
    """«9», «9:30», «21.00», «7 15» → «HH:MM»."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:\s*[:.\s-]\s*(\d{2}))?\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_deadline(text: str, today: date) -> date | None:
    """Срок цели: «31.12.2027», «31.12.27», «31.12», «12.2027». Только будущее."""
    t = (text or "").strip().lower()
    result: date | None = None
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        try:
            result = date(y, mo, d)
        except ValueError:
            return None
        if not m.group(3) and result <= today:
            try:
                result = date(y + 1, mo, d)
            except ValueError:
                return None
    else:
        m = re.fullmatch(r"(\d{1,2})[./](\d{4})", t)
        if m:
            mo, y = int(m.group(1)), int(m.group(2))
            if not 1 <= mo <= 12:
                return None
            result = date(y, mo, calendar.monthrange(y, mo)[1])
    if result is None or result <= today or result.year > today.year + 100:
        return None
    return result
