"""«Хочу купить» — проверка покупки до того, как ты её сделаешь."""

from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer

from .. import classify, clock, motivation
from ..callbacks import GoalCb, Nav, TxCb, WishCb
from ..db import Database, Wish
from ..fmt import duration, esc, pct
from ..keyboards import cancel_kb
from ..money import money
from ..parsing import parse_entry
from ..reports import goal_shares, main_goal, safe_per_day, whole
from ..ui import Event, ask, btn, finish, kb, render
from . import goals as goals_ui

router = Router(name="wish")

WAIT_HOURS = 24


class WishFlow(StatesGroup):
    item = State()


async def start_wish(event: Event, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WishFlow.item)
    await ask(
        event,
        state,
        "🛑 <b>ХОЧУ КУПИТЬ</b>\n\n"
        "Что и за сколько? Сначала я покажу, чего это тебе стоит на самом деле.\n\n"
        "Например: <code>кроссовки 12000</code>",
        cancel_kb(),
    )


async def analysis_text(db: Database, wish: Wish) -> str:
    s = db.settings
    cur = s.currency
    today = clock.today()
    b = await db.balances()
    goals = await db.goals()
    income_per_day, savings_per_day = await db.daily_rates()

    lines = [f"🛑 <b>СТОП.</b> «{esc(wish.title)}» за <b>{money(wish.amount, cur)}</b>", ""]
    if b.wallet <= 0:
        lines.append("• У тебя <b>нет денег на расходы</b>. Эта покупка — прямо из твоих целей.")
    elif wish.amount > b.wallet:
        lines.append(
            f"• Это <b>больше всех твоих денег на расходы</b> ({money(b.wallet, cur)}). Придётся лезть в цели."
        )
    else:
        after = b.wallet - wish.amount
        lines.append(
            f"• Это <b>{pct(wish.amount / b.wallet)}</b> всех твоих денег на расходы ({money(b.wallet, cur)})."
        )
        lines.append(
            f"• После покупки останется {money(after, cur)} → "
            f"по {money(safe_per_day(after, today), cur)} в день до конца месяца."
        )
    if income_per_day >= 1:
        lines.append(
            f"• Это <b>{duration(wish.amount / income_per_day)}</b> твоей работы "
            f"(в среднем ты зарабатываешь {money(whole(income_per_day), cur)} в день)."
        )
    goal = main_goal(goals)
    if goal is not None and not goal.reached:
        rate = savings_per_day * goal_shares(goals).get(goal.id, 0.0)
        if rate >= 1:
            lines.append(
                f"• Если отправить эти деньги в «{esc(goal.title)}», она станет ближе на "
                f"<b>{duration(wish.amount / rate)}</b>."
            )
        new_progress = (goal.saved + wish.amount) / goal.target
        if pct(new_progress) != pct(goal.progress):
            lines.append(
                f"• Прогресс «{esc(goal.title)}» вырос бы с {pct(goal.progress)} до <b>{pct(new_progress)}</b>."
            )
    lines += ["", motivation.quote("want", s.harsh, seed=wish.id)]
    return "\n".join(lines)


def decision_kb(wish_id: int) -> InlineKeyboardMarkup:
    return kb(
        [btn("💪 Не покупаю", WishCb(action="no", id=wish_id))],
        [btn(f"⏳ Подумаю {WAIT_HOURS} часа", WishCb(action="wait", id=wish_id))],
        [btn("🤡 Всё равно куплю", WishCb(action="buy", id=wish_id))],
    )


def reminder(wish: Wish, cur: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"⏰ <b>Прошли сутки.</b>\n\n"
        f"Ты всё ещё хочешь «{esc(wish.title)}» за <b>{money(wish.amount, cur)}</b>?\n\n"
        "Если желание пропало — это был просто импульс. И ты его только что победил."
    )
    markup = kb(
        [btn("💪 Уже не хочу", WishCb(action="no", id=wish.id))],
        [btn("🤡 Всё равно куплю", WishCb(action="buy", id=wish.id))],
    )
    return text, markup


@router.message(WishFlow.item, F.text)
async def wish_item(message: Message, state: FSMContext, db: Database) -> None:
    parsed = parse_entry(message.text or "", clock.today())
    if parsed is None:
        await message.answer("🤨 Напиши, что и сколько стоит: <code>кроссовки 12000</code>", reply_markup=cancel_kb())
        return
    await finish(message, state)
    title = (parsed.note or "Покупка")[:60]
    wish_id = await db.add_wish(title, parsed.amount)
    wish = await db.get_wish(wish_id)
    assert wish is not None
    await message.answer(await analysis_text(db, wish), reply_markup=decision_kb(wish_id))


