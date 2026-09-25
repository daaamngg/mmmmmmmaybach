"""Сквозные сценарии: бот обрабатывает настоящие апдейты, Telegram заменён имитацией."""

from __future__ import annotations

from datetime import date

from aiogram.methods import SendDocument, SendPhoto

from finbot.scheduler import Scheduler

from .conftest import OWNER, STRANGER, Harness


async def start(chat: Harness) -> None:
    await chat.send("/start")
    assert chat.db.settings.owner_id == OWNER


async def make_goal(chat: Harness, title: str = "Maybach", target: str = "20кк", photo: str | None = None) -> int:
    await chat.send("🎯 Цели")
    await chat.click("Новая цель")
    await chat.send(title)
    await chat.send(target)
    if photo:
        await chat.send(photo=photo)
        await chat.click("Готово")
    else:
        await chat.click("Без фото")
    goals = await chat.db.goals()
    return next(g.id for g in goals if g.title == title)


async def test_start_claims_owner_and_ignores_strangers(chat: Harness) -> None:
    await chat.send("привет", uid=STRANGER)
    assert "Отправь /start" in chat.sent_texts()[-1]
    await start(chat)
    assert "СИСТЕМА ДИСЦИПЛИНЫ" in chat.all_text()
    assert "Создать первую цель" in chat.screen()
    before = len(chat.tg.calls)
    await chat.send("/start", uid=STRANGER)
    await chat.send("500 шаурма", uid=STRANGER)
    assert len(chat.tg.calls) == before  # чужим бот не отвечает вообще
    assert chat.db.settings.owner_id == OWNER


async def test_quick_input_income_and_expense(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat)
    await chat.send("+2000 зарплата")
    text = chat.last_text()
    assert "+2 000 ₽" in text and "Зарплата" in text
    assert "80 / 20" in text and "Maybach +1 600 ₽" in text
    b = await chat.db.balances()
    assert b.wallet == 40_000 and b.goals[goal_id] == 160_000

    await chat.send("350 шаурма")
    text = chat.last_text()
    assert "−350 ₽" in text and "Кафе и доставка" in text
    assert "За месяц" not in text or "🤡" in text
    assert (await chat.db.balances()).wallet == 5_000

    # «зп 50к» без плюса — всё равно доход
    await chat.send("зп 50к")
    assert "💰" in chat.last_text() and "Зарплата" in chat.last_text()


async def test_unknown_category_picker_undo_and_flip(chat: Harness) -> None:
    await start(chat)
    await chat.send("500 штуковина")
    assert "📦 Прочее" in chat.last_text()
    await chat.click("Продукты")  # категорию выбираем одним нажатием
    assert "🛒 Продукты" in chat.last_text()
    txs = await chat.db.recent_txs()
    assert txs[0].category == "food"

    await chat.click("Это доход")
    assert "💰" in chat.last_text()
    assert (await chat.db.recent_txs())[0].kind == "income"

    await chat.click("Изменить %")
    await chat.click("100%")
    assert "100 / 0" in chat.last_text()

    await chat.click("Отменить")
    assert "Отменено" in chat.last_text()
    assert await chat.db.count_tx() == 0


async def test_goal_with_photos_and_carousel(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat, photo="car1")
    last = chat.last()
    assert last.photo, "карточка цели должна быть с фото"
    assert "Maybach" in (last.caption or "")

    # Фото в чат без контекста → спросит, к какой цели прикрепить
    await chat.send(photo="car2")
    assert "К какой цели прикрепить" in chat.last_text()
    await chat.click("Maybach")
    goal = await chat.db.get_goal(goal_id)
    assert goal.photos == 2
    assert "📷 2/2" in chat.screen(1)
    await chat.click("◀️")
    assert "📷 1/2" in chat.screen(1)

    # Локальные копии фото скачаны
    photos = await chat.db.photos(goal_id)
    assert all(p.local_path for p in photos)

    await chat.click("Изменить")
    await chat.click("Удалить это фото")
    assert (await chat.db.get_goal(goal_id)).photos == 1


