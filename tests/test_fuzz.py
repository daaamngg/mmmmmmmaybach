"""Случайные нажатия и сообщения: бот не должен падать, а деньги — всегда сходиться до копейки."""

from __future__ import annotations

import os
import random

import pytest

from .conftest import OWNER, Harness

MESSAGES = [
    "+2000 зп", "350 шаурма", "такси 500", "вчера 700 такси", "12.09 1500 продукты", "1.5к доставка",
    "зп 50к", "500 штуковина", "кроссовки 12000", "Maybach", "20кк", "100к", "0", "-", "привет", "9:30",
    "31.12.2027", "УДАЛИТЬ", "65", "/cancel", "/balance", "/goals", "/calendar", "/stats", "/history",
    "💸 Расход", "💰 Доход", "🎯 Цели", "📅 Календарь", "💼 Баланс", "📊 Статистика", "🛑 Хочу купить",
    "🔥 Мотивация", "/settings", "/want", "<b>html</b> 300", "10000000000000000", "1,5", "25.09 +5000",
]  # fmt: skip
SKIP_BUTTONS = ("Удалить все данные",)


async def check_ledger(chat: Harness) -> None:
    db = chat.db
    txs = await db.all_txs()
    expected_total = 0
    for t in txs:
        moved = sum(m.amount for m in t.moves)
        if t.kind == "income":
            assert moved == t.amount, t
            expected_total += t.amount
        elif t.kind == "expense":
            assert moved == -t.amount, t
            expected_total -= t.amount
        elif t.kind == "transfer":
            assert moved == 0, t
        else:  # adjust
            assert abs(moved) == t.amount, t
            expected_total += moved
    b = await db.balances()
    assert b.wallet + b.pool + sum(b.goals.values()) == expected_total
    for g in await db.goals(None):
        if g.status != "active":  # закрытая цель всегда пустая
            assert b.goals.get(g.id, 0) == 0, g


SEEDS = int(os.environ.get("FUZZ_SEEDS", "12"))  # FUZZ_SEEDS=300 pytest tests/test_fuzz.py — долгий прогон


@pytest.mark.parametrize("seed", range(SEEDS))
async def test_random_clicking(chat: Harness, seed: int) -> None:
    rnd = random.Random(seed)
    await chat.send("/start")
    for step in range(120):
        roll = rnd.random()
        if roll < 0.45:
            await chat.send(rnd.choice(MESSAGES))
        elif roll < 0.5:
            await chat.send(photo=f"p{seed}_{step}")
        else:
            buttons = [
                (msg, b)
                for msg in chat.tg.live(OWNER)[-3:]
                if msg.reply_markup
                for row in msg.reply_markup.inline_keyboard
                for b in row
                if b.callback_data and not any(s in b.text for s in SKIP_BUTTONS)
            ]
            if buttons:
                msg, button = rnd.choice(buttons)
                await chat.click_data(msg, button.callback_data)
        chat.assert_healthy()
        if step % 20 == 0:
            await check_ledger(chat)
    await check_ledger(chat)