async def _already(cb: CallbackQuery, db: Database, wish_id: int, callback_answer: CallbackAnswer) -> None:
    wish = await db.get_wish(wish_id)
    callback_answer.text = "Уже решено"
    if wish is not None:
        verdict = {"resisted": "💪 Ты отказался от этой покупки.", "bought": "🤡 Ты это купил."}.get(
            wish.status, "Решение уже принято."
        )
        await render(cb, f"«{esc(wish.title)}» — {money(wish.amount, db.settings.currency)}\n{verdict}")


@router.callback_query(WishCb.filter(F.action == "no"))
async def cb_no(cb: CallbackQuery, callback_data: WishCb, db: Database, callback_answer: CallbackAnswer) -> None:
    wish = await db.resist_wish(callback_data.id)
    if wish is None:
        await _already(cb, db, callback_data.id, callback_answer)
        return
    cur = db.settings.currency
    stats = await db.wish_stats()
    b = await db.balances()
    lines = [
        f"💪 <b>ОТКАЗ ЗАСЧИТАН.</b> «{esc(wish.title)}» — {money(wish.amount, cur)} остаются при тебе.",
        "",
        f"Всего отказов: <b>{stats.resisted}</b> на <b>{money(stats.resisted_sum, cur)}</b>.",
        "",
        motivation.quote("resisted", db.settings.harsh, seed=wish.id),
    ]
    move = min(wish.amount, b.wallet)
    markup = (
        kb([btn(f"💰 Отправить {money(move, cur)} в цели", WishCb(action="save", id=wish.id))]) if move > 0 else None
    )
    await render(cb, "\n".join(lines), markup)


@router.callback_query(WishCb.filter(F.action == "save"))
async def cb_save(cb: CallbackQuery, callback_data: WishCb, db: Database, callback_answer: CallbackAnswer) -> None:
    res = await db.save_wish(callback_data.id)
    if res is None:
        callback_answer.text = "Уже отправлено или на расходах пусто"
        callback_answer.show_alert = True
        return
    cur = db.settings.currency
    wish = await db.get_wish(callback_data.id)
    title = esc(wish.title) if wish else "покупки"
    titles = await goals_ui.goal_titles(db)
    if res.to_goals:
        lines = [f"✅ <b>{money(res.amount, cur)}</b> ушли в цели вместо «{title}»:"]
        lines += goals_ui.transfer_lines(res, titles, cur)
    else:
        lines = [
            f"✅ <b>{money(res.amount, cur)}</b> отложены в свободные накопления вместо «{title}».",
            "Все цели уже заполнены или на паузе — поставь новую, побольше.",
        ]
    lines += ["", motivation.quote("deposit", db.settings.harsh, seed=res.tx_id)]
    markup = kb([btn("↩️ Отменить", TxCb(action="undo", id=res.tx_id or 0)), btn("🎯 Цели", GoalCb(action="list"))])
    await render(cb, "\n".join(lines), markup)
    if res.completed:
        await goals_ui.celebrate(cb, db, res.completed)


@router.callback_query(WishCb.filter(F.action == "wait"))
async def cb_wait(cb: CallbackQuery, callback_data: WishCb, db: Database, callback_answer: CallbackAnswer) -> None:
    remind_at = clock.now() + timedelta(hours=WAIT_HOURS)
    wish = await db.postpone_wish(callback_data.id, int(remind_at.timestamp()))
    if wish is None:
        await _already(cb, db, callback_data.id, callback_answer)
        return
    callback_answer.text = f"Напомню через {WAIT_HOURS} ч"
    await render(
        cb,
        f"⏳ <b>Правильно.</b> Импульс живёт сутки.\n\n"
        f"Вернусь завтра в {remind_at:%H:%M} и спрошу, хочешь ли ты ещё «{esc(wish.title)}». "
        "Скорее всего — уже нет.",
    )


@router.callback_query(WishCb.filter(F.action == "buy"))
async def cb_buy(cb: CallbackQuery, callback_data: WishCb, db: Database, callback_answer: CallbackAnswer) -> None:
    current = await db.get_wish(callback_data.id)
    if current is None:
        return
    category = await classify.resolve(db, current.title, "expense") or "shopping"
    wish = await db.buy_wish(callback_data.id, category)
    if wish is None:
        await _already(cb, db, callback_data.id, callback_answer)
        return
    s = db.settings
    b = await db.balances()
    lines = [
        f"🤡 <b>Сдался.</b> «{esc(wish.title)}» — {money(-wish.amount, s.currency)} записано в расходы.",
        f"💼 Осталось на расходы: <b>{money(b.wallet, s.currency)}</b>",
    ]
    if b.wallet < 0:
        lines.append("🚨 <b>ТЫ В МИНУСЕ.</b> Эту покупку ты сделал за счёт своих целей.")
    lines += ["", motivation.quote("caved", s.harsh, seed=wish.id)]
    markup = kb(
        [btn("↩️ Отменить покупку", TxCb(action="undo", id=wish.tx_id or 0))], [btn("💼 Баланс", Nav(to="balance"))]
    )
    await render(cb, "\n".join(lines), markup)
