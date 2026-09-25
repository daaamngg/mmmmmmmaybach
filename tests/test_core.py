"""Деньги, разбор ввода, категории и учёт в базе."""

from __future__ import annotations

from datetime import date

import pytest

from finbot import categories
from finbot.db import Database, TxLocked, TxSpent
from finbot.money import allocate_capped, money, parse_amount, pct_part, split_weighted
from finbot.parsing import parse_deadline, parse_entry, parse_time

T = date(2026, 9, 25)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2000", 200000),
        ("2 000", 200000),
        ("2к", 200000),
        ("2k", 200000),
        ("1,5к", 150000),
        ("1.5кк", 150000000),
        ("2000.50", 200050),
        ("2000,5", 200050),
        ("1.500", 150000),
        ("15 000,50", 1500050),
        ("20 млн", 2000000000),
        ("3 ляма", 300000000),
        ("500р", 50000),
        ("5$", 500),
        ("+300", 30000),
        ("0", None),
        ("abc", None),
        ("", None),
        ("1.5000", None),
    ],
)
def test_parse_amount(text: str, expected: int | None) -> None:
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    ("text", "amount", "sign", "note", "day"),
    [
        ("350 шаурма", 35000, None, "шаурма", None),
        ("+2000 зарплата", 200000, "+", "зарплата", None),
        ("-500 такси", 50000, "-", "такси", None),
        ("−500 такси", 50000, "-", "такси", None),
        ("такси 500", 50000, None, "такси", None),
        ("зп +50к", 5000000, "+", "зп", None),
        ("вчера 500 такси", 50000, None, "такси", date(2026, 9, 24)),
        ("позавчера +5к фриланс", 500000, "+", "фриланс", date(2026, 9, 23)),
        ("12.09 1500 продукты", 150000, None, "продукты", date(2026, 9, 12)),
        ("28.12 300 подарок", 30000, None, "подарок", date(2025, 12, 28)),
        ("iPhone 15 120000", 12000000, None, "iPhone 15", None),
        ("500 кофе", 50000, None, "кофе", None),
        ("2 000 кофе", 200000, None, "кофе", None),
        ("100 р за проезд", 10000, None, "за проезд", None),
        ("1.5к доставка", 150000, None, "доставка", None),
    ],
)
def test_parse_entry(text: str, amount: int, sign: str | None, note: str, day: date | None) -> None:
    e = parse_entry(text, T)
    assert e is not None
    assert (e.amount, e.sign, e.note, e.day) == (amount, sign, note, day)


@pytest.mark.parametrize("text", ["привет", "кофе", "", "   ", "+", "-"])
def test_parse_entry_rejects(text: str) -> None:
    assert parse_entry(text, T) is None


def test_parse_time_and_deadline() -> None:
    assert parse_time("9") == "09:00"
    assert parse_time("21.30") == "21:30"
    assert parse_time("7 15") == "07:15"
    assert parse_time("25:00") is None
    assert parse_deadline("31.12.2027", T) == date(2027, 12, 31)
    assert parse_deadline("31.12", T) == date(2026, 12, 31)
    assert parse_deadline("01.01", T) == date(2027, 1, 1)
    assert parse_deadline("06.2027", T) == date(2027, 6, 30)
    assert parse_deadline("01.01.2020", T) is None
    assert parse_deadline("завтра", T) is None


def test_money_format() -> None:
    assert money(160000, "₽") == "1 600 ₽"
    assert money(-123456789, "₽") == "−1 234 567,89 ₽"
    assert money(150, "$", signed=True) == "+1,50 $"


def test_split_and_allocate() -> None:
    assert pct_part(200000, 80) == 160000
    assert pct_part(200100, 80) == 160100  # 1600.8 → 1601 ₽
    assert pct_part(150, 80) == 120
    assert pct_part(5000, 0) == 0 and pct_part(5000, 100) == 5000
    parts = split_weighted(160000, [1, 1, 1])
    assert sum(parts) == 160000 and all(p % 100 == 0 for p in parts)
    assert split_weighted(160050, [3, 1]) == [120050, 40000]
    alloc, left = allocate_capped(160000, [(1, 1, 50000), (2, 1, 1000000), (3, 0, 5000)])
    assert alloc == {1: 50000, 2: 110000, 3: 0} and left == 0
    alloc, left = allocate_capped(160000, [(1, 1, 50000), (2, 1, 30000)])
    assert alloc == {1: 50000, 2: 30000} and left == 80000
    assert allocate_capped(1000, []) == ({}, 1000)


