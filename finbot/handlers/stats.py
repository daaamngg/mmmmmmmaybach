"""Статистика месяца по категориям и выгрузка в CSV."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from aiogram import Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup

from .. import categories, clock
from ..callbacks import CalCb, Nav, StatCb
from ..db import Database, Tx
from ..fmt import bar, esc, month_bounds, month_title, pct, plural, shift_month
from ..money import money
from ..reports import impulse_total, stat_lines, verdict, whole
from ..ui import Event, btn, chat_id_of, kb, render

router = Router(name="stats")

KIND_NAMES = {"income": "Доход", "expense": "Расход", "transfer": "Перевод", "adjust": "Корректировка"}


async def stats_view(db: Database, y: int, m: int) -> tuple[str, InlineKeyboardMarkup]:
    cur = db.settings.currency
    today = clock.today()
    first, last = month_bounds(y, m)
    summ = await db.summary(first, last)
    py, pm = shift_month(y, m, -1)
    prev = await db.summary(*month_bounds(py, pm))
    impulse = impulse_total(summ)
    spent = summ.expense - summ.goal_purchases
    prev_spent = prev.expense - prev.goal_purchases
    wishes = await db.wish_stats(clock.local_ts(first), clock.local_ts(last + timedelta(days=1)))

    lines = [f"📊 <b>СТАТИСТИКА · {month_title(y, m).upper()}</b>", "", *stat_lines(summ, cur)]
    if summ.income:
        lines.append(f"💯 Норма сбережений: <b>{pct(max(0, summ.saved) / summ.income)}</b>")
    if (y, m) == (today.year, today.month):
        days = today.day
    elif (y, m) < (today.year, today.month):
        days = last.day
    else:
        days = 0
    if days and spent:
        lines.append(f"📆 В среднем в день: <b>{money(whole(spent / days), cur)}</b>")
    if prev_spent and spent:
        change = (spent - prev_spent) / prev_spent
        arrow = "📈" if change > 0 else "📉"
        sign = "+" if change > 0 else "−"
        lines.append(f"{arrow} Траты к прошлому месяцу: <b>{sign}{pct(abs(change))}</b>")

    if summ.by_category:
        lines += ["", "<b>РАСХОДЫ ПО КАТЕГОРИЯМ</b>"]
        for key, total, n in summ.by_category[:14]:
            cat = categories.get(key)
            share = total / summ.expense if summ.expense else 0
            clown = " 🤡" if cat.tier == "impulse" else ""
            times = f"{n} {plural(n, 'раз', 'раза', 'раз')}"
            lines.append(f"{cat.emoji} {esc(cat.name)}{clown} — <b>{money(total, cur)}</b> · {pct(share)} · {times}")
            lines.append(f"<code>{bar(share)}</code>")
    if impulse and spent:
        lines += ["", f"🤡 На хотелки: <b>{money(impulse, cur)}</b> — {pct(impulse / spent)} всех трат"]
    if wishes.resisted or wishes.bought:
        lines.append(
            f"🛑 «Хочу купить»: устоял {wishes.resisted} ({money(wishes.resisted_sum, cur)}), "
            f"сдался {wishes.bought} ({money(wishes.bought_sum, cur)})"
        )
    lines += ["", f"<b>Вердикт:</b> {verdict(summ, impulse)}"]

    ny, nm = shift_month(y, m, 1)
    has_next = (y, m) < (today.year, today.month)
    markup = kb(
        [
            btn("«", StatCb(y=py, m=pm)),
            btn(month_title(y, m), Nav(to="noop")),
            btn("»", StatCb(y=ny, m=nm)) if has_next else btn(" ", Nav(to="noop")),
        ],
        [btn("📅 Календарь", CalCb(action="m", y=y, m=m)), btn("📤 Экспорт CSV", Nav(to="export"))],
    )
    return "\n".join(lines), markup


async def show_stats(event: Event, db: Database, y: int | None = None, m: int | None = None) -> None:
    today = clock.today()
    text, markup = await stats_view(db, y or today.year, m or today.month)
    await render(event, text, markup)


@router.callback_query(StatCb.filter())
async def cb_stats(cb: CallbackQuery, callback_data: StatCb, db: Database) -> None:
    if 1 <= callback_data.m <= 12 and 2000 <= callback_data.y <= 2200:
        await show_stats(cb, db, callback_data.y, callback_data.m)


def _csv_amount(value: int) -> str:
    major, minor = divmod(abs(value), 100)
    return ("-" if value < 0 else "") + f"{major},{minor:02d}"


def build_csv(txs: list[Tx], titles: dict[int, str]) -> bytes:
    """CSV для Excel: разделитель «;», кодировка UTF-8 с BOM (кириллица откроется правильно)."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата", "Время", "Тип", "Сумма", "Категория", "Комментарий", "В цели", "На расходы", "Цели"])
    for t in txs:
        local = clock.from_ts(t.created_at)
        cat = categories.get(t.category).name if t.kind in ("income", "expense") else ""
        to_goals = t.to_goals
        saved = sum(to_goals.values()) + t.to_pool
        goals = ", ".join(f"{titles.get(g, '?')}: {_csv_amount(a)}" for g, a in to_goals.items())
        writer.writerow(
            [
                t.day.strftime("%d.%m.%Y"),
                local.strftime("%H:%M") if local.date() == t.day else "",
                KIND_NAMES.get(t.kind, t.kind),
                _csv_amount(t.amount),
                cat,
                t.note or "",
                _csv_amount(saved) if saved else "",
                _csv_amount(t.to_wallet) if t.to_wallet else "",
                goals,
            ]
        )
    return ("﻿" + buf.getvalue()).encode("utf-8")


async def send_export(event: Event, db: Database) -> None:
    bot = event.bot
    assert bot is not None
    txs = await db.all_txs()
    if not txs:
        await bot.send_message(chat_id_of(event), "📤 Выгружать пока нечего — записей нет.")
        return
    titles = {g.id: g.title for g in await db.goals(None)}
    data = build_csv(txs, titles)
    name = f"finbot_{clock.today():%Y-%m-%d}.csv"
    await bot.send_document(
        chat_id_of(event),
        BufferedInputFile(data, filename=name),
        caption=f"📤 Все записи: {len(txs)}. Открывается в Excel и Google Таблицах.",
    )
