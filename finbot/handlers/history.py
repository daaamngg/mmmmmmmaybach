"""История записей и удаление записи из списка (с подтверждением)."""

from __future__ import annotations

from datetime import date, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_answer import CallbackAnswer

from .. import categories
from ..callbacks import DelCb, Nav
from ..db import Database, Tx, TxBlocked
from ..fmt import truncate
from ..money import money
from ..ui import Event, btn, kb, render
from .calendar import day_view
from .entry import tx_line

router = Router(name="history")

HISTORY_LIMIT = 20
PICK_LIMIT = 30


async def history_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    cur = db.settings.currency
    txs = await db.recent_txs(HISTORY_LIMIT)
    lines = ["🧾 <b>ПОСЛЕДНИЕ ЗАПИСИ</b>", ""]
    if not txs:
        lines.append("Пока пусто. Напиши, например, <code>350 шаурма</code> или <code>+2000 зп</code>.")
    for t in txs:
        lines.append(f"<code>{t.day:%d.%m}</code> {tx_line(t, cur)}")
    markup = kb(
        [btn("🗑 Удалить запись", DelCb(action="list", id=0, ctx="h"))] if txs else None,
        [btn("💼 Баланс", Nav(to="balance")), btn("📅 Календарь", Nav(to="calendar"))],
    )
    return "\n".join(lines), markup


async def show_history(event: Event, db: Database) -> None:
    text, markup = await history_view(db)
    await render(event, text, markup)


def _ctx_day(ctx: str) -> date | None:
    try:
        return datetime.strptime(ctx, "%Y%m%d").date()
    except ValueError:
        return None


async def _context(db: Database, ctx: str) -> tuple[str, InlineKeyboardMarkup, list[Tx]]:
    """Базовый экран (история или день) и записи, которые можно удалить."""
    day = _ctx_day(ctx) if ctx != "h" else None
    if day is not None:
        text, markup = await day_view(db, day)
        txs = await db.day_txs(day)
    else:
        text, markup = await history_view(db)
        txs = await db.recent_txs(HISTORY_LIMIT)
    return text, markup, txs


def _label(tx: Tx, cur: str) -> str:
    cat = categories.get(tx.category)
    what = truncate(tx.note, 22) if tx.note else cat.name
    sign = {"income": "+", "expense": "−"}.get(tx.kind, "")
    return f"{cat.emoji if tx.kind in ('income', 'expense') else '🔁'} {sign}{money(tx.amount, cur)} · {what} · {tx.day:%d.%m}"


def _pick_markup(txs: list[Tx], ctx: str, cur: str, confirm_id: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for t in txs[:PICK_LIMIT]:
        if t.id == confirm_id:
            rows.append([btn(f"✅ Да, удалить: {_label(t, cur)}", DelCb(action="ok", id=t.id, ctx=ctx))])
            rows.append([btn("❌ Нет", DelCb(action="list", id=0, ctx=ctx))])
        else:
            rows.append([btn(f"🗑 {_label(t, cur)}", DelCb(action="ask", id=t.id, ctx=ctx))])
    rows.append([btn("« Готово", DelCb(action="back", id=0, ctx=ctx))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(DelCb.filter(F.action.in_({"list", "ask"})))
async def cb_pick(cb: CallbackQuery, callback_data: DelCb, db: Database) -> None:
    text, _, txs = await _context(db, callback_data.ctx)
    confirm = callback_data.id if callback_data.action == "ask" else 0
    await render(cb, text, _pick_markup(txs, callback_data.ctx, db.settings.currency, confirm))


@router.callback_query(DelCb.filter(F.action == "ok"))
async def cb_delete(cb: CallbackQuery, callback_data: DelCb, db: Database, callback_answer: CallbackAnswer) -> None:
    try:
        tx = await db.delete_tx(callback_data.id)
    except TxBlocked as e:
        callback_answer.text = e.reason
        callback_answer.show_alert = True
        return
    callback_answer.text = "🗑 Удалено" if tx else "Уже удалено"
    text, markup, _ = await _context(db, callback_data.ctx)
    await render(cb, text, markup)


@router.callback_query(DelCb.filter(F.action == "back"))
async def cb_back(cb: CallbackQuery, callback_data: DelCb, db: Database) -> None:
    text, markup, _ = await _context(db, callback_data.ctx)
    await render(cb, text, markup)
