"""Настройки: процент в цели, валюта, жёсткость, напоминания, остаток, бэкап, сброс."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer

from .. import clock, motivation
from ..callbacks import Nav, SetCb
from ..context import App
from ..db import Database
from ..fmt import esc
from ..keyboards import cancel_kb
from ..money import money, parse_amount
from ..parsing import parse_time
from ..ui import Event, ask, btn, chat_id_of, chunks, finish, kb, render

router = Router(name="settings")

PCT_CHOICES = (50, 60, 70, 75, 80, 85, 90, 100)
CURRENCIES = ("₽", "$", "€", "₴", "₸", "Br", "сом", "£")
WIPE_WORD = "УДАЛИТЬ"


class SettingsFlow(StatesGroup):
    pct = State()
    morning = State()
    evening = State()
    wallet = State()
    wipe = State()


def webapp_status(app: App) -> str:
    if app.webapp_ready:
        return "работает ✅ (кнопка «Приложение» слева от поля ввода)"
    if app.config.webapp_url:
        return "запускается — жду HTTPS-сертификат (проверка на сервере: finbot doctor)"
    return "не включено (на сервере: finbot webapp on)"


async def settings_view(db: Database, app: App) -> tuple[str, InlineKeyboardMarkup]:
    s = db.settings
    harsh = "☠️ Без цензуры" if s.harsh >= 2 else "🔥 Жёстко"
    learned = await db.learned_count()
    ai = f"<b>{esc(app.ai.title)}</b> — {esc(app.ai.status)}" if app.ai else "не подключена (см. README)"
    lines = [
        "⚙️ <b>НАСТРОЙКИ</b>",
        "",
        f"⚖️ С каждого дохода: <b>{s.goal_pct}%</b> в цели, <b>{100 - s.goal_pct}%</b> на расходы",
        f"💱 Валюта: <b>{s.currency}</b>",
        f"🔥 Жёсткость: <b>{harsh}</b>",
        f"🌅 Утренний пинок: <b>{s.morning_time if s.morning_on else 'выкл'}</b>",
        f"🌙 Вечерний отчёт: <b>{s.evening_time if s.evening_on else 'выкл'}</b>",
        "",
        f"🧠 Запомнил твоих слов для категорий: <b>{learned}</b>",
        f"🤖 Нейросеть для категорий: {ai}",
        f"📱 Приложение: {webapp_status(app)}",
    ]
    markup = kb(
        [btn("⚖️ Процент в цели", SetCb(action="pct")), btn("💱 Валюта", SetCb(action="cur"))],
        [btn(f"Жёсткость: {harsh}", SetCb(action="harsh"))],
        [
            btn(f"🌅 Утро: {'вкл' if s.morning_on else 'выкл'}", SetCb(action="mon")),
            btn("🕘 Время утра", SetCb(action="mtime")),
        ],
        [
            btn(f"🌙 Вечер: {'вкл' if s.evening_on else 'выкл'}", SetCb(action="eon")),
            btn("🕘 Время вечера", SetCb(action="etime")),
        ],
        [btn("💼 Задать остаток на расходы", Nav(to="wallet"))],
        [btn("📤 Экспорт CSV", Nav(to="export")), btn("💾 Бэкап базы", SetCb(action="backup"))],
        [btn("🧨 Удалить все данные", SetCb(action="wipe"))],
        [btn("« Баланс", Nav(to="balance"))],
    )
    return "\n".join(lines), markup


async def show_settings(event: Event, db: Database, app: App, note: str = "") -> None:
    text, markup = await settings_view(db, app)
    await render(event, (note + "\n\n" if note else "") + text, markup)


async def _show_new(message: Message, db: Database, app: App, note: str) -> None:
    text, markup = await settings_view(db, app)
    await message.answer(f"{note}\n\n{text}", reply_markup=markup)


@router.callback_query(SetCb.filter(F.action == "back"))
async def cb_back(cb: CallbackQuery, db: Database, app: App) -> None:
    await show_settings(cb, db, app)


# ── процент в цели ──


@router.callback_query(SetCb.filter(F.action == "pct"))
async def cb_pct(cb: CallbackQuery, db: Database) -> None:
    current = db.settings.goal_pct
    options = [btn(("• " if p == current else "") + f"{p}%", SetCb(action="pctset", arg=str(p))) for p in PCT_CHOICES]
    await render(
        cb,
        "⚖️ <b>Сколько процентов с каждого дохода отправлять в цели?</b>\n\n"
        "Остальное пойдёт на расходы. Гоггинс бы выбрал максимум, который ты выдержишь.\n"
        "Уже записанные доходы не пересчитываются — только новые.",
        kb(
            *chunks(options, 4),
            [btn("✏️ Свой процент", SetCb(action="pctcustom"))],
            [btn("« Назад", SetCb(action="back"))],
        ),
    )


@router.callback_query(SetCb.filter(F.action == "pctset"))
async def cb_pct_set(
    cb: CallbackQuery, callback_data: SetCb, db: Database, app: App, callback_answer: CallbackAnswer
) -> None:
    try:
        value = int(callback_data.arg)
    except ValueError:
        return
    await db.update_settings(goal_pct=max(0, min(100, value)))
    callback_answer.text = f"Теперь {value}% в цели"
    await show_settings(cb, db, app)


@router.callback_query(SetCb.filter(F.action == "pctcustom"))
async def cb_pct_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SettingsFlow.pct)
    await ask(cb, state, "✏️ Введи процент от 0 до 100:", cancel_kb())


@router.message(SettingsFlow.pct, F.text)
async def pct_input(message: Message, state: FSMContext, db: Database, app: App) -> None:
    raw = (message.text or "").strip().rstrip("%").strip()
    if not raw.isdigit() or not 0 <= int(raw) <= 100:
        await message.answer("Нужно целое число от 0 до 100.", reply_markup=cancel_kb())
        return
    await finish(message, state)
    await db.update_settings(goal_pct=int(raw))
    await _show_new(message, db, app, f"✅ Теперь {raw}% с каждого дохода — в цели.")


# ── валюта ──


@router.callback_query(SetCb.filter(F.action == "cur"))
async def cb_currency(cb: CallbackQuery, db: Database) -> None:
    current = db.settings.currency
    options = [btn(("• " if c == current else "") + c, SetCb(action="curset", arg=c)) for c in CURRENCIES]
    await render(
        cb,
        "💱 <b>Валюта</b>\n\nМеняется только значок — суммы не пересчитываются.",
        kb(*chunks(options, 4), [btn("« Назад", SetCb(action="back"))]),
    )


@router.callback_query(SetCb.filter(F.action == "curset"))
async def cb_currency_set(cb: CallbackQuery, callback_data: SetCb, db: Database, app: App) -> None:
    if callback_data.arg in CURRENCIES:
        await db.update_settings(currency=callback_data.arg)
    await show_settings(cb, db, app)


# ── жёсткость ──


@router.callback_query(SetCb.filter(F.action == "harsh"))
async def cb_harsh(cb: CallbackQuery, db: Database, app: App, callback_answer: CallbackAnswer) -> None:
    level = 1 if db.settings.harsh >= 2 else 2
    await db.update_settings(harsh=level)
    callback_answer.text = "☠️ Без цензуры. Ты сам попросил." if level == 2 else "🔥 Жёстко, но без мата"
    await show_settings(cb, db, app, motivation.quote("general", level))


# ── напоминания ──


@router.callback_query(SetCb.filter(F.action.in_({"mon", "eon"})))
async def cb_toggle(cb: CallbackQuery, callback_data: SetCb, db: Database, app: App) -> None:
    if callback_data.action == "mon":
        await db.update_settings(morning_on=not db.settings.morning_on)
    else:
        await db.update_settings(evening_on=not db.settings.evening_on)
    await show_settings(cb, db, app)


@router.callback_query(SetCb.filter(F.action.in_({"mtime", "etime"})))
async def cb_time(cb: CallbackQuery, callback_data: SetCb, state: FSMContext) -> None:
    await state.clear()
    morning = callback_data.action == "mtime"
    await state.set_state(SettingsFlow.morning if morning else SettingsFlow.evening)
    what = "утренний пинок" if morning else "вечерний отчёт"
    await ask(cb, state, f"🕘 Во сколько присылать {what}? Например: <code>8:30</code>", cancel_kb())


@router.message(SettingsFlow.morning, F.text)
@router.message(SettingsFlow.evening, F.text)
async def time_input(message: Message, state: FSMContext, db: Database, app: App) -> None:
    value = parse_time(message.text or "")
    if value is None:
        await message.answer(
            "🤨 Не понял время. Например: <code>8:30</code> или <code>21:00</code>", reply_markup=cancel_kb()
        )
        return
    morning = await state.get_state() == SettingsFlow.morning.state
    await finish(message, state)
    if morning:
        await db.update_settings(morning_time=value, morning_on=True)
    else:
        await db.update_settings(evening_time=value, evening_on=True)
    await _show_new(
        message, db, app, f"✅ Буду присылать {'утренний пинок' if morning else 'вечерний отчёт'} в {value}."
    )


# ── остаток на расходы ──


async def start_wallet(event: Event, state: FSMContext, db: Database) -> None:
    await state.clear()
    await state.set_state(SettingsFlow.wallet)
    b = await db.balances()
    await ask(
        event,
        state,
        "💼 <b>Сколько у тебя сейчас денег на расходы?</b>\n\n"
        "Всё, что на карте и в кошельке, <b>не считая</b> отложенного на цели. "
        "Я выставлю ровно эту сумму.\n\n"
        f"Сейчас в боте: {money(b.wallet, db.settings.currency)}",
        cancel_kb(),
    )


@router.message(SettingsFlow.wallet, F.text)
async def wallet_input(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    value = 0 if raw in ("0", "0,00", "0.00") else parse_amount(raw.lstrip("-−"))
    if value is None:
        await message.answer("🤨 Не понял сумму. Например: <code>15000</code>", reply_markup=cancel_kb())
        return
    if raw.startswith(("-", "−")):
        value = -value
    await finish(message, state)
    await db.set_wallet(value)
    cur = db.settings.currency
    await message.answer(
        f"✅ Остаток на расходы: <b>{money(value, cur)}</b>.\n\nТеперь записывай каждую трату. Я слежу. 👁"
    )


# ── бэкап и сброс ──


@router.callback_query(SetCb.filter(F.action == "backup"))
async def cb_backup(cb: CallbackQuery, db: Database, app: App, bot: Bot) -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="finbot-"))
    try:
        path = tmp_dir / f"finbot_backup_{clock.today():%Y-%m-%d}.db"
        await db.backup_to(str(path))
        await bot.send_document(
            chat_id_of(cb),
            FSInputFile(path),
            caption=(
                "💾 Копия базы. Чтобы восстановить: останови бота и положи этот файл вместо "
                f"<code>{app.config.db_path.name}</code> в папку data."
            ),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.callback_query(SetCb.filter(F.action == "wipe"))
async def cb_wipe(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SettingsFlow.wipe)
    await ask(
        cb,
        state,
        "🧨 <b>Удалить ВСЕ данные?</b>\n\nДоходы, расходы, цели, фото — всё. Это нельзя отменить.\n\n"
        f"Если уверен — напиши слово <code>{WIPE_WORD}</code>.",
        cancel_kb(),
    )


@router.message(SettingsFlow.wipe, F.text)
async def wipe_input(message: Message, state: FSMContext, db: Database, app: App) -> None:
    await finish(message, state)
    if (message.text or "").strip().upper() != WIPE_WORD:
        await message.answer("Отменено. Данные на месте.")
        return
    await db.wipe()
    shutil.rmtree(app.config.photos_dir, ignore_errors=True)
    await message.answer("🧨 Все данные удалены. Чистый лист. Начни заново — и в этот раз без слабостей. /start")