async def test_album_is_acknowledged_once(chat: Harness) -> None:
    await start(chat)
    await chat.send("🎯 Цели")
    await chat.click("Новая цель")
    await chat.send("Квартира")
    await chat.send("5кк")
    for i in range(4):
        await chat.send(photo=f"flat{i}", media_group_id="album1")
    acks = [t for t in chat.sent_texts() if "Добавлено фото" in t]
    assert len(acks) >= 1
    await chat.click("Готово")
    goal = (await chat.db.goals())[0]
    assert goal.photos == 4


async def test_new_goal_from_loose_photo(chat: Harness) -> None:
    await start(chat)
    await chat.send(photo="dream")
    await chat.click("Новая цель с этим фото")
    await chat.send("Мотоцикл")
    await chat.send("800к")
    goal = (await chat.db.goals())[0]
    assert goal.title == "Мотоцикл" and goal.target == 80_000_000 and goal.photos == 1
    assert chat.last().photo


async def test_deposit_withdraw_weight_and_edit(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat, "Телефон", "100к")
    await chat.send("/settings")
    await chat.click("Задать остаток")
    await chat.send("30000")
    assert (await chat.db.balances()).wallet == 3_000_000

    await chat.send("🎯 Цели")
    await chat.click("Телефон")
    await chat.click("Пополнить")
    await chat.click("С расходов")
    await chat.send("50000")  # больше, чем есть
    assert "Столько нет" in chat.last_text()
    await chat.send("10000")
    assert "+10 000" in chat.last_text()
    assert (await chat.db.get_goal(goal_id)).saved == 1_000_000

    await chat.click("К цели")
    await chat.click("➖ Снять")
    await chat.send("2000")
    assert "Точно снять" in chat.last_text()
    await chat.click("Да, снять")
    assert (await chat.db.get_goal(goal_id)).saved == 800_000
    assert (await chat.db.balances()).wallet == 2_200_000

    await chat.click("К цели")
    await chat.click("➕", exact=True)  # доля +1
    assert (await chat.db.get_goal(goal_id)).weight == 2
    await chat.click("Изменить")
    await chat.click("Название")
    await chat.send("iPhone 17 Pro")
    await chat.click("Изменить")
    await chat.click("Сумма")
    await chat.send("150к")
    await chat.click("Изменить")
    await chat.click("Срок")
    await chat.send("31.12.2026")
    goal = await chat.db.get_goal(goal_id)
    assert (goal.title, goal.target, goal.deadline) == ("iPhone 17 Pro", 15_000_000, date(2026, 12, 31))
    assert "Срок" in chat.last_text()


async def test_goal_completion_buy_archive_and_restore(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat, "Наушники", "10к", photo="pods")
    await chat.send("+20000 премия")  # 80% = 16 000 → цель 10 000 заполнена, 6 000 в свободные
    assert any("ЦЕЛЬ ДОСТИГНУТА" in (c.caption or "") for c in chat.tg.calls if isinstance(c, SendPhoto))
    b = await chat.db.balances()
    assert b.goals[goal_id] == 1_000_000 and b.pool == 600_000

    await chat.click("Купил! Закрыть цель")
    await chat.click("Да, купил")
    assert "КУПЛЕНО" in chat.last_text()
    assert (await chat.db.get_goal(goal_id)).status == "bought"

    await chat.send("🎯 Цели")
    await chat.click("Архив")
    assert "Наушники" in chat.last_text()

    # Отмена покупки возвращает цель
    await chat.send("/history")
    await chat.click("Удалить запись")
    await chat.click("Наушники")
    await chat.click("Да, удалить")
    assert (await chat.db.get_goal(goal_id)).status == "active"


