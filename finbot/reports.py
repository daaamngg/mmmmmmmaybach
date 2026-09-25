"""Сводки: главный экран, утренний пинок, вечерний отчёт, итоги месяца, вердикты."""

from __future__ import annotations

import calendar
import random
from datetime import date, timedelta

from . import categories, clock, motivation
from .db import Database, Goal, PeriodSummary
from .fmt import (
    MONTHS,
    bar,
    duration,
    esc,
    fmt_day,
    month_bounds,
    pct,
    plural,
    shift_month,
)
from .money import money


def days_left_in_month(today: date) -> int:
    return calendar.monthrange(today.year, today.month)[1] - today.day + 1


def whole(value: float) -> int:
    """Округление вниз до целых рублей — для средних и лимитов копейки только мешают."""
    return int(value) // 100 * 100


def safe_per_day(wallet: int, today: date) -> int:
    """Сколько можно тратить в день, чтобы дотянуть до конца месяца."""
    return whole(max(0, wallet) / days_left_in_month(today))


def goal_shares(goals: list[Goal]) -> dict[int, float]:
    """Доля каждой цели в автоматических отчислениях (только цели, которые их получают)."""
    receiving = [g for g in goals if g.receives]
    total = sum(g.weight for g in receiving)
    return {g.id: g.weight / total for g in receiving} if total else {}


def main_goal(goals: list[Goal]) -> Goal | None:
    active = [g for g in goals if g.status == "active"]
    if not active:
        return None
    receiving = [g for g in active if g.receives]
    pool = receiving or active
    return max(pool, key=lambda g: (g.weight, -g.id))


def eta_days(goal: Goal, share: float, savings_per_day: float) -> float | None:
    """Через сколько дней цель будет достигнута при текущем темпе. None — если темпа нет."""
    if goal.reached:
        return 0.0
    rate = savings_per_day * share
    if rate <= 0:
        return None
    return goal.remaining / rate


async def impulse_streak(db: Database) -> int | None:
    """Сколько дней подряд без трат на хотелки. None — если данных ещё нет."""
    first = await db.first_day()
    if first is None:
        return None
    today = clock.today()
    last = await db.last_expense_day(categories.IMPULSE_KEYS)
    if last is None:
        return (today - first).days + 1
    return max(0, (today - last).days)


def impulse_total(summary: PeriodSummary) -> int:
    return sum(s for cat, s, _ in summary.by_category if cat in categories.IMPULSE_KEYS)


def verdict(summary: PeriodSummary, impulse: int) -> str:
    """Короткий жёсткий вердикт по месяцу."""
    spent = summary.expense - summary.goal_purchases
    if summary.income == 0 and spent == 0:
        return "Пусто. Либо ты ничего не делал, либо ничего не записывал. Оба варианта — плохо."
    if summary.income and spent > summary.income:
        return "Ты тратишь больше, чем зарабатываешь. Это прямая дорога в долги. Режь расходы. Сегодня."
    share = impulse / spent if spent else 0
    if share >= 0.4:
        return f"{pct(share)} трат — на хотелки. Ты работаешь на кафе и маркетплейсы, а не на свою мечту."
    if share >= 0.2:
        return "Каждый пятый рубль уходит на ерунду. Неплохо, но ты способен на большее. Правило 40%."
    if summary.income and summary.saved >= summary.income * 0.7:
        return "Мощно. Большая часть дохода работает на цели. Не расслабляйся — держи темп."
    return "Нормально. Но «нормально» — это для обычных людей. Ты хочешь быть обычным?"


def stat_lines(summary: PeriodSummary, cur: str) -> list[str]:
    lines = [
        f"💰 Заработано: <b>{money(summary.income, cur)}</b>",
        f"💸 Потрачено: <b>{money(summary.expense, cur)}</b>",
    ]
    if summary.goal_purchases:
        lines.append(f"   из них покупки целей: {money(summary.goal_purchases, cur)}")
    lines.append(f"🎯 Отложено в цели: <b>{money(summary.saved, cur)}</b>")
    return lines


