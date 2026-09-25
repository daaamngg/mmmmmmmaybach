"""Локальное время бота.

Все модули берут время только отсюда (``clock.now()``), поэтому часовой пояс
задаётся в одном месте, а в тестах время легко подменить.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo

_tz: tzinfo | None = None


def set_timezone(tz: tzinfo | None) -> None:
    global _tz
    _tz = tz


def now() -> datetime:
    """Текущее время с часовым поясом (из настроек или системным)."""
    if _tz is not None:
        return datetime.now(_tz)
    return datetime.now().astimezone()


def today() -> date:
    return now().date()


def to_local(dt: datetime) -> datetime:
    """Переводит время (например, дату сообщения Telegram в UTC) в локальное."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz) if _tz is not None else dt.astimezone()


def from_ts(ts: int) -> datetime:
    return to_local(datetime.fromtimestamp(ts, timezone.utc))


def local_ts(d: date) -> int:
    """Unix-время локальной полуночи указанного дня."""
    if _tz is not None:
        return int(datetime(d.year, d.month, d.day, tzinfo=_tz).timestamp())
    return int(datetime(d.year, d.month, d.day).astimezone().timestamp())
