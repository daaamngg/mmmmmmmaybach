"""Доходы и расходы: быстрый ввод, ввод по кнопке, карточка записи и её правка."""

from __future__ import annotations

from contextlib import suppress
from datetime import date

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer

from .. import categories, classify, clock, motivation, texts
from ..ai import AICategorizer
from ..callbacks import TxCb
from ..context import App
from ..db import Database, Tx, TxBlocked
from ..fmt import esc, fmt_day, fmt_day_rel, month_bounds, pct, plural, truncate
from ..keyboards import cancel_kb
from ..money import money
from ..parsing import parse_entry
from ..ui import Event, ask, btn, chunks, finish, kb, render, spawn
from . import goals as goals_ui

router = Router(name="entry")

PCT_OPTIONS = (0, 50, 70, 80, 90, 100)


class EntryFlow(StatesGroup):
    amount = State()


# ─────────────────────────── запись ───────────────────────────


def message_moment(message: Message) -> tuple[date, int]:
    """Дата и время, когда сообщение было отправлено (важно, если бот был выключен)."""
    local = clock.to_local(message.date)
    return local.date(), int(message.date.timestamp())


async def record(
    message: Message,
    db: Database,
    app: App,
    kind: str,
    amount: int,
    note: str,
    day: date,
    created_at: int | None = None,
) -> None:
    """Сохраняет доход/расход и показывает карточку записи."""
    note_value = note or None
    category = await classify.resolve(db, note, kind)
    completed: tuple[int, ...] = ()
    if kind == "income":
        res = await db.add_income(
            amount, category=category or categories.DEFAULT_INCOME, note=note_value, day=day, created_at=created_at
        )
        tx_id, completed, mode = res.tx_id, res.completed, "main"
    else:
        tx_id = await db.add_expense(
            amount, category=category or categories.DEFAULT_EXPENSE, note=note_value, day=day, created_at=created_at
        )
        # Категорию не узнали — сразу предлагаем выбрать одним нажатием.
        mode = "cats" if category is None else "main"
    text, markup = await tx_view(db, tx_id, mode=mode)
    sent = await message.answer(text, reply_markup=markup)
    if category is None and note and app.ai is not None and message.bot is not None:
        # Нейросеть думает в фоне: запись уже сохранена и показана, лагов нет.
        spawn(refine_with_ai(message.bot, db, app.ai, tx_id, kind, note, sent.chat.id, sent.message_id))
    if completed:
        await goals_ui.celebrate(message, db, completed)


async def refine_with_ai(
    bot: Bot, db: Database, ai: AICategorizer, tx_id: int, kind: str, note: str, chat_id: int, message_id: int
) -> None:
    category = await ai.categorize(note, kind)
    default = categories.default_for(kind)
    if category is None or category == default:
        return
    # Меняем, только если ты ещё не выбрал категорию сам.
    if not await db.set_category_if(tx_id, default, category):
        return
    await classify.remember(db, note, kind, category, source="ai")
    text, markup = await tx_view(db, tx_id, ai_note=True)
    with suppress(TelegramAPIError):
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)


async def start_entry(event: Event, state: FSMContext, db: Database, kind: str, day: date | None = None) -> None:
    await state.set_state(EntryFlow.amount)
    await state.update_data(kind=kind, day=day.isoformat() if day else None)
    if kind == "income":
        text = (
            "💰 <b>Сколько заработал?</b>\n"
            "Например: <code>2000</code> или <code>50к зарплата</code>\n\n"
            f"{db.settings.goal_pct}% сразу уйдёт в цели."
        )
    else:
        text = "💸 <b>Сколько потратил и на что?</b>\nНапример: <code>350 шаурма</code> или <code>1.5к продукты</code>"
    today = clock.today()
    if day is not None and day != today:
        text += f"\n\n📅 Дата: <b>{fmt_day(day, today)}</b>"
    await ask(event, state, text, cancel_kb())


@router.message(EntryFlow.amount, F.text)
async def entry_amount(message: Message, state: FSMContext, db: Database, app: App) -> None:
    data = await state.get_data()
    today = clock.today()
    parsed = parse_entry(message.text or "", today)
    if parsed is None:
        await message.answer("🤨 Не понял сумму. Напиши, например: <code>350 шаурма</code>", reply_markup=cancel_kb())
        return
    msg_day, created_at = message_moment(message)
    fixed_day = date.fromisoformat(data["day"]) if data.get("day") else None
    day = fixed_day or parsed.day or msg_day
    if day > today:
        await message.answer("⏳ Будущее ещё не наступило. Записывай то, что уже случилось.")
        return
    await finish(message, state)
    await record(message, db, app, data.get("kind", "expense"), parsed.amount, parsed.note, day, created_at)