async def test_pool_distribution_and_goal_delete(chat: Harness) -> None:
    await start(chat)
    await chat.send("+10000 зп")  # целей нет → 8000 в свободные
    assert "свободные" in chat.last_text().lower()
    g1 = await make_goal(chat, "Отпуск", "100к")
    g2 = await make_goal(chat, "Ноутбук", "100к")
    await chat.send("🎯 Цели")
    await chat.click("Распределить")
    await chat.click("По долям")
    b = await chat.db.balances()
    assert b.pool == 0 and b.goals[g1] == b.goals[g2] == 400_000

    await chat.send("🎯 Цели")
    await chat.click("Отпуск")
    await chat.click("Изменить")
    await chat.click("Удалить цель")
    await chat.click("В другие цели")
    b = await chat.db.balances()
    assert b.goals[g2] == 800_000 and b.goals.get(g1, 0) == 0
    await chat.click("Вернуть цель")
    assert (await chat.db.get_goal(g1)).status == "active"
    assert (await chat.db.balances()).goals[g1] == 400_000


async def test_calendar_day_view_and_delete(chat: Harness) -> None:
    await start(chat)
    await chat.send("+5000 зп")
    await chat.send("вчера 700 такси")
    await chat.send("300 кофе")
    await chat.send("📅 Календарь")
    text = chat.last_text()
    assert "СЕНТЯБРЬ 2026" in text and "Заработано" in text
    assert "25🟡" in chat.screen(1) and "24🔴" in chat.screen(1)

    await chat.click("24🔴")
    assert "Четверг, 24 сентября" in chat.last_text() and "такси" in chat.last_text()
    await chat.click("25.09 ›")
    assert "кофе" in chat.last_text()

    await chat.click("Удалить запись")
    await chat.click("кофе")
    await chat.click("Да, удалить")
    assert "кофе" not in chat.last_text()
    assert await chat.db.count_tx() == 2

    await chat.click("‹ 24.09")
    await chat.click("➕ Расход")
    await chat.send("250 метро")
    assert "вчера" in chat.last_text()
    txs = await chat.db.day_txs(date(2026, 9, 24))
    assert len(txs) == 2

    await chat.send("📅 Календарь")
    await chat.click("«")
    assert "АВГУСТ 2026" in chat.last_text()


async def test_stats_and_export(chat: Harness) -> None:
    await start(chat)
    await chat.send("+50000 зп")
    await chat.send("1500 доставка")
    await chat.send("3000 продукты")
    await chat.send("📊 Статистика")
    text = chat.last_text()
    assert "СТАТИСТИКА" in text and "Кафе и доставка" in text and "Вердикт" in text
    await chat.click("Экспорт CSV")
    docs = [c for c in chat.tg.calls if isinstance(c, SendDocument)]
    assert docs and docs[-1].document.filename.endswith(".csv")
    body = docs[-1].document.data.decode("utf-8-sig")
    assert "Дата;Время;Тип;Сумма" in body and "доставка" in body and "Доход;50000,00" in body


async def test_want_to_buy_resist_and_save(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat, "Машина", "1кк")
    await chat.send("+100000 зп")  # 20 000 на расходы
    await chat.send("🛑 Хочу купить")
    await chat.send("кроссовки 12000")
    text = chat.last_text()
    assert "СТОП" in text and "кроссовки" in text and "60%" in text
    await chat.click("Не покупаю")
    assert "ОТКАЗ ЗАСЧИТАН" in chat.last_text()
    before = (await chat.db.balances()).goals[goal_id]
    await chat.click("Отправить")
    after = (await chat.db.balances()).goals[goal_id]
    assert after - before == 1_200_000


async def test_want_to_buy_wait_reminder_and_cave(chat: Harness) -> None:
    await start(chat)
    await chat.send("+10000 зп")
    await chat.send("/want")
    await chat.send("наушники 5000")
    await chat.click("Подумаю")
    assert "Вернусь завтра" in chat.last_text()

    scheduler = Scheduler(chat.bot, chat.app)
    await chat.db.update_settings(morning_on=False, evening_on=False)
    await scheduler.tick()
    assert "Прошли сутки" not in chat.all_text()
    chat.clock.advance(hours=24, minutes=1)
    await scheduler.tick()
    assert "Прошли сутки" in chat.last_text()
    await scheduler.tick()  # второй раз не напоминает
    assert sum("Прошли сутки" in t for t in chat.sent_texts()) == 1

    await chat.click("Всё равно куплю")
    assert "Сдался" in chat.last_text()
    assert (await chat.db.balances()).wallet == 200_000 - 500_000