@pytest.mark.parametrize(
    ("note", "kind", "expected"),
    [
        ("шаурма", "expense", "cafe"),
        ("продукты в пятерочке", "expense", "food"),
        ("такси домой", "expense", "transport"),
        ("кинопоиск", "expense", "subs"),
        ("Яндекс Го", "expense", "transport"),
        ("пиво", "expense", "vice"),
        ("бизнес ланч", "expense", "cafe"),
        ("wb", "expense", "shopping"),
        ("зал", "expense", "growth"),
        ("кредит", "expense", "debt"),
        ("что-то непонятное", "expense", None),
        ("зарплата", "income", "salary"),
        ("аванс", "income", "salary"),
        ("продал велик", "income", "business"),
    ],
)
def test_detect_category(note: str, kind: str, expected: str | None) -> None:
    assert categories.detect(note, kind) == expected


def test_guess_kind() -> None:
    assert categories.guess_kind("зарплата") == "income"
    assert categories.guess_kind("зп") == "income"
    assert categories.guess_kind("подарили") == "income"
    assert categories.guess_kind("шаурма") == "expense"
    assert categories.guess_kind("бизнес ланч") == "expense"
    assert categories.guess_kind("") == "expense"


# ─────────────────────────── база ───────────────────────────


async def test_income_split_between_goals(db: Database, frozen) -> None:
    car = await db.create_goal("Машина", 10_000_000)
    phone = await db.create_goal("Телефон", 100_000, weight=3)
    res = await db.add_income(200_000, category="salary")  # 2000 ₽, 80% → 1600 ₽
    assert res.to_wallet == 40_000
    assert sum(res.to_goals.values()) + res.to_pool == 160_000
    # Телефон (доля 3) влезает только на 1000 ₽ — остальное перетекает в машину.
    assert res.to_goals[phone] == 100_000 and res.to_goals[car] == 60_000
    assert res.completed == (phone,)
    b = await db.balances()
    assert (b.wallet, b.pool, b.in_goals, b.total) == (40_000, 0, 160_000, 200_000)


async def test_income_without_goals_goes_to_pool(db: Database, frozen) -> None:
    res = await db.add_income(100_000, category="salary", pct=50)
    assert res.to_pool == 50_000 and res.to_wallet == 50_000 and not res.to_goals


async def test_undo_restores_everything(db: Database, frozen) -> None:
    goal = await db.create_goal("Цель", 1_000_000)
    inc = await db.add_income(500_000, category="salary")
    exp = await db.add_expense(30_000, category="cafe", note="шаурма")
    b = await db.balances()
    assert b.wallet == 70_000 and b.goals[goal] == 400_000
    assert await db.delete_tx(exp) is not None
    assert await db.delete_tx(inc.tx_id) is not None
    assert await db.delete_tx(inc.tx_id) is None  # второй раз — уже нечего
    b = await db.balances()
    assert (b.wallet, b.pool, b.goals.get(goal, 0)) == (0, 0, 0)


async def test_reapply_and_flip(db: Database, frozen) -> None:
    await db.create_goal("Цель", 1_000_000)
    res = await db.add_income(100_000, category="salary")
    again = await db.reapply_income(res.tx_id, 100)
    assert again is not None and again.to_wallet == 0 and sum(again.to_goals.values()) == 100_000
    flipped = await db.flip_tx(res.tx_id, "other")
    assert flipped is not None
    new_id, _ = flipped
    tx = await db.get_tx(new_id)
    assert tx is not None and tx.kind == "expense" and tx.amount == 100_000
    b = await db.balances()
    assert b.wallet == -100_000 and b.in_goals == 0


async def test_transfers_are_strict(db: Database, frozen) -> None:
    goal = await db.create_goal("Цель", 1_000_000)
    await db.set_wallet(50_000)
    assert await db.transfer("wallet", "goal", 60_000, dst_goal=goal) is None
    res = await db.transfer("wallet", "goal", 50_000, dst_goal=goal)
    assert res is not None
    assert await db.transfer("wallet", "goal", 1, dst_goal=goal) is None
    assert await db.transfer("goal", "wallet", 50_001, src_goal=goal) is None
    out = await db.transfer("goal", "wallet", 20_000, src_goal=goal)
    assert out is not None
    b = await db.balances()
    assert b.wallet == 20_000 and b.goals[goal] == 30_000


async def test_pool_to_goals_moves_only_what_fits(db: Database, frozen) -> None:
    await db.add_income(100_000, category="salary")  # целей нет → 800 ₽ в свободные
    goal = await db.create_goal("Маленькая", 30_000)
    res = await db.transfer("pool", "goals", 80_000)
    assert res is not None and res.amount == 30_000 and res.completed == (goal,)
    b = await db.balances()
    assert b.pool == 50_000 and b.goals[goal] == 30_000


