"""Календарь: месяц с отметками по дням, итоги месяца, подробности дня."""

from __future__ import annotations

import calendar as pycal
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_answer import CallbackAnswer

from .. import categories, clock
from ..callbacks import CalCb, DelCb, Nav, StatCb
from ..db import Database, Tx
from ..fmt import (
    WEEKDAYS,
    WEEKDAYS_SHORT,
    esc,
    fmt_day,
    month_bounds,
    month_title,
    shift_month,
    truncate,
)
from ..money import money
from ..reports import stat_lines
from ..ui import Event, btn, kb, render
from .entry import start_entry

router = Router(name="calendar")

NOOP = Nav(to="noop")
MAX_DAY_ITEMS = 30


async def month_view(db: Database, y: int, m: int) -> tuple[str, InlineKeyboardMarkup]:
    cur = db.settings.currency
    today = clock.today()
    first, last = month_bounds(y, m)
    days = await db.day_totals(first, last)
    summ = await db.summary(first, last)

    lines = [f"📅 <b>{month_title(y, m).upper()}</b>", "", *stat_lines(summ, cur)]
    lines.append(f"📈 Разница: <b>{money(summ.income - summ.expense, cur, signed=True)}</b>")
    first_use = await db.first_day()
    if first_use is not None and first <= today:
        start, end = max(first, first_use), min(last, today)
        total = (end - start).days + 1
        if total > 0:
            lines.append(f"🧘 Дней без трат: <b>{max(0, total - summ.spend_days)}</b> из {total}")
    lines += ["", "🟢 доход · 🔴 расход · 🟡 и то и другое", "Нажми на день — покажу подробности."]

    py, pm = shift_month(y, m, -1)
    ny, nm = shift_month(y, m, 1)
    has_next = (y, m) < (today.year, today.month)
    rows: list[list[InlineKeyboardButton]] = [
        [
            btn("«", CalCb(action="m", y=py, m=pm)),
            btn(month_title(y, m), NOOP),
            btn("»", CalCb(action="m", y=ny, m=nm)) if has_next else btn(" ", NOOP),
        ],
        [btn(d, NOOP) for d in WEEKDAYS_SHORT],
    ]
    for week in pycal.Calendar(firstweekday=0).monthdayscalendar(y, m):
        row = []
        for d in week:
            if d == 0:
                row.append(btn(" ", NOOP))
                continue
            day = date(y, m, d)
            inc, exp = days.get(day, (0, 0))
            mark = "🟡" if inc and exp else "🟢" if inc else "🔴" if exp else ""
            label = f"{d}{mark}" if mark or day != today else f"·{d}·"
            row.append(btn(label, CalCb(action="d", y=y, m=m, d=d)))
        rows.append(row)
    rows.append(
        [
            btn("📊 Статистика", StatCb(y=y, m=m)),
            btn("📍 Сегодня", CalCb(action="d", y=today.year, m=today.month, d=today.day)),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_month(event: Event, db: Database, y: int | None = None, m: int | None = None) -> None:
    today = clock.today()
    text, markup = await month_view(db, y or today.year, m or today.month)
    await render(event, text, markup)


def day_line(tx: Tx, cur: str, with_time: bool) -> str:
    cat = categories.get(tx.category)
    what = esc(truncate(tx.note, 40)) if tx.note else cat.name
    line = f"• {cat.emoji} <b>{money(tx.amount, cur)}</b> — {what}"
    if tx.kind == "income":
        saved = sum(tx.to_goals.values()) + tx.to_pool
        if saved:
            line += f" <i>(в цели {money(saved, cur)})</i>"
    if with_time:
        local = clock.from_ts(tx.created_at)
        if local.date() == tx.day:
            line += f" · {local:%H:%M}"
    return line


async def day_view(db: Database, day: date) -> tuple[str, InlineKeyboardMarkup]:
    cur = db.settings.currency
    today = clock.today()
    txs = await db.day_txs(day)
    incomes = [t for t in txs if t.kind == "income"]
    expenses = [t for t in txs if t.kind == "expense"]
    inc_sum = sum(t.amount for t in incomes)
    exp_sum = sum(t.amount for t in expenses)

    lines = [f"📅 <b>{WEEKDAYS[day.weekday()]}, {fmt_day(day, today)}</b>", ""]
    if not txs:
        lines.append("Записей нет." + (" Чистый день — ни рубля на ветер. 💪" if day <= today else ""))
    shown = 0
    if incomes:
        lines.append(f"💰 Доходы: <b>{money(inc_sum, cur, signed=True)}</b>")
        for t in incomes[:MAX_DAY_ITEMS]:
            lines.append(day_line(t, cur, with_time=False))
            shown += 1
        lines.append("")
    if expenses:
        lines.append(f"💸 Расходы: <b>{money(-exp_sum, cur)}</b>")
        for t in expenses[: max(0, MAX_DAY_ITEMS - shown)]:
            lines.append(day_line(t, cur, with_time=True))
            shown += 1
        lines.append("")
    if len(txs) > shown:
        lines.append(f"…и ещё {len(txs) - shown}")
    if txs:
        lines.append(f"Итог дня: <b>{money(inc_sum - exp_sum, cur, signed=True)}</b>")

    prev_day, next_day = day - timedelta(days=1), day + timedelta(days=1)
    nav = [
        btn(f"‹ {prev_day:%d.%m}", CalCb(action="d", y=prev_day.year, m=prev_day.month, d=prev_day.day)),
        btn("📅 Месяц", CalCb(action="m", y=day.year, m=day.month)),
    ]
    if next_day <= today:
        nav.append(btn(f"{next_day:%d.%m} ›", CalCb(action="d", y=next_day.year, m=next_day.month, d=next_day.day)))
    ymd = (day.year, day.month, day.day)
    markup = kb(
        [
            btn("➕ Расход", CalCb(action="add", y=ymd[0], m=ymd[1], d=ymd[2], arg="expense")),
            btn("➕ Доход", CalCb(action="add", y=ymd[0], m=ymd[1], d=ymd[2], arg="income")),
        ]
        if day <= today
        else None,
        [btn("🗑 Удалить запись", DelCb(action="list", id=0, ctx=f"{day:%Y%m%d}"))] if txs else None,
        nav,
    )
    return "\n".join(lines), markup


async def show_day(event: Event, db: Database, day: date) -> None:
    text, markup = await day_view(db, day)
    await render(event, text, markup)


def _date(cb: CalCb) -> date | None:
    try:
        return date(cb.y, cb.m, cb.d or 1)
    except ValueError:
        return None


@router.callback_query(CalCb.filter(F.action == "m"))
async def cb_month(cb: CallbackQuery, callback_data: CalCb, db: Database) -> None:
    if not 1 <= callback_data.m <= 12 or not 2000 <= callback_data.y <= 2200:
        return
    await show_month(cb, db, callback_data.y, callback_data.m)


@router.callback_query(CalCb.filter(F.action == "d"))
async def cb_day(cb: CallbackQuery, callback_data: CalCb, db: Database) -> None:
    day = _date(callback_data)
    if day is not None:
        await show_day(cb, db, day)


@router.callback_query(CalCb.filter(F.action == "add"))
async def cb_add(
    cb: CallbackQuery, callback_data: CalCb, state: FSMContext, db: Database, callback_answer: CallbackAnswer
) -> None:
    day = _date(callback_data)
    if day is None:
        return
    if day > clock.today():
        callback_answer.text = "Будущее ещё не наступило"
        return
    await state.clear()
    kind = "income" if callback_data.arg == "income" else "expense"
    await start_entry(cb, state, db, kind, day)