# ─────────────────────────── быстрый ввод ───────────────────────────


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def quick_input(message: Message, db: Database, app: App) -> None:
    today = clock.today()
    parsed = parse_entry(message.text or "", today)
    if parsed is None:
        await message.answer(texts.DONT_UNDERSTAND)
        return
    if parsed.sign == "+":
        kind = "income"
    elif parsed.sign == "-":
        kind = "expense"
    else:
        kind = await classify.guess_kind(db, parsed.note)
    msg_day, created_at = message_moment(message)
    day = parsed.day or msg_day
    if day > today:
        await message.answer("⏳ Будущее ещё не наступило. Записывай то, что уже случилось.")
        return
    await record(message, db, app, kind, parsed.amount, parsed.note, day, created_at)


# ─────────────────────────── карточка записи ───────────────────────────


def tx_line(tx: Tx, cur: str) -> str:
    """Одна строка о записи — для отмены, истории, списков."""
    cat = categories.get(tx.category)
    note = f" · {esc(truncate(tx.note, 40))}" if tx.note else ""
    if tx.kind == "income":
        return f"💰 {money(tx.amount, cur, signed=True)} · {cat.label}{note}"
    if tx.kind == "expense":
        return f"💸 {money(-tx.amount, cur)} · {cat.label}{note}"
    if tx.kind == "transfer":
        return f"🔁 {money(tx.amount, cur)}{note or ' · перевод'}"
    wallet = tx.to_wallet
    value = wallet if wallet else tx.amount
    return f"⚙️ {money(value, cur, signed=True)}{note or ' · корректировка'}"


