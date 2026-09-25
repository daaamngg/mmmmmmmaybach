"""Фоновые напоминания: утренний пинок, вечерний отчёт, «подумай 24 часа»."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

from . import clock, reports
from .callbacks import Nav
from .context import App
from .handlers.wish import reminder
from .ui import btn, kb

log = logging.getLogger(__name__)

# Если бот был выключен в момент напоминания, он пришлёт его, только если опоздал не больше чем на столько.
LATE_WINDOW = timedelta(hours=3)


def in_window(now: datetime, hhmm: str) -> bool:
    try:
        h, m = (int(x) for x in hhmm.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except ValueError:
        return False
    return start <= now < start + LATE_WINDOW


class Scheduler:
    def __init__(self, bot: Bot, app: App) -> None:
        self.bot = bot
        self.app = app

    async def run(self) -> None:
        log.info("Планировщик напоминаний запущен")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в планировщике")
            # Просыпаемся в начале каждой минуты.
            await asyncio.sleep(max(5.0, 60.5 - clock.now().second))

    async def tick(self) -> None:
        owner = self.app.owner_id
        if not owner:
            return
        db = self.app.db
        now = clock.now()
        today = now.date().isoformat()
        s = db.settings

        if s.morning_on and s.last_morning != today and in_window(now, s.morning_time):
            await db.update_settings(last_morning=today)
            markup = kb([btn("💼 Баланс", Nav(to="balance")), btn("🎯 Цели", Nav(to="goals"))])
            await self._send(owner, await reports.morning_text(db), markup)

        if s.evening_on and s.last_evening != today and in_window(now, s.evening_time):
            await db.update_settings(last_evening=today)
            text, empty = await reports.evening_text(db)
            if empty:
                markup = kb(
                    [btn("💸 Записать расход", Nav(to="expense"))],
                    [btn("✅ Сегодня без трат", Nav(to="nospend"))],
                )
            else:
                markup = kb([btn("📅 Календарь", Nav(to="calendar")), btn("💼 Баланс", Nav(to="balance"))])
            await self._send(owner, text, markup)

        for wish in await db.due_wishes(int(now.timestamp())):
            await db.mark_reminded(wish.id)
            text, markup = reminder(wish, s.currency)
            await self._send(owner, text, markup)

    async def _send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None) -> None:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramAPIError as e:
            log.warning("Не удалось отправить напоминание: %s", e)