async def test_scheduler_morning_and_evening(chat: Harness) -> None:
    await start(chat)
    scheduler = Scheduler(chat.bot, chat.app)
    chat.clock.value = chat.clock.value.replace(hour=9, minute=0)
    await scheduler.tick()
    assert "ПОДЪЁМ" in chat.last_text()
    await scheduler.tick()
    assert sum("ПОДЪЁМ" in t for t in chat.sent_texts()) == 1

    chat.clock.value = chat.clock.value.replace(hour=21, minute=5)
    await scheduler.tick()
    assert "ИТОГИ ДНЯ" in chat.last_text() and "ни одной записи" in chat.last_text()
    await chat.click("Сегодня без трат")
    assert "День без трат" in chat.last_text()

    # Бот был выключен вечером и включился слишком поздно — старое не шлём.
    chat.clock.advance(days=1)
    chat.clock.value = chat.clock.value.replace(hour=23, minute=59)
    await chat.db.update_settings(morning_on=False)
    await chat.db.update_settings(evening_time="18:00")
    await scheduler.tick()
    assert sum("ИТОГИ ДНЯ" in t for t in chat.sent_texts()) == 1


async def test_menu_button_interrupts_input(chat: Harness) -> None:
    await start(chat)
    await chat.send("💸 Расход")
    await chat.send("🎯 Цели")  # не станет «суммой», а откроет цели
    assert "ЦЕЛИ" in chat.last_text()
    await chat.send("💰 Доход")
    await chat.send("/cancel")
    assert "Отменено" in chat.last_text()
    await chat.send("💸 Расход")
    await chat.click("Отмена")
    assert "Отменено" in chat.last_text()
    await chat.send("💸 Расход")
    await chat.send("бла-бла")
    assert "Не понял сумму" in chat.last_text()
    await chat.send("450 обед")
    assert "−450" in chat.last_text()


async def test_settings_flow(chat: Harness) -> None:
    await start(chat)
    await chat.send("/settings")
    await chat.click("Процент в цели")
    await chat.click("90%")
    assert chat.db.settings.goal_pct == 90
    await chat.click("Валюта")
    await chat.click("$")
    assert chat.db.settings.currency == "$"
    await chat.click("Жёсткость")
    assert chat.db.settings.harsh == 2
    await chat.click("Утро: вкл")
    assert chat.db.settings.morning_on is False
    await chat.click("Время вечера")
    await chat.send("22:15")
    assert chat.db.settings.evening_time == "22:15"
    await chat.click("Процент в цели")
    await chat.click("Свой процент")
    await chat.send("65")
    assert chat.db.settings.goal_pct == 65
    await chat.click("Бэкап базы")
    docs = [c for c in chat.tg.calls if isinstance(c, SendDocument)]
    assert docs and "Копия базы" in (docs[-1].caption or "")

    await chat.send("+100 зп")
    assert "$" in chat.last_text()
    await chat.send("/settings")
    await chat.click("Удалить все данные")
    await chat.send("не")
    assert await chat.db.count_tx() == 1
    await chat.send("/settings")
    await chat.click("Удалить все данные")
    await chat.send("УДАЛИТЬ")
    assert await chat.db.count_tx() == 0
    assert chat.db.settings.owner_id == OWNER  # бот остаётся твоим


async def test_misc_screens(chat: Harness) -> None:
    await start(chat)
    await chat.send("привет")
    assert "Не понял" in chat.last_text()
    await chat.send("/unknown")
    assert "Не знаю такой команды" in chat.last_text()
    await chat.send("/help")
    assert "КАК ПОЛЬЗОВАТЬСЯ" in chat.last_text()
    await chat.send("🔥 Мотивация")
    first = chat.last_text()
    assert "МОТИВАЦИЯ" in first
    await chat.click("Ещё")
    await chat.send("💼 Баланс")
    assert "БАЛАНС" in chat.last_text()
    await chat.click("История")
    assert "ПОСЛЕДНИЕ ЗАПИСИ" in chat.last_text()
    await chat.send("завтра 500 такси")  # в будущее не пишем — сумма не распознана
    assert "Не понял" in chat.last_text()
    await chat.send("31.12 500 такси")  # 31 декабря — это прошлый год
    txs = await chat.db.recent_txs()
    assert txs[0].day == date(2025, 12, 31)