async def test_buy_and_delete_goal_lock_and_restore(db: Database, frozen) -> None:
    goal = await db.create_goal("Телефон", 100_000)
    other = await db.create_goal("Машина", 10_000_000)
    inc = await db.add_income(500_000, category="salary")
    bought = await db.buy_goal(goal)
    assert bought is not None
    tx_id, spent = bought
    assert spent == 100_000
    assert (await db.get_goal(goal)).status == "bought"
    # Доход, который пополнил купленную цель, отменить нельзя — иначе поедут балансы.
    with pytest.raises(TxLocked):
        await db.delete_tx(inc.tx_id)
    # А саму покупку — можно: цель вернётся.
    await db.delete_tx(tx_id)
    g = await db.get_goal(goal)
    assert g.status == "active" and g.saved == 100_000

    res = await db.delete_goal(other, "pool")
    assert res is not None and res.amount > 0
    b = await db.balances()
    assert b.pool == res.amount and other not in [g.id for g in await db.goals()]
    await db.delete_tx(res.tx_id)
    assert (await db.get_goal(other)).status == "active"


async def test_undo_cannot_resurrect_moved_money(db: Database, frozen) -> None:
    goal = await db.create_goal("Цель", 1_000_000)
    await db.set_wallet(100_000)
    dep = await db.transfer("wallet", "goal", 50_000, dst_goal=goal)
    await db.transfer("goal", "wallet", 50_000, src_goal=goal)  # потом всё сняли обратно
    # Отмена пополнения увела бы цель в −500 ₽ — запрещено.
    with pytest.raises(TxSpent):
        await db.delete_tx(dep.tx_id)

    inc = await db.add_income(100_000, category="salary")
    b = await db.balances()
    out = await db.transfer("goal", "wallet", b.goals[goal], src_goal=goal)
    await db.add_expense(150_000, category="other")  # снятое потрачено (на расходах осталось 500 ₽)
    # Доход, чья доля в цели уже снята, нельзя отменить, перевернуть или пересчитать в 0%.
    with pytest.raises(TxSpent):
        await db.delete_tx(inc.tx_id)
    with pytest.raises(TxSpent):
        await db.flip_tx(inc.tx_id, "other")
    with pytest.raises(TxSpent):
        await db.reapply_income(inc.tx_id, 0)
    # И снятие нельзя отменить, если снятые деньги уже потрачены.
    with pytest.raises(TxSpent):
        await db.delete_tx(out.tx_id)
    after = await db.balances()
    assert after.goals[goal] == 0 and after.pool == 0
    # Отмена расхода — всегда можно.
    txs = await db.recent_txs(1)
    assert await db.delete_tx(txs[0].id) is not None


async def test_wishes(db: Database, frozen) -> None:
    await db.create_goal("Цель", 1_000_000)
    await db.set_wallet(100_000)
    w = await db.add_wish("Кроссовки", 12_000)
    assert await db.resist_wish(w) is not None
    assert await db.resist_wish(w) is None
    res = await db.save_wish(w)
    assert res is not None and res.amount == 12_000
    assert await db.save_wish(w) is None  # второй раз не отправит
    w2 = await db.add_wish("Наушники", 20_000)
    bought = await db.buy_wish(w2, "shopping")
    assert bought is not None and bought.tx_id
    await db.delete_tx(bought.tx_id)
    assert (await db.get_wish(w2)).status == "pending"
    stats = await db.wish_stats()
    assert (stats.resisted, stats.resisted_sum, stats.bought) == (1, 12_000, 0)


async def test_summary_and_calendar(db: Database, frozen) -> None:
    await db.add_income(300_000, category="salary", day=date(2026, 9, 1))
    await db.add_expense(50_000, category="cafe", day=date(2026, 9, 2))
    await db.add_expense(20_000, category="food", day=date(2026, 9, 2))
    await db.add_expense(10_000, category="cafe", day=date(2026, 8, 31))
    s = await db.summary(date(2026, 9, 1), date(2026, 9, 30))
    assert (s.income, s.expense, s.income_count, s.expense_count, s.spend_days) == (300_000, 70_000, 1, 2, 1)
    assert s.by_category[0] == ("cafe", 50_000, 1)
    assert s.saved == 240_000
    days = await db.day_totals(date(2026, 9, 1), date(2026, 9, 30))
    assert days[date(2026, 9, 2)] == (0, 70_000) and days[date(2026, 9, 1)] == (300_000, 0)
    assert await db.first_day() == date(2026, 8, 31)


async def test_settings_persist(tmp_path, frozen) -> None:
    path = tmp_path / "s.db"
    d1 = Database(path)
    await d1.open()
    await d1.update_settings(goal_pct=70, currency="$", morning_on=False, owner_id=123)
    await d1.close()
    d2 = Database(path)
    await d2.open()
    s = d2.settings
    assert (s.goal_pct, s.currency, s.morning_on, s.owner_id, s.evening_on) == (70, "$", False, 123, True)
    await d2.close()