async def dashboard_text(db: Database) -> str:
    s = db.settings
    cur = s.currency
    today = clock.today()
    b = await db.balances()
    first, last = month_bounds(today.year, today.month)
    summ = await db.summary(first, last)
    goals = await db.goals()
    streak = await impulse_streak(db)

    lines = ["💼 <b>БАЛАНС</b>", "", f"💸 На расходы: <b>{money(b.wallet, cur)}</b>"]
    if b.wallet > 0:
        left = days_left_in_month(today)
        lines.append(
            f"   ≈ {money(safe_per_day(b.wallet, today), cur)} в день до конца месяца "
            f"({left} {plural(left, 'день', 'дня', 'дней')})"
        )
    elif b.wallet < 0:
        lines.append("   🚨 <b>Ты в минусе.</b> Сейчас ты тратишь деньги своих целей.")
    lines.append(f"🎯 В целях: <b>{money(b.in_goals, cur)}</b>")
    if b.pool:
        lines.append(f"🆓 Свободные накопления: <b>{money(b.pool, cur)}</b>")
    lines += ["━━━━━━━━━━━━━━", f"🏦 Всего: <b>{money(b.total, cur)}</b>", ""]
    lines.append(f"📅 <b>{MONTHS[today.month]}</b>")
    lines += ["   " + line for line in stat_lines(summ, cur)]
    lines.append("")
    if streak == 0:
        lines.append("🔥 Серия без хотелок: <b>обнулена сегодня</b>. Завтра начинай заново.")
    elif streak is not None:
        lines.append(f"🔥 Без трат на хотелки: <b>{streak} {plural(streak, 'день', 'дня', 'дней')} подряд</b>")
    goal = main_goal(goals)
    if goal:
        lines.append(f"🏁 Главная цель: <b>{esc(goal.title)}</b> — {pct(goal.progress)}")
        lines.append(f"<code>{bar(goal.progress)}</code>")
    lines.append(f"⚖️ Правило: {s.goal_pct}% в цели / {100 - s.goal_pct}% на жизнь")
    lines.append("")
    ctx = "overspent" if b.wallet < 0 else "general"
    lines.append(motivation.quote(ctx, s.harsh))
    return "\n".join(lines)


async def month_report_text(db: Database, y: int, m: int) -> str:
    cur = db.settings.currency
    first, last = month_bounds(y, m)
    summ = await db.summary(first, last)
    impulse = impulse_total(summ)
    since = clock.local_ts(first)
    until = clock.local_ts(last + timedelta(days=1))
    wishes = await db.wish_stats(since, until)
    lines = [f"📊 <b>ИТОГИ: {MONTHS[m].upper()} {y}</b>", "", *stat_lines(summ, cur)]
    if impulse:
        share = impulse / max(1, summ.expense - summ.goal_purchases)
        lines.append(f"🤡 На хотелки: <b>{money(impulse, cur)}</b> ({pct(share)} трат)")
    if wishes.resisted:
        lines.append(f"🛑 Отказался от покупок: {wishes.resisted} на {money(wishes.resisted_sum, cur)}")
    lines += ["", f"<b>Вердикт:</b> {verdict(summ, impulse)}"]
    return "\n".join(lines)


async def morning_text(db: Database) -> str:
    s = db.settings
    cur = s.currency
    today = clock.today()
    b = await db.balances()
    goals = await db.goals()
    streak = await impulse_streak(db)
    lines = ["🌅 <b>ПОДЪЁМ.</b>", "", motivation.quote("morning", s.harsh), ""]
    if b.wallet > 0:
        lines.append(
            f"💸 На расходы: {money(b.wallet, cur)} → лимит на сегодня "
            f"<b>≈ {money(safe_per_day(b.wallet, today), cur)}</b>"
        )
    elif b.wallet < 0:
        lines.append(f"🚨 На расходы: <b>{money(b.wallet, cur)}</b>. Сегодня — ноль трат на хотелки.")
    else:
        lines.append("💸 На расходы: 0. Сегодня ты ничего не тратишь. Вообще.")
    goal = main_goal(goals)
    if goal and goal.reached:
        lines.append(f"🏁 «{esc(goal.title)}» — ✅ достигнута. Закрой её и ставь новую. Выше.")
    elif goal:
        lines.append(f"🏁 «{esc(goal.title)}»: {pct(goal.progress)} — осталось {money(goal.remaining, cur)}")
    else:
        lines.append("🏁 Целей нет. Человек без цели просто тратит деньги. Поставь цель сегодня.")
    if streak:
        lines.append(f"🔥 Без трат на хотелки: {streak} {plural(streak, 'день', 'дня', 'дней')}. Не обнуляй.")
    if today.day == 1:
        py, pm = shift_month(today.year, today.month, -1)
        lines += ["", await month_report_text(db, py, pm)]
    return "\n".join(lines)