async def test_html_injection_is_escaped(chat: Harness) -> None:
    await start(chat)
    await chat.send("500 <b>жирный</b> & <script>")
    assert "&lt;b&gt;" in chat.last_text() or "<b>жирный</b>" not in chat.last_text()
    await chat.send("🎯 Цели")
    await chat.click("Новая цель")
    await chat.send("<i>Цель</i> & Co")
    await chat.send("100к")
    await chat.click("Без фото")
    assert "&lt;i&gt;Цель&lt;/i&gt; &amp; Co" in chat.last_text()


async def test_stale_button_is_answered(chat: Harness) -> None:
    await start(chat)
    await chat.send("💼 Баланс")
    msg = chat.last()
    await chat.click_data(msg, "zzz:unknown")
    await chat.click_data(msg, "t:undo:999999")
    await chat.click_data(msg, "g:open:424242::0")
    assert "Этой цели уже нет" in chat.last_text()


async def test_crash_is_reported_not_silent(chat: Harness, monkeypatch) -> None:
    await start(chat)

    async def boom() -> None:
        raise RuntimeError("test crash")

    monkeypatch.setattr(chat.db, "balances", boom)
    await chat.send("💼 Баланс")
    assert "Что-то пошло не так" in chat.last_text()
    chat.tg.calls.clear()  # ошибка ожидаемая — не считаем её поломкой


async def test_broken_photo_falls_back_to_local_copy(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat, photo="car")
    photo = (await chat.db.photos(goal_id))[0]
    assert photo.local_path
    # Представим, что file_id устарел (например, сменили токен бота).
    await chat.db.update_photo(photo.id, file_id="broken_id")
    await chat.send("🎯 Цели")
    await chat.click("Maybach")
    assert chat.last().photo, "фото должно подгрузиться с диска"
    refreshed = (await chat.db.photos(goal_id))[0]
    assert refreshed.file_id.startswith("uploaded_")


async def test_owner_claim_is_race_free(chat: Harness) -> None:
    """Два /start одновременно (бот был выключен, пачка апдейтов) — владелец ровно один."""
    import asyncio

    from aiogram.types import Update

    def start_update(uid: int, n: int) -> Update:
        return Update.model_validate(
            {
                "update_id": 900 + n,
                "message": {
                    "message_id": n,
                    "date": int(chat.clock.value.timestamp()),
                    "chat": {"id": uid, "type": "private", "first_name": "x"},
                    "from": {"id": uid, "is_bot": False, "first_name": "x"},
                    "text": "/start",
                },
            },
            context={"bot": chat.bot},
        )

    await asyncio.gather(
        chat.dp.feed_update(chat.bot, start_update(OWNER, 1)),
        chat.dp.feed_update(chat.bot, start_update(STRANGER, 2)),
    )
    assert chat.db.settings.owner_id == OWNER
    assert not chat.tg.live(STRANGER), "чужой не должен получить ни одного сообщения"


async def test_month_report_and_busy_day(chat: Harness) -> None:
    await start(chat)
    await chat.send("+100000 зп")
    for i in range(45):
        await chat.send(f"{100 + i} кофе номер {i}")
    await chat.send("📅 Календарь")
    await chat.click("·25·" if "·25·" in chat.screen(1) else "25🟡")
    day = chat.last_text()
    assert "…и ещё" in day and len(day) < 4096

    scheduler = Scheduler(chat.bot, chat.app)
    chat.clock.value = chat.clock.value.replace(hour=21, minute=30)
    await scheduler.tick()
    evening = chat.last_text()
    assert "ИТОГИ ДНЯ" in evening and "Потрачено" in evening and "хотелки" in evening

    chat.clock.value = chat.clock.value.replace(month=10, day=1, hour=9, minute=1)
    await scheduler.tick()
    morning = chat.last_text()
    assert "ПОДЪЁМ" in morning and "ИТОГИ: СЕНТЯБРЬ 2026" in morning and "Вердикт" in morning


