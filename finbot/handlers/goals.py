"""Цели: список, карточка с фото, создание, пополнение, снятие, доли, покупка, удаление."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import date
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.callback_answer import CallbackAnswer

from .. import clock, motivation
from ..callbacks import GoalCb, Nav, TxCb
from ..context import App
from ..db import Database, Goal, Photo, TransferResult
from ..fmt import bar, duration, esc, fmt_day, pct, truncate
from ..keyboards import cancel_kb
from ..money import money, parse_amount
from ..parsing import parse_deadline
from ..reports import eta_days, goal_shares
from ..ui import (
    Event,
    PhotoSource,
    ask,
    btn,
    chat_id_of,
    finish,
    kb,
    render,
    send_screen,
    spawn,
)

log = logging.getLogger(__name__)
router = Router(name="goals")

MAX_TITLE = 60
MAX_WEIGHT = 10


class GoalNew(StatesGroup):
    title = State()
    target = State()


class GoalPhotos(StatesGroup):
    waiting = State()


class GoalEdit(StatesGroup):
    title = State()
    target = State()
    deadline = State()


class GoalMoney(StatesGroup):
    deposit = State()
    withdraw = State()


# ─────────────────────────── фото ───────────────────────────


def photo_source(photo: Photo) -> PhotoSource:
    return PhotoSource(photo.file_id, photo.file_unique_id, photo.local_path, row_id=photo.id)


async def remember_refreshed(db: Database, src: PhotoSource | None) -> None:
    """Если фото пришлось заново загрузить с диска — запоминаем новый file_id."""
    if src is not None and src.refreshed_id and src.row_id:
        await db.update_photo(src.row_id, file_id=src.refreshed_id)


async def save_local_copy(bot: Bot, db: Database, photos_dir: Path, photo_id: int, goal_id: int, file_id: str) -> None:
    """Локальная копия фото — чтобы картинки целей жили у тебя на диске."""
    dest = photos_dir / f"goal_{goal_id}" / f"{photo_id}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await bot.download(file_id, destination=dest)
    await db.update_photo(photo_id, local_path=str(dest))


async def add_photos(bot: Bot, db: Database, app: App, goal_id: int, photos: list[tuple[str, str | None]]) -> int:
    added = 0
    for file_id, unique_id in photos:
        photo_id = await db.add_photo(goal_id, file_id, unique_id)
        if photo_id is not None:
            added += 1
            spawn(save_local_copy(bot, db, app.config.photos_dir, photo_id, goal_id, file_id))
    return added


# ─────────────────────────── экраны ───────────────────────────


async def goals_list_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    cur = db.settings.currency
    goals = await db.goals()
    b = await db.balances()
    has_archive = bool(await db.goals("bought"))
    archive_row = [btn("🏆 Архив достигнутых", GoalCb(action="archive"))] if has_archive else None

    if not goals:
        text = (
            "🎯 <b>ЦЕЛИ</b>\n\n"
            "У тебя нет ни одной цели.\n"
            "Человек без цели просто тратит деньги. Создай первую — и прикрепи фото, "
            "чтобы видеть её каждый день."
        )
        if b.pool:
            text += f"\n\n🆓 Свободные накопления: <b>{money(b.pool, cur)}</b> — ждут свою цель."
        return text, kb([btn("➕ Новая цель", GoalCb(action="new"))], archive_row)

    shares = goal_shares(goals)
    lines = ["🎯 <b>ЦЕЛИ</b>", ""]
    for i, g in enumerate(goals, 1):
        mark = " ✅" if g.reached else (" ⏸" if g.weight == 0 else "")
        lines.append(f"<b>{i}. {esc(g.title)}</b>{mark}")
        lines.append(f"<code>{bar(g.progress)}</code> {pct(g.progress)}")
        share = shares.get(g.id)
        tail = f" · доля {pct(share)}" if share else ""
        lines.append(f"{money(g.saved, cur)} из {money(g.target, cur)}{tail}")
        lines.append("")
    lines.append(f"💰 Всего в целях: <b>{money(b.in_goals, cur)}</b>")
    if b.pool:
        lines.append(f"🆓 Свободные накопления: <b>{money(b.pool, cur)}</b>")

    rows: list[list[InlineKeyboardButton] | None] = [
        [
            btn(
                f"{'✅' if g.reached else '🎯'} {truncate(g.title, 26)} · {pct(g.progress)}",
                GoalCb(action="open", id=g.id),
            )
        ]
        for g in goals
    ]
    rows.append([btn("➕ Новая цель", GoalCb(action="new"))])
    if b.pool > 0:
        rows.append([btn(f"🆓 Распределить {money(b.pool, cur)}", GoalCb(action="pool"))])
    rows.append(archive_row)
    return "\n".join(lines), kb(*rows)


async def show_goals(event: Event, db: Database) -> None:
    text, markup = await goals_list_view(db)
    await render(event, text, markup)


async def goal_card_view(
    db: Database, goal: Goal, idx: int = 0, mode: str = "main"
) -> tuple[str, InlineKeyboardMarkup, PhotoSource | None]:
    s = db.settings
    cur = s.currency
    today = clock.today()
    photos = await db.photos(goal.id) if goal.photos else []
    if photos:
        idx %= len(photos)
    else:
        idx = 0
    goals = await db.goals()
    b = await db.balances()
    share = goal_shares(goals).get(goal.id, 0.0)
    _, savings_per_day = await db.daily_rates()
    eta = eta_days(goal, share, savings_per_day)

    lines = [f"🎯 <b>{esc(goal.title)}</b>", ""]
    lines.append(f"💰 <b>{money(goal.saved, cur)}</b> из {money(goal.target, cur)}")
    lines.append(f"<code>{bar(goal.progress)}</code> {pct(goal.progress)}")
    if goal.reached:
        lines.append("✅ <b>ЦЕЛЬ ДОСТИГНУТА!</b>")
    else:
        lines.append(f"Осталось: <b>{money(goal.remaining, cur)}</b>")
    lines.append("")
    if goal.weight == 0:
        lines.append("⏸ На паузе — не получает отчисления с доходов.")
    elif goal.reached:
        lines.append("Отчисления с доходов теперь идут в другие цели.")
    else:
        lines.append(f"⚖️ Доля {goal.weight} → {pct(share)} от отчислений в цели")
        if eta:
            lines.append(f"📈 При текущем темпе: ≈ <b>{duration(eta)}</b>")
            if eta > 3650:
                lines.append("⚠️ Это слишком долго. Экономией такую цель не взять — ищи, как зарабатывать больше.")
        else:
            lines.append("📈 Темп: за последние 30 дней отчислений не было")
    if goal.deadline and not goal.reached:
        days_left = (goal.deadline - today).days
        if days_left > 0:
            if days_left <= 60:
                need = f"{money(-(-goal.remaining // days_left), cur)} в день"
            else:
                need = f"{money(int(goal.remaining / (days_left / 30.44)), cur)} в месяц"
            lines.append(f"📆 Срок: {fmt_day(goal.deadline, today)} → нужно ≈ {need}")
            if eta is None or eta > days_left:
                lines.append("⚠️ В этом темпе не успеешь. Зарабатывай больше или трать меньше.")
        else:
            lines.append(f"📆 Срок ({fmt_day(goal.deadline, today)}) прошёл. Ты не успел. Сделай выводы.")
    if not photos:
        lines += ["", "📷 Добавь фото цели — смотреть на мечту каждый день мощно мотивирует."]

    gid = goal.id
    photo_nav = None
    if len(photos) > 1:
        photo_nav = [
            btn("◀️", GoalCb(action="open", id=gid, idx=idx - 1)),
            btn(f"📷 {idx + 1}/{len(photos)}", Nav(to="noop")),
            btn("▶️", GoalCb(action="open", id=gid, idx=idx + 1)),
        ]

    if mode == "edit":
        markup = kb(
            [btn("✏️ Название", GoalCb(action="etitle", id=gid)), btn("🎯 Сумма", GoalCb(action="etarget", id=gid))],
            [btn("📆 Срок", GoalCb(action="edl", id=gid)), btn("📷 Добавить фото", GoalCb(action="addph", id=gid))],
            [btn("🗑 Удалить это фото", GoalCb(action="phdel", id=gid, arg=str(photos[idx].id)))] if photos else None,
            [btn("🗑 Удалить цель", GoalCb(action="del", id=gid))],
            [btn("« Назад", GoalCb(action="open", id=gid, idx=idx))],
        )
    else:
        pool_row = None
        if b.pool > 0 and not goal.reached:
            pool_row = [btn(f"🆓 Забрать свободные ({money(b.pool, cur)})", GoalCb(action="poolto", id=gid))]
        markup = kb(
            [btn("➕ Пополнить", GoalCb(action="dep", id=gid)), btn("➖ Снять", GoalCb(action="wd", id=gid))],
            photo_nav,
            [
                btn("➖", GoalCb(action="w", id=gid, arg="-1", idx=idx)),
                btn(f"⚖️ Доля: {goal.weight}", Nav(to="noop")),
                btn("➕", GoalCb(action="w", id=gid, arg="1", idx=idx)),
            ],
            [btn("🛒 Купил! Закрыть цель", GoalCb(action="buy", id=gid))] if goal.reached and goal.saved > 0 else None,
            pool_row,
            [btn("📷 Фото", GoalCb(action="addph", id=gid)), btn("✏️ Изменить", GoalCb(action="edit", id=gid, idx=idx))],
            [btn("« Все цели", GoalCb(action="list"))],
        )
    photo = photo_source(photos[idx]) if photos else None
    return "\n".join(lines), markup, photo


async def show_goal(event: Event, db: Database, goal_id: int, idx: int = 0, mode: str = "main") -> None:
    goal = await db.get_goal(goal_id)
    if goal is None or goal.status != "active":
        text, markup = await goals_list_view(db)
        await render(event, "⚠️ Этой цели уже нет.\n\n" + text, markup)
        return
    if idx < 0 and goal.photos:
        idx %= goal.photos
    text, markup, photo = await goal_card_view(db, goal, idx, mode)
    await render(event, text, markup, photo)
    await remember_refreshed(db, photo)


async def cover(db: Database, goal: Goal) -> PhotoSource | None:
    if not goal.photos:
        return None
    photos = await db.photos(goal.id)
    return photo_source(photos[0]) if photos else None


async def celebrate(event: Event, db: Database, goal_ids: tuple[int, ...] | list[int]) -> None:
    """Праздничное сообщение (с фото цели), когда цель достигнута."""
    bot = event.bot
    assert bot is not None
    cur = db.settings.currency
    for gid in goal_ids:
        goal = await db.get_goal(gid)
        if goal is None:
            continue
        photo = await cover(db, goal)
        text = (
            f"🏆 <b>ЦЕЛЬ ДОСТИГНУТА: «{esc(goal.title)}»</b>\n\n"
            f"💰 {money(goal.saved, cur)} из {money(goal.target, cur)}\n\n"
            f"{motivation.quote('goal_done', db.settings.harsh)}"
        )
        markup = kb(
            [btn("🛒 Купил! Закрыть цель", GoalCb(action="buy", id=goal.id))],
            [btn("🎯 Открыть цель", GoalCb(action="open", id=goal.id))],
        )
        await send_screen(bot, chat_id_of(event), text, markup, photo)
        await remember_refreshed(db, photo)


def transfer_lines(res: TransferResult, titles: dict[int, str], cur: str) -> list[str]:
    lines = []
    for gid, amount in res.to_goals.items():
        lines.append(f"   • {esc(truncate(titles.get(gid, '?'), 30))} +{money(amount, cur)}")
    if res.to_pool:
        lines.append(f"   • 🆓 Свободные накопления +{money(res.to_pool, cur)}")
    return lines


async def goal_titles(db: Database) -> dict[int, str]:
    return {g.id: g.title for g in await db.goals(None)}


# ─────────────────────────── навигация ───────────────────────────


@router.callback_query(GoalCb.filter(F.action == "list"))
async def cb_list(cb: CallbackQuery, db: Database) -> None:
    await show_goals(cb, db)


@router.callback_query(GoalCb.filter(F.action == "open"))
async def cb_open(cb: CallbackQuery, callback_data: GoalCb, db: Database) -> None:
    await show_goal(cb, db, callback_data.id, callback_data.idx)


@router.callback_query(GoalCb.filter(F.action == "edit"))
async def cb_edit(cb: CallbackQuery, callback_data: GoalCb, db: Database) -> None:
    await show_goal(cb, db, callback_data.id, callback_data.idx, mode="edit")


@router.callback_query(GoalCb.filter(F.action == "w"))
async def cb_weight(cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer) -> None:
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        await show_goals(cb, db)
        return
    new = max(0, min(MAX_WEIGHT, goal.weight + (1 if callback_data.arg == "1" else -1)))
    if new == goal.weight:
        callback_answer.text = "Максимум 10" if new == MAX_WEIGHT else "Минимум 0 — цель на паузе"
        return
    await db.update_goal(goal.id, weight=new)
    callback_answer.text = "⏸ Цель на паузе" if new == 0 else f"Доля: {new}"
    await show_goal(cb, db, goal.id, callback_data.idx)


@router.callback_query(GoalCb.filter(F.action == "archive"))
async def cb_archive(cb: CallbackQuery, db: Database) -> None:
    cur = db.settings.currency
    today = clock.today()
    bought = await db.goals("bought")
    lines = ["🏆 <b>ДОСТИГНУТЫЕ ЦЕЛИ</b>", ""]
    for g in bought[:40]:
        when = fmt_day(clock.from_ts(g.closed_at).date(), today) if g.closed_at else ""
        lines.append(f"✅ <b>{esc(g.title)}</b> — {money(g.target, cur)} · {when}")
    if not bought:
        lines.append("Пока пусто. Первая взятая цель появится здесь.")
    else:
        lines += ["", f"Взято целей: <b>{len(bought)}</b>. Это не предел. Stay hard."]
    await render(cb, "\n".join(lines), kb([btn("« К целям", GoalCb(action="list"))]))


# ─────────────────────────── новая цель ───────────────────────────


async def start_new_goal(event: Event, state: FSMContext, photos: list[tuple[str, str | None]] | None = None) -> None:
    await state.clear()
    await state.set_state(GoalNew.title)
    if photos:
        await state.update_data(pending_photos=photos)
    await ask(
        event,
        state,
        "🎯 <b>НОВАЯ ЦЕЛЬ</b>\n\nКак она называется?\n"
        "Например: <i>Maybach</i>, <i>Квартира</i>, <i>Подушка безопасности</i>",
        cancel_kb(),
    )


@router.callback_query(GoalCb.filter(F.action == "new"))
async def cb_new(cb: CallbackQuery, state: FSMContext) -> None:
    await start_new_goal(cb, state)


@router.message(GoalNew.title, F.text)
async def new_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if not title or len(title) > MAX_TITLE:
        await message.answer(f"Название — от 1 до {MAX_TITLE} символов. Ещё раз:", reply_markup=cancel_kb())
        return
    await state.update_data(title=title)
    await state.set_state(GoalNew.target)
    await ask(
        message,
        state,
        f"💰 Сколько нужно накопить на «{esc(title)}»?\n"
        "Например: <code>150000</code>, <code>150к</code>, <code>20кк</code>",
        cancel_kb(),
    )


@router.message(GoalNew.target, F.text)
async def new_target(message: Message, state: FSMContext, db: Database, app: App, bot: Bot) -> None:
    target = parse_amount(message.text or "")
    if not target:
        await message.answer("🤨 Не понял сумму. Например: <code>150к</code>", reply_markup=cancel_kb())
        return
    data = await finish(message, state)
    goal_id = await db.create_goal(data["title"], target)
    pending = data.get("pending_photos") or []
    if pending:
        await add_photos(bot, db, app, goal_id, [tuple(p) for p in pending])
        await message.answer("🔥 Цель создана. Теперь каждый рубль имеет смысл.")
        await show_goal(message, db, goal_id)
        return
    await state.set_state(GoalPhotos.waiting)
    await state.update_data(goal_id=goal_id)
    await ask(
        message,
        state,
        "🔥 <b>Цель создана.</b>\n\n📸 Теперь скинь фото цели — машина, квартира, что угодно. "
        "Можно несколько.\nСмотреть на мечту каждый день — лучший способ не слить деньги.",
        kb([btn("⏭ Без фото", GoalCb(action="phdone", id=goal_id))]),
    )


# ─────────────────────────── фото целей ───────────────────────────


@router.callback_query(GoalCb.filter(F.action == "addph"))
async def cb_add_photos(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        await show_goals(cb, db)
        return
    await state.clear()
    await state.set_state(GoalPhotos.waiting)
    await state.update_data(goal_id=goal.id)
    await ask(
        cb,
        state,
        f"📸 Кидай фото для «{esc(goal.title)}». Можно сразу несколько.",
        kb([btn("✅ Готово", GoalCb(action="phdone", id=goal.id))]),
    )


@router.message(GoalPhotos.waiting, F.photo)
async def photo_for_goal(message: Message, state: FSMContext, db: Database, app: App, bot: Bot) -> None:
    goal_id = (await state.get_data()).get("goal_id")
    if not goal_id or not message.photo:
        return
    largest = message.photo[-1]
    added = await add_photos(bot, db, app, goal_id, [(largest.file_id, largest.file_unique_id)])
    chat_id = message.chat.id
    app.photos_added[chat_id] = app.photos_added.get(chat_id, 0) + added

    async def acknowledge() -> None:
        n = app.photos_added.pop(chat_id, 0)
        text = f"📸 Добавлено фото: <b>{n}</b>. Кидай ещё или жми «Готово»." if n else "Эти фото уже есть у цели."
        await bot.send_message(chat_id, text, reply_markup=kb([btn("✅ Готово", GoalCb(action="phdone", id=goal_id))]))

    # Альбом приходит пачкой сообщений — отвечаем один раз, когда пачка закончилась.
    app.debouncer.schedule(("goal_photos", chat_id), acknowledge)


@router.message(GoalPhotos.waiting, F.document)
async def photo_as_file(message: Message) -> None:
    await message.answer("📎 Это файл. Отправь картинку как фото (со сжатием), тогда я её прикреплю.")


@router.message(GoalPhotos.waiting)
async def photo_expected(message: Message, state: FSMContext) -> None:
    goal_id = (await state.get_data()).get("goal_id", 0)
    await message.answer(
        "📸 Жду фото. Закончил — жми «Готово».",
        reply_markup=kb([btn("✅ Готово", GoalCb(action="phdone", id=goal_id))]),
    )


@router.callback_query(GoalCb.filter(F.action == "phdone"), StateFilter("*"))
async def cb_photos_done(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    if await state.get_state() == GoalPhotos.waiting.state:
        await finish(cb, state)
    await show_goal(cb, db, callback_data.id, -1)


@router.callback_query(GoalCb.filter(F.action == "phdel"))
async def cb_photo_delete(
    cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer
) -> None:
    try:
        photo_id = int(callback_data.arg)
    except ValueError:
        return
    photo = await db.delete_photo(photo_id)
    if photo is not None and photo.local_path:
        with suppress(OSError):
            Path(photo.local_path).unlink()
    callback_answer.text = "Фото удалено"
    await show_goal(cb, db, callback_data.id)


# Фото, присланное просто так — спрашиваем, к какой цели прикрепить.
@router.message(StateFilter(None), F.photo)
async def loose_photo(message: Message, db: Database, app: App, bot: Bot) -> None:
    if not message.photo:
        return
    chat_id = message.chat.id
    largest = message.photo[-1]
    app.pending_photos.setdefault(chat_id, []).append((largest.file_id, largest.file_unique_id))

    async def ask_goal() -> None:
        photos = app.pending_photos.get(chat_id) or []
        if not photos:
            return
        goals = await db.goals()
        what = "это фото" if len(photos) == 1 else f"эти фото ({len(photos)})"
        rows: list[list[InlineKeyboardButton] | None] = [
            [btn(f"🎯 {truncate(g.title, 30)}", GoalCb(action="attach", id=g.id))] for g in goals[:20]
        ]
        rows.append([btn("➕ Новая цель с этим фото", GoalCb(action="newph"))])
        rows.append([btn("❌ Не надо", GoalCb(action="attachno"))])
        await bot.send_message(chat_id, f"📷 К какой цели прикрепить {what}?", reply_markup=kb(*rows))

    app.debouncer.schedule(("loose_photos", chat_id), ask_goal)


@router.callback_query(GoalCb.filter(F.action == "attach"))
async def cb_attach(
    cb: CallbackQuery, callback_data: GoalCb, db: Database, app: App, bot: Bot, callback_answer: CallbackAnswer
) -> None:
    photos = app.pending_photos.pop(chat_id_of(cb), [])
    if not photos:
        callback_answer.text = "Фото уже нет — пришли ещё раз"
        return
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        callback_answer.text = "Этой цели уже нет"
        return
    added = await add_photos(bot, db, app, goal.id, photos)
    callback_answer.text = f"Прикреплено: {added}" if added else "Эти фото уже есть у цели"
    await show_goal(cb, db, goal.id, -1)


@router.callback_query(GoalCb.filter(F.action == "newph"))
async def cb_new_with_photo(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    photos = app.pending_photos.pop(chat_id_of(cb), [])
    await start_new_goal(cb, state, photos)


@router.callback_query(GoalCb.filter(F.action == "attachno"))
async def cb_attach_no(cb: CallbackQuery, app: App) -> None:
    app.pending_photos.pop(chat_id_of(cb), None)
    await render(cb, "Ок, фото не сохраняю.")


# ─────────────────────────── пополнение ───────────────────────────


SOURCE_NOTES = {
    "wallet": "С расходов в цель",
    "pool": "Из свободных накоплений в цель",
    "outside": "Накоплено до бота",
}


@router.callback_query(GoalCb.filter(F.action == "dep"))
async def cb_deposit(cb: CallbackQuery, callback_data: GoalCb, db: Database, bot: Bot) -> None:
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        await show_goals(cb, db)
        return
    cur = db.settings.currency
    b = await db.balances()
    rows: list[list[InlineKeyboardButton] | None] = []
    if b.wallet > 0:
        rows.append([btn(f"💼 С расходов ({money(b.wallet, cur)})", GoalCb(action="depsrc", id=goal.id, arg="wallet"))])
    if b.pool > 0:
        rows.append([btn(f"🆓 Из свободных ({money(b.pool, cur)})", GoalCb(action="depsrc", id=goal.id, arg="pool"))])
    rows.append([btn("🏦 Деньги, отложенные ещё до бота", GoalCb(action="depsrc", id=goal.id, arg="outside"))])
    rows.append([btn("❌ Отмена", Nav(to="cancel"))])
    await bot.send_message(
        chat_id_of(cb), f"➕ <b>Пополнить «{esc(goal.title)}»</b>\n\nОткуда берём деньги?", reply_markup=kb(*rows)
    )


@router.callback_query(GoalCb.filter(F.action == "depsrc"))
async def cb_deposit_source(
    cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database, callback_answer: CallbackAnswer
) -> None:
    goal = await db.get_goal(callback_data.id)
    src = callback_data.arg
    if goal is None or goal.status != "active" or src not in SOURCE_NOTES:
        await show_goals(cb, db)
        return
    cur = db.settings.currency
    b = await db.balances()
    limit = {"wallet": b.wallet, "pool": b.pool}.get(src)
    if limit is not None and limit <= 0:
        callback_answer.text = "Там пусто"
        callback_answer.show_alert = True
        return
    await state.clear()
    await state.set_state(GoalMoney.deposit)
    await state.update_data(goal_id=goal.id, src=src)
    text = f"➕ Сколько положить в «{esc(goal.title)}»?"
    rows: list[list[InlineKeyboardButton] | None] = []
    if limit is not None:
        text += f"\nДоступно: <b>{money(limit, cur)}</b>"
        rows.append([btn(f"Всё: {money(limit, cur)}", GoalCb(action="depall", id=goal.id, arg=src))])
    else:
        text += "\n\nЭто деньги, которые ты уже отложил на эту цель раньше. Доходом они не считаются."
    rows.append([btn("❌ Отмена", Nav(to="cancel"))])
    await ask(cb, state, text, kb(*rows), replace=True)


@router.message(GoalMoney.deposit, F.text)
async def deposit_amount(message: Message, state: FSMContext, db: Database) -> None:
    amount = parse_amount(message.text or "")
    if not amount:
        await message.answer("🤨 Не понял сумму. Например: <code>5000</code>", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    await do_deposit(message, state, db, data["goal_id"], data["src"], amount)


@router.callback_query(GoalCb.filter(F.action == "depall"), StateFilter("*"))
async def cb_deposit_all(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    b = await db.balances()
    amount = {"wallet": b.wallet, "pool": b.pool}.get(callback_data.arg, 0)
    await do_deposit(cb, state, db, callback_data.id, callback_data.arg, amount)


async def do_deposit(event: Event, state: FSMContext, db: Database, goal_id: int, src: str, amount: int) -> None:
    cur = db.settings.currency
    goal = await db.get_goal(goal_id)
    if goal is None or goal.status != "active" or src not in SOURCE_NOTES:
        await finish(event, state)
        await render(event, "⚠️ Этой цели уже нет.")
        return
    res = await db.transfer(src, "goal", amount, dst_goal=goal_id, note=f"{SOURCE_NOTES[src]} «{goal.title}»")
    if res is None:
        b = await db.balances()
        available = {"wallet": b.wallet, "pool": b.pool}.get(src, 0)
        text = f"❌ Столько нет. Доступно: <b>{money(max(0, available), cur)}</b>. Введи сумму поменьше."
        if isinstance(event, Message):
            await event.answer(text, reply_markup=cancel_kb())
        else:
            await finish(event, state)
            await render(event, text)
        return
    await finish(event, state)
    goal = await db.get_goal(goal_id)
    assert goal is not None
    text = (
        f"✅ <b>+{money(res.amount, cur)}</b> → «{esc(goal.title)}»\n"
        f"<code>{bar(goal.progress)}</code> {pct(goal.progress)} · {money(goal.saved, cur)} из {money(goal.target, cur)}\n\n"
        f"{motivation.quote('deposit', db.settings.harsh, seed=res.tx_id)}"
    )
    markup = kb(
        [btn("↩️ Отменить", TxCb(action="undo", id=res.tx_id or 0)), btn("🎯 К цели", GoalCb(action="open", id=goal_id))]
    )
    await render(event, text, markup)
    if res.completed:
        await celebrate(event, db, res.completed)


# ─────────────────────────── снятие ───────────────────────────


@router.callback_query(GoalCb.filter(F.action == "wd"))
async def cb_withdraw(
    cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database, callback_answer: CallbackAnswer
) -> None:
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        await show_goals(cb, db)
        return
    if goal.saved <= 0:
        callback_answer.text = "В цели пусто"
        return
    cur = db.settings.currency
    await state.clear()
    await state.set_state(GoalMoney.withdraw)
    await state.update_data(goal_id=goal.id)
    await ask(
        cb,
        state,
        f"➖ <b>Снять из «{esc(goal.title)}»</b>\nТам сейчас {money(goal.saved, cur)}.\n\n"
        f"Сколько снять на расходы?\n\n{motivation.quote('withdraw', db.settings.harsh)}",
        kb(
            [btn(f"Всё: {money(goal.saved, cur)}", GoalCb(action="wdall", id=goal.id))],
            [btn("❌ Отмена", Nav(to="cancel"))],
        ),
    )


@router.message(GoalMoney.withdraw, F.text)
async def withdraw_amount(message: Message, state: FSMContext, db: Database) -> None:
    amount = parse_amount(message.text or "")
    if not amount:
        await message.answer("🤨 Не понял сумму. Например: <code>5000</code>", reply_markup=cancel_kb())
        return
    goal_id = (await state.get_data())["goal_id"]
    goal = await db.get_goal(goal_id)
    if goal is not None and amount > goal.saved:
        await message.answer(
            f"❌ В цели только {money(goal.saved, db.settings.currency)}. Введи сумму поменьше.",
            reply_markup=cancel_kb(),
        )
        return
    await finish(message, state)
    await withdraw_confirm(message, db, goal_id, amount)


@router.callback_query(GoalCb.filter(F.action == "wdall"), StateFilter("*"))
async def cb_withdraw_all(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    await finish(cb, state)
    goal = await db.get_goal(callback_data.id)
    await withdraw_confirm(cb, db, callback_data.id, goal.saved if goal else 0)


async def withdraw_confirm(event: Event, db: Database, goal_id: int, amount: int) -> None:
    goal = await db.get_goal(goal_id)
    if goal is None or goal.status != "active" or amount <= 0:
        await render(event, "⚠️ Снимать нечего.")
        return
    cur = db.settings.currency
    text = (
        f"⚠️ Точно снять <b>{money(amount, cur)}</b> из «{esc(goal.title)}» на расходы?\n\n"
        f"{motivation.quote('withdraw', db.settings.harsh)}"
    )
    markup = kb(
        [btn("💪 Нет, оставлю в цели", GoalCb(action="wdno", id=goal_id))],
        [btn("😔 Да, снять", GoalCb(action="wdok", id=goal_id, arg=str(amount)))],
    )
    await render(event, text, markup)


@router.callback_query(GoalCb.filter(F.action == "wdok"))
async def cb_withdraw_ok(
    cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer
) -> None:
    cur = db.settings.currency
    goal = await db.get_goal(callback_data.id)
    try:
        amount = int(callback_data.arg)
    except ValueError:
        return
    if goal is None or goal.status != "active":
        await render(cb, "⚠️ Этой цели уже нет.")
        return
    res = await db.transfer("goal", "wallet", amount, src_goal=goal.id, note=f"Снятие из «{goal.title}»")
    if res is None:
        callback_answer.text = "В цели столько нет"
        callback_answer.show_alert = True
        return
    goal = await db.get_goal(goal.id)
    assert goal is not None
    text = (
        f"➖ Снято <b>{money(amount, cur)}</b> из «{esc(goal.title)}» на расходы.\n"
        f"В цели осталось: {money(goal.saved, cur)}.\n\n"
        "Запомни: эти деньги ты должен вернуть. С процентами — в виде дисциплины."
    )
    markup = kb(
        [btn("↩️ Отменить", TxCb(action="undo", id=res.tx_id or 0)), btn("🎯 К цели", GoalCb(action="open", id=goal.id))]
    )
    await render(cb, text, markup)


@router.callback_query(GoalCb.filter(F.action == "wdno"))
async def cb_withdraw_no(cb: CallbackQuery, callback_data: GoalCb, db: Database) -> None:
    await render(
        cb,
        "💪 <b>Правильно.</b> Деньги остаются в цели.\n\n" + motivation.quote("resisted", db.settings.harsh),
        kb([btn("🎯 К цели", GoalCb(action="open", id=callback_data.id))]),
    )


# ─────────────────────────── свободные накопления ───────────────────────────


@router.callback_query(GoalCb.filter(F.action == "pool"))
async def cb_pool(cb: CallbackQuery, db: Database, callback_answer: CallbackAnswer) -> None:
    cur = db.settings.currency
    b = await db.balances()
    if b.pool <= 0:
        callback_answer.text = "Свободных накоплений нет"
        await show_goals(cb, db)
        return
    goals = [g for g in await db.goals() if not g.reached]
    rows: list[list[InlineKeyboardButton] | None] = []
    if any(g.receives for g in goals):
        rows.append([btn("⚖️ По долям всех целей", GoalCb(action="poolto", id=0))])
    rows += [[btn(f"→ {truncate(g.title, 30)}", GoalCb(action="poolto", id=g.id))] for g in goals[:15]]
    rows.append([btn("« Назад", GoalCb(action="list"))])
    text = f"🆓 <b>Свободные накопления: {money(b.pool, cur)}</b>\n\nКуда направить?"
    if not goals:
        text += "\n\nВсе цели уже заполнены. Создай новую — выше, больше, страшнее."
    await render(cb, text, kb(*rows))


@router.callback_query(GoalCb.filter(F.action == "poolto"))
async def cb_pool_to(cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer) -> None:
    cur = db.settings.currency
    b = await db.balances()
    if callback_data.id:
        res = await db.transfer("pool", "goal", b.pool, dst_goal=callback_data.id, note="Свободные накопления → цель")
    else:
        res = await db.transfer("pool", "goals", b.pool, note="Свободные накопления → цели")
    if res is None:
        callback_answer.text = "Нечего распределять или некуда: все цели заполнены или на паузе"
        callback_answer.show_alert = True
        return
    titles = await goal_titles(db)
    lines = [f"✅ Распределено <b>{money(res.amount, cur)}</b>:", *transfer_lines(res, titles, cur)]
    markup = kb([btn("↩️ Отменить", TxCb(action="undo", id=res.tx_id or 0)), btn("🎯 К целям", GoalCb(action="list"))])
    await render(cb, "\n".join(lines), markup)
    if res.completed:
        await celebrate(cb, db, res.completed)


# ─────────────────────────── правка ───────────────────────────


async def _start_edit(cb: CallbackQuery, state: FSMContext, db: Database, goal_id: int, st: State, text: str) -> None:
    goal = await db.get_goal(goal_id)
    if goal is None or goal.status != "active":
        await show_goals(cb, db)
        return
    await state.clear()
    await state.set_state(st)
    await state.update_data(goal_id=goal_id)
    rows: list[list[InlineKeyboardButton] | None] = []
    if st == GoalEdit.deadline and goal.deadline:
        rows.append([btn("🗑 Убрать срок", GoalCb(action="nodl", id=goal_id))])
    rows.append([btn("❌ Отмена", Nav(to="cancel"))])
    await ask(cb, state, text.format(title=esc(goal.title)), kb(*rows))


@router.callback_query(GoalCb.filter(F.action == "etitle"))
async def cb_edit_title(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    await _start_edit(cb, state, db, callback_data.id, GoalEdit.title, "✏️ Новое название для «{title}»:")


@router.callback_query(GoalCb.filter(F.action == "etarget"))
async def cb_edit_target(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    await _start_edit(
        cb, state, db, callback_data.id, GoalEdit.target, "🎯 Новая сумма цели «{title}»? Например: <code>200к</code>"
    )


@router.callback_query(GoalCb.filter(F.action == "edl"))
async def cb_edit_deadline(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    await _start_edit(
        cb,
        state,
        db,
        callback_data.id,
        GoalEdit.deadline,
        "📆 К какой дате хочешь закрыть «{title}»?\n"
        "Например: <code>31.12.2027</code> или <code>06.2027</code>\n\n"
        "Со сроком я посчитаю, сколько нужно откладывать в месяц.",
    )


@router.callback_query(GoalCb.filter(F.action == "nodl"), StateFilter("*"))
async def cb_no_deadline(cb: CallbackQuery, callback_data: GoalCb, state: FSMContext, db: Database) -> None:
    await finish(cb, state)
    await db.update_goal(callback_data.id, deadline=None)
    await show_goal(cb, db, callback_data.id)


@router.message(GoalEdit.title, F.text)
async def edit_title(message: Message, state: FSMContext, db: Database) -> None:
    title = " ".join((message.text or "").split())
    if not title or len(title) > MAX_TITLE:
        await message.answer(f"Название — от 1 до {MAX_TITLE} символов. Ещё раз:", reply_markup=cancel_kb())
        return
    data = await finish(message, state)
    await db.update_goal(data["goal_id"], title=title)
    await show_goal(message, db, data["goal_id"])


@router.message(GoalEdit.target, F.text)
async def edit_target(message: Message, state: FSMContext, db: Database) -> None:
    target = parse_amount(message.text or "")
    if not target:
        await message.answer("🤨 Не понял сумму. Например: <code>200к</code>", reply_markup=cancel_kb())
        return
    data = await finish(message, state)
    await db.update_goal(data["goal_id"], target=target)
    await show_goal(message, db, data["goal_id"])


@router.message(GoalEdit.deadline, F.text)
async def edit_deadline(message: Message, state: FSMContext, db: Database) -> None:
    deadline: date | None = parse_deadline(message.text or "", clock.today())
    if deadline is None:
        await message.answer(
            "🤨 Нужна дата в будущем: <code>31.12.2027</code> или <code>06.2027</code>", reply_markup=cancel_kb()
        )
        return
    data = await finish(message, state)
    await db.update_goal(data["goal_id"], deadline=deadline)
    await show_goal(message, db, data["goal_id"])


# ─────────────────────────── покупка и удаление ───────────────────────────


@router.callback_query(GoalCb.filter(F.action == "buy"))
async def cb_buy(cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer) -> None:
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        callback_answer.text = "Эта цель уже закрыта"
        return
    if not goal.reached:
        callback_answer.text = "Цель ещё не достигнута. Работай."
        callback_answer.show_alert = True
        return
    cur = db.settings.currency
    text = (
        f"🛒 Закрыть цель «{esc(goal.title)}»?\n\n"
        f"Спишу {money(min(goal.saved, goal.target), cur)} как покупку цели, а сама цель уйдёт в архив 🏆."
    )
    if goal.saved > goal.target:
        text += f"\nЛишние {money(goal.saved - goal.target, cur)} вернутся в свободные накопления."
    await render(
        cb,
        text,
        kb(
            [btn("✅ Да, купил!", GoalCb(action="buyok", id=goal.id))],
            [btn("❌ Ещё нет", GoalCb(action="open", id=goal.id))],
        ),
    )


@router.callback_query(GoalCb.filter(F.action == "buyok"))
async def cb_buy_ok(cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer) -> None:
    goal = await db.get_goal(callback_data.id)
    bought = await db.buy_goal(callback_data.id) if goal else None
    if goal is None or bought is None:
        callback_answer.text = "Эта цель уже закрыта"
        return
    tx_id, spent = bought
    photo = await cover(db, goal)
    text = (
        f"🏆 <b>«{esc(goal.title)}» — КУПЛЕНО!</b>\n\n"
        f"Потрачено из накоплений: {money(spent, db.settings.currency)}.\n"
        "Без долгов. Без кредитов. Без оправданий.\n\n"
        f"{motivation.quote('goal_bought', db.settings.harsh)}"
    )
    markup = kb(
        [btn("🎯 Поставить новую цель", GoalCb(action="new"))],
        [btn("↩️ Отменить", TxCb(action="undo", id=tx_id))],
    )
    await render(cb, text, markup, photo)
    await remember_refreshed(db, photo)


@router.callback_query(GoalCb.filter(F.action == "del"))
async def cb_delete(cb: CallbackQuery, callback_data: GoalCb, db: Database) -> None:
    goal = await db.get_goal(callback_data.id)
    if goal is None or goal.status != "active":
        await show_goals(cb, db)
        return
    cur = db.settings.currency
    back = [btn("❌ Не удалять", GoalCb(action="open", id=goal.id))]
    if goal.saved > 0:
        others = [g for g in await db.goals() if g.id != goal.id]
        rows: list[list[InlineKeyboardButton] | None] = []
        if any(g.receives for g in others):
            rows.append([btn("➡️ В другие цели", GoalCb(action="delok", id=goal.id, arg="goals"))])
        rows.append([btn("🆓 В свободные накопления", GoalCb(action="delok", id=goal.id, arg="pool"))])
        rows.append([btn("💼 На расходы", GoalCb(action="delok", id=goal.id, arg="wallet"))])
        rows.append(back)
        text = f"🗑 Удалить цель «{esc(goal.title)}»?\n\nВ ней {money(goal.saved, cur)}. Куда деть деньги?"
        await render(cb, text, kb(*rows))
        return
    await render(
        cb,
        f"🗑 Удалить цель «{esc(goal.title)}»?",
        kb([btn("🗑 Да, удалить", GoalCb(action="delok", id=goal.id, arg="pool"))], back),
    )


@router.callback_query(GoalCb.filter(F.action == "delok"))
async def cb_delete_ok(cb: CallbackQuery, callback_data: GoalCb, db: Database, callback_answer: CallbackAnswer) -> None:
    dest = callback_data.arg if callback_data.arg in ("goals", "pool", "wallet") else "pool"
    goal = await db.get_goal(callback_data.id)
    res = await db.delete_goal(callback_data.id, dest) if goal else None
    if goal is None or res is None:
        callback_answer.text = "Цель уже удалена"
        await show_goals(cb, db)
        return
    cur = db.settings.currency
    lines = [f"🗑 Цель «{esc(goal.title)}» удалена."]
    if res.amount:
        where = {
            "goals": "распределены по другим целям",
            "pool": "лежат в свободных накоплениях",
            "wallet": "добавлены на расходы",
        }[dest if res.amount > 0 else "wallet"]
        lines.append(f"Деньги ({money(res.amount, cur)}) {where}.")
    lines += ["", motivation.quote("goal_deleted", db.settings.harsh)]
    rows: list[list[InlineKeyboardButton] | None] = []
    if res.tx_id:
        rows.append([btn("↩️ Вернуть цель", TxCb(action="undo", id=res.tx_id))])
    rows.append([btn("🎯 К целям", GoalCb(action="list"))])
    await render(cb, "\n".join(lines), kb(*rows))
    if res.completed:
        await celebrate(cb, db, res.completed)