async def evening_text(db: Database) -> tuple[str, bool]:
    """Текст вечернего отчёта и признак «сегодня ничего не записано»."""
    s = db.settings
    cur = s.currency
    today = clock.today()
    summ = await db.summary(today, today)
    b = await db.balances()
    empty = summ.income_count == 0 and summ.expense_count == 0
    lines = [f"🌙 <b>ИТОГИ ДНЯ</b> · {fmt_day(today, today)}", ""]
    if empty:
        lines += ["📭 Сегодня ни одной записи.", "", motivation.quote("evening_empty", s.harsh)]
        return "\n".join(lines), True
    n = summ.expense_count
    lines.append(f"💸 Потрачено: <b>{money(summ.expense, cur)}</b> ({n} {plural(n, 'запись', 'записи', 'записей')})")
    if summ.income:
        lines.append(f"💰 Заработано: <b>{money(summ.income, cur)}</b>")
    impulse = impulse_total(summ)
    if impulse:
        lines.append(f"🤡 Из них на хотелки: <b>{money(impulse, cur)}</b>")
    lines.append(f"💼 Осталось на расходы: <b>{money(b.wallet, cur)}</b>")
    lines.append("")
    if summ.expense == 0:
        ctx = "no_spend"
    elif b.wallet < 0:
        ctx = "overspent"
    elif impulse:
        ctx = "expense_impulse"
    else:
        ctx = "evening"
    lines.append(motivation.quote(ctx, s.harsh))
    return "\n".join(lines), False


async def personal_line(db: Database) -> str | None:
    """Персональный укол по реальным цифрам — для раздела «Мотивация»."""
    cur = db.settings.currency
    today = clock.today()
    first, last = month_bounds(today.year, today.month)
    summ = await db.summary(first, last)
    goals = await db.goals()
    goal = main_goal(goals)
    options: list[str] = []
    impulse = impulse_total(summ)
    if impulse and goal:
        options.append(
            f"За этот месяц ты слил на хотелки <b>{money(impulse, cur)}</b>. "
            f"Это {pct(impulse / goal.target)} от «{esc(goal.title)}». "
            "Каждая такая трата — кирпич, вынутый из твоей мечты."
        )
    elif impulse:
        options.append(
            f"За этот месяц на хотелки ушло <b>{money(impulse, cur)}</b>. Подумай, что ты мог бы на это построить."
        )
    wishes = await db.wish_stats()
    if wishes.resisted:
        options.append(
            f"Ты уже {wishes.resisted} {plural(wishes.resisted, 'раз', 'раза', 'раз')} сказал соблазну «нет» "
            f"и сохранил <b>{money(wishes.resisted_sum, cur)}</b>. Не останавливайся."
        )
    streak = await impulse_streak(db)
    if streak and streak >= 2:
        options.append(f"🔥 {streak} {plural(streak, 'день', 'дня', 'дней')} без трат на хотелки. Не смей обнулять.")
    if goal and not goal.reached:
        _, savings_per_day = await db.daily_rates()
        days = eta_days(goal, goal_shares(goals).get(goal.id, 0.0), savings_per_day)
        if days:
            options.append(
                f"При текущем темпе «{esc(goal.title)}» будет твоей через <b>{duration(days)}</b>. "
                "Хочешь быстрее? Меньше трать, больше зарабатывай."
            )
    return random.choice(options) if options else None