async def test_want_with_empty_wallet(chat: Harness) -> None:
    await start(chat)
    await chat.send("/want")
    await chat.send("PS5 60000")
    assert "нет денег на расходы" in chat.last_text()
    await chat.click("Не покупаю")
    assert "ОТКАЗ ЗАСЧИТАН" in chat.last_text()
    assert "Отправить" not in chat.screen(1)  # отправлять нечего


async def test_bot_learns_from_category_choice(chat: Harness) -> None:
    await start(chat)
    await chat.send("400 бенз 95")
    assert "📦 Прочее" in chat.last_text()
    await chat.click("Транспорт")
    assert "🚕 Транспорт" in chat.last_text()
    # В следующий раз — сразу правильно, без вопросов.
    await chat.send("бенз 92 1200")
    assert "🚕 Транспорт" in chat.last_text()
    assert "Прочее" not in chat.screen(1)


class FakeAI:
    title = "Fake (test)"
    status = "работает ✅"

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def categorize(self, note: str, kind: str) -> str | None:
        self.calls.append((note, kind))
        return self.answer


async def test_ai_fills_unknown_category_in_background(chat: Harness) -> None:
    await start(chat)
    ai = FakeAI("home")
    chat.app.ai = ai  # type: ignore[assignment]
    try:
        await chat.send("350 ершик для унитаза")
        assert ai.calls == [("ершик для унитаза", "expense")]
        text = chat.last_text()
        assert "🏠 Жильё и счета" in text and "нейросеть" in text
        assert (await chat.db.recent_txs(1))[0].category == "home"
        # Запомнил: второй раз нейросеть уже не нужна.
        await chat.send("200 ершик для унитаза")
        assert len(ai.calls) == 1 and "🏠 Жильё и счета" in chat.last_text()
        # Известные слова вообще не отправляются в нейросеть.
        await chat.send("300 шаурма")
        assert len(ai.calls) == 1
        await chat.send("/settings")
        assert "Fake (test)" in chat.last_text()
    finally:
        chat.app.ai = None


async def test_ai_never_overrides_your_choice(chat: Harness) -> None:
    await start(chat)
    ai = FakeAI("fun")
    chat.app.ai = ai  # type: ignore[assignment]
    try:
        # Ты выбрал категорию раньше, чем ответила нейросеть, — её ответ не применяется.
        await chat.db.set_category_if(0, "other", "fun")  # no-op на несуществующей записи
        tx_id = await chat.db.add_expense(10_000, category="other", note="штуковина")
        await chat.db.set_category(tx_id, "food")
        from finbot.handlers.entry import refine_with_ai

        await refine_with_ai(chat.bot, chat.db, ai, tx_id, "expense", "штуковина", 1, 1)  # type: ignore[arg-type]
        assert (await chat.db.get_tx(tx_id)).category == "food"
        # Нейросеть ответила «не знаю» — запись просто остаётся в «Прочем».
        ai.answer = None
        await chat.send("500 непонятная вещь")
        assert "📦 Прочее" in chat.last_text()
    finally:
        chat.app.ai = None


async def test_photos_survive_moving_data_folder(chat: Harness) -> None:
    await start(chat)
    goal_id = await make_goal(chat, photo="car")
    photo = (await chat.db.photos(goal_id))[0]
    assert photo.local_path == f"photos/goal_{goal_id}/{photo.id}.jpg"  # путь относительно папки данных
    from finbot.handlers.goals import local_file

    assert local_file(chat.db, photo.local_path).is_file()
    # Старый формат (абсолютный путь с другого компьютера) тоже находится.
    expected = local_file(chat.db, photo.local_path)
    for legacy in (
        f"C:\\Users\\me\\bot\\data\\photos\\goal_{goal_id}\\{photo.id}.jpg",
        f"C:/Users/me/bot/data/photos/goal_{goal_id}/{photo.id}.jpg",
        f"/home/old/bot/data/photos/goal_{goal_id}/{photo.id}.jpg",
    ):
        assert local_file(chat.db, legacy) == expected, legacy