async def tx_view(
    db: Database, tx_id: int, mode: str = "main", *, ai_note: bool = False
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и кнопки карточки записи. mode: main | cats | pct."""
    tx = await db.get_tx(tx_id)
    if tx is None:
        return "↩️ Эта запись уже удалена.", None
    s = db.settings
    cur = s.currency
    today = clock.today()
    cat = categories.get(tx.category)
    b = await db.balances()
    lines: list[str] = []

    if tx.kind == "income":
        lines.append(f"💰 <b>{money(tx.amount, cur, signed=True)}</b> · {cat.label}")
    else:
        lines.append(f"💸 <b>{money(-tx.amount, cur)}</b> · {cat.label}")
    if tx.note and tx.note.lower() != cat.name.lower():
        lines.append(f"📝 {esc(tx.note)}")
    if tx.day != today:
        lines.append(f"📅 {fmt_day_rel(tx.day, today)}")
    if ai_note:
        lines.append("🤖 Категорию подобрала нейросеть. Не так — жми «🏷 Категория».")
    lines.append("")

    if tx.kind == "income":
        goal_pct = tx.goal_pct if tx.goal_pct is not None else s.goal_pct
        to_goals = tx.to_goals
        saved = sum(to_goals.values()) + tx.to_pool
        lines.append(f"⚖️ Распределение {goal_pct} / {100 - goal_pct}:")
        if saved:
            lines.append(f"🎯 В цели: <b>{money(saved, cur)}</b>")
            all_goals = {g.id: g for g in await db.goals(None)}
            for gid, amount in to_goals.items():
                g = all_goals.get(gid)
                if g is not None:
                    lines.append(f"   • {esc(truncate(g.title, 30))} +{money(amount, cur)} → {pct(g.progress)}")
            if tx.to_pool:
                lines.append(f"   • 🆓 Свободные накопления +{money(tx.to_pool, cur)}")
        lines.append(f"💸 На расходы: <b>{money(tx.to_wallet, cur, signed=True)}</b> → теперь {money(b.wallet, cur)}")
        if tx.to_pool and not to_goals:
            lines += ["", motivation.quote("no_goals", s.harsh, seed=tx.id)]
        else:
            lines += ["", motivation.quote("income", s.harsh, seed=tx.id)]
    else:
        lines.append(f"💼 Осталось на расходы: <b>{money(b.wallet, cur)}</b>")
        if b.wallet < 0:
            lines.append("🚨 <b>ТЫ В МИНУСЕ.</b> Дальше ты тратишь деньги своих целей.")
        if cat.tier == "impulse":
            first, last = month_bounds(tx.day.year, tx.day.month)
            total, n = await db.category_total(cat.key, first, last)
            lines.append(
                f"🤡 На «{cat.name}» за месяц: <b>{money(total, cur)}</b> ({n} {plural(n, 'раз', 'раза', 'раз')})"
            )
        if b.wallet < 0:
            ctx = "overspent"
        elif tx.category == "goal":
            ctx = "goal_bought"
        else:
            ctx = {"impulse": "expense_impulse", "growth": "expense_growth"}.get(cat.tier, "expense_normal")
        lines += ["", motivation.quote(ctx, s.harsh, seed=tx.id)]

    return "\n".join(lines), tx_keyboard(tx, mode)


def tx_keyboard(tx: Tx, mode: str) -> InlineKeyboardMarkup:
    undo = btn("↩️ Отменить", TxCb(action="undo", id=tx.id))
    if mode == "cats":
        options = [
            btn(("✅ " if c.key == tx.category else "") + c.label, TxCb(action="setcat", id=tx.id, arg=c.key))
            for c in categories.for_kind(tx.kind)
        ]
        return kb(*chunks(options, 2), [btn("« Готово", TxCb(action="back", id=tx.id)), undo])
    if mode == "pct" and tx.kind == "income":
        options = [
            btn(("• " if p == tx.goal_pct else "") + f"{p}%", TxCb(action="setpct", id=tx.id, arg=str(p)))
            for p in PCT_OPTIONS
        ]
        return kb(options[:3], options[3:], [btn("« Назад", TxCb(action="back", id=tx.id))])
    if tx.category == "goal":
        return kb([undo])
    if tx.kind == "income":
        return kb(
            [btn("⚖️ Изменить %", TxCb(action="pct", id=tx.id)), btn("🏷 Категория", TxCb(action="cat", id=tx.id))],
            [btn("🔄 Это расход", TxCb(action="flip", id=tx.id)), undo],
        )
    return kb(
        [btn("🏷 Категория", TxCb(action="cat", id=tx.id)), btn("🔄 Это доход", TxCb(action="flip", id=tx.id))],
        [undo],
    )


async def _show(cb: CallbackQuery, db: Database, tx_id: int, mode: str = "main") -> None:
    text, markup = await tx_view(db, tx_id, mode)
    await render(cb, text, markup)


def _blocked(callback_answer: CallbackAnswer, e: TxBlocked) -> None:
    callback_answer.text = e.reason
    callback_answer.show_alert = True


@router.callback_query(TxCb.filter(F.action == "undo"))
async def tx_undo(cb: CallbackQuery, callback_data: TxCb, db: Database, callback_answer: CallbackAnswer) -> None:
    try:
        tx = await db.delete_tx(callback_data.id)
    except TxBlocked as e:
        _blocked(callback_answer, e)
        return
    if tx is None:
        callback_answer.text = "Уже отменено"
        await render(cb, "↩️ Эта запись уже отменена.")
        return
    callback_answer.text = "Отменено"
    await render(cb, f"↩️ <b>Отменено</b>\n<s>{tx_line(tx, db.settings.currency)}</s>")


@router.callback_query(TxCb.filter(F.action.in_({"cat", "pct", "back"})))
async def tx_mode(cb: CallbackQuery, callback_data: TxCb, db: Database) -> None:
    mode = {"cat": "cats", "pct": "pct"}.get(callback_data.action, "main")
    await _show(cb, db, callback_data.id, mode)


@router.callback_query(TxCb.filter(F.action == "setcat"))
async def tx_set_category(
    cb: CallbackQuery, callback_data: TxCb, db: Database, callback_answer: CallbackAnswer
) -> None:
    tx = await db.get_tx(callback_data.id)
    valid = {c.key for c in categories.for_kind(tx.kind)} if tx else set()
    if tx is None or callback_data.arg not in valid:
        callback_answer.text = "Запись не найдена"
        await _show(cb, db, callback_data.id)
        return
    await db.set_category(tx.id, callback_data.arg)
    # Запоминаем выбор: в следующий раз эти слова сразу попадут в нужную категорию.
    await classify.remember(db, tx.note, tx.kind, callback_data.arg)
    callback_answer.text = f"{categories.get(callback_data.arg).label} · запомнил"
    await _show(cb, db, tx.id)


@router.callback_query(TxCb.filter(F.action == "setpct"))
async def tx_set_pct(cb: CallbackQuery, callback_data: TxCb, db: Database, callback_answer: CallbackAnswer) -> None:
    try:
        value = int(callback_data.arg)
        res = await db.reapply_income(callback_data.id, max(0, min(100, value)))
    except TxBlocked as e:
        _blocked(callback_answer, e)
        return
    except ValueError:
        return
    if res is None:
        callback_answer.text = "Запись не найдена"
    await _show(cb, db, callback_data.id)
    if res is not None and res.completed:
        await goals_ui.celebrate(cb, db, res.completed)


@router.callback_query(TxCb.filter(F.action == "flip"))
async def tx_flip(cb: CallbackQuery, callback_data: TxCb, db: Database, callback_answer: CallbackAnswer) -> None:
    tx = await db.get_tx(callback_data.id)
    if tx is None:
        await _show(cb, db, callback_data.id)
        return
    new_kind = "expense" if tx.kind == "income" else "income"
    category = await classify.resolve(db, tx.note, new_kind) or categories.default_for(new_kind)
    try:
        flipped = await db.flip_tx(tx.id, category)
    except TxBlocked as e:
        _blocked(callback_answer, e)
        return
    if flipped is None:
        callback_answer.text = "Эту запись нельзя перевернуть"
        return
    new_id, completed = flipped
    callback_answer.text = "Теперь это доход" if new_kind == "income" else "Теперь это расход"
    await _show(cb, db, new_id)
    if completed:
        await goals_ui.celebrate(cb, db, completed)
