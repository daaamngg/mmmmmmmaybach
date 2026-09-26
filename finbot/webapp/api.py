"""API мини-приложения: те же данные и действия, что и в чате с ботом."""

from __future__ import annotations

import io
import logging
from datetime import date
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiohttp import web

from .. import categories, classify, clock, motivation
from ..context import App
from ..db import Database, Goal, Tx, TxBlocked
from ..fmt import WEEKDAYS, esc, fmt_day, month_bounds, month_title, shift_month
from ..handlers import goals as goals_ui
from ..money import parse_amount
from ..reports import (
    days_left_in_month,
    eta_days,
    goal_shares,
    impulse_streak,
    impulse_total,
    main_goal,
    safe_per_day,
    verdict,
)
from ..ui import spawn
from .keys import APP, BOT

log = logging.getLogger(__name__)
routes = web.RouteTableDef()

QUOTE_CONTEXTS = {"general", "speech", "morning", "want", "resisted", "income", "expense_impulse", "overspent"}
MAX_NOTE = 100
MAX_TITLE = 60
MAX_JSON = 64 * 1024
MAX_PHOTO = 10 * 1024 * 1024  # столько Telegram принимает в sendPhoto
# Картинку узнаём по первым байтам, а не по заголовку запроса.
IMAGE_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png"}


class ApiError(Exception):
    """Ошибка с понятным текстом — приложение показывает его как есть."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _app(request: web.Request) -> App:
    return request.app[APP]


def _db(request: web.Request) -> Database:
    return request.app[APP].db


async def _body(request: web.Request) -> dict[str, Any]:
    if (request.content_length or 0) > MAX_JSON:
        raise ApiError(413, "Слишком большой запрос")
    try:
        data = await request.json()
    except ValueError as e:
        raise ApiError(400, "Некорректный запрос") from e
    if not isinstance(data, dict):
        raise ApiError(400, "Некорректный запрос")
    return data


def _int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ApiError(400, f"Некорректный параметр: {what}") from e


# ─────────────────────────── сериализация ───────────────────────────


def category_json(c: categories.Category) -> dict[str, Any]:
    return {"key": c.key, "emoji": c.emoji, "name": c.name, "impulse": c.tier == "impulse"}


def tx_json(tx: Tx) -> dict[str, Any]:
    regular = tx.kind in ("income", "expense")
    cat = categories.get(tx.category)
    local = clock.from_ts(tx.created_at)
    if regular:
        emoji, label = cat.emoji, cat.name
    elif tx.kind == "transfer":
        emoji, label = "🔁", "Перевод"
    else:
        emoji, label = "⚙️", "Корректировка"
    return {
        "id": tx.id,
        "kind": tx.kind,
        "amount": tx.amount,
        "category": tx.category if regular else None,
        "emoji": emoji,
        "label": label,
        "note": tx.note or "",
        "day": tx.day.isoformat(),
        "time": local.strftime("%H:%M") if local.date() == tx.day else None,
        "saved": (sum(tx.to_goals.values()) + tx.to_pool) if tx.kind == "income" else 0,
        "wallet": tx.to_wallet,
        "impulse": tx.kind == "expense" and cat.tier == "impulse",
    }


def goal_json(goal: Goal, share: float, savings_per_day: float, photo_ids: list[int]) -> dict[str, Any]:
    eta = eta_days(goal, share, savings_per_day)
    return {
        "id": goal.id,
        "title": goal.title,
        "target": goal.target,
        "saved": goal.saved,
        "remaining": goal.remaining,
        "progress": round(goal.progress, 5),
        "weight": goal.weight,
        "share": round(share, 4),
        "reached": goal.reached,
        "eta_days": round(eta, 1) if eta else None,
        "deadline": goal.deadline.isoformat() if goal.deadline else None,
        "photos": photo_ids,
    }


async def goals_payload(db: Database) -> tuple[list[dict[str, Any]], int | None]:
    goals = await db.goals()
    shares = goal_shares(goals)
    _, savings_per_day = await db.daily_rates()
    items = []
    for g in goals:
        photo_ids = [p.id for p in await db.photos(g.id)] if g.photos else []
        items.append(goal_json(g, shares.get(g.id, 0.0), savings_per_day, photo_ids))
    main = main_goal(goals)
    return items, main.id if main else None


# ─────────────────────────── чтение ───────────────────────────


@routes.get("/api/state")
async def state(request: web.Request) -> web.Response:
    app = _app(request)
    db = app.db
    s = db.settings
    today = clock.today()
    b = await db.balances()
    summ = await db.summary(*month_bounds(today.year, today.month))
    goals, main_id = await goals_payload(db)
    return web.json_response(
        {
            "currency": s.currency,
            "goal_pct": s.goal_pct,
            "today": today.isoformat(),
            "month": {
                "title": month_title(today.year, today.month),
                "income": summ.income,
                "expense": summ.expense,
                "saved": summ.saved,
                "impulse": impulse_total(summ),
            },
            "balances": {"wallet": b.wallet, "pool": b.pool, "goals": b.in_goals, "total": b.total},
            "safe_per_day": safe_per_day(b.wallet, today),
            "days_left": days_left_in_month(today),
            "streak": await impulse_streak(db),
            "goals": goals,
            "main_goal": main_id,
            "quote": motivation.pick("overspent" if b.wallet < 0 else "general", s.harsh),
            "ai": app.ai.status if app.ai else None,
        }
    )


@routes.get("/api/categories")
async def category_list(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "expense": [category_json(c) for c in categories.EXPENSE],
            "income": [category_json(c) for c in categories.INCOME],
        }
    )


@routes.get("/api/classify")
async def classify_preview(request: web.Request) -> web.Response:
    """Подсказка категории, пока ты печатаешь комментарий."""
    db = _db(request)
    note = request.query.get("note", "")[:MAX_NOTE]
    kind = request.query.get("kind", "expense")
    if kind not in ("income", "expense"):
        kind = "expense"
    return web.json_response(
        {"category": await classify.resolve(db, note, kind), "kind": await classify.guess_kind(db, note)}
    )


@routes.get("/api/month")
async def month(request: web.Request) -> web.Response:
    db = _db(request)
    today = clock.today()
    y = _int(request.query.get("y", today.year), "y")
    m = _int(request.query.get("m", today.month), "m")
    if not (1 <= m <= 12 and 2000 <= y <= 2200):
        raise ApiError(400, "Некорректный месяц")
    first, last = month_bounds(y, m)
    days = await db.day_totals(first, last)
    other = await db.other_days(first, last)
    summ = await db.summary(first, last)
    impulse = impulse_total(summ)
    no_spend = None
    first_use = await db.first_day()
    if first_use is not None and first <= today:
        start, end = max(first, first_use), min(last, today)
        total = (end - start).days + 1
        if total > 0:
            no_spend = {"days": max(0, total - summ.spend_days), "of": total}
    cats = []
    for key, total_sum, count in summ.by_category:
        c = categories.get(key)
        cats.append(
            {
                **category_json(c),
                "total": total_sum,
                "count": count,
                "share": round(total_sum / summ.expense, 4) if summ.expense else 0,
            }
        )
    py, pm = shift_month(y, m, -1)
    ny, nm = shift_month(y, m, 1)
    return web.json_response(
        {
            "y": y,
            "m": m,
            "title": month_title(y, m),
            "first_weekday": first.weekday(),
            "days_in_month": last.day,
            "today": today.isoformat(),
            # [доход, расход, сколько других изменений: пополнения целей, снятия, остаток]
            "days": {d.isoformat(): [*days.get(d, (0, 0)), other.get(d, 0)] for d in sorted(set(days) | set(other))},
            "summary": {
                "income": summ.income,
                "expense": summ.expense,
                "saved": summ.saved,
                "impulse": impulse,
                "goal_purchases": summ.goal_purchases,
            },
            "no_spend": no_spend,
            "categories": cats,
            "verdict": verdict(summ, impulse),
            "prev": [py, pm],
            "next": [ny, nm] if (y, m) < (today.year, today.month) else None,
        }
    )


@routes.get("/api/day")
async def day(request: web.Request) -> web.Response:
    db = _db(request)
    try:
        d = date.fromisoformat(request.query.get("d", ""))
    except ValueError as e:
        raise ApiError(400, "Некорректная дата") from e
    today = clock.today()
    txs = await db.day_txs(d, all_kinds=True)
    return web.json_response(
        {
            "date": d.isoformat(),
            "title": f"{WEEKDAYS[d.weekday()]}, {fmt_day(d, today)}",
            "future": d > today,
            "txs": [tx_json(t) for t in txs],
        }
    )


@routes.get("/api/history")
async def history(request: web.Request) -> web.Response:
    limit = max(1, min(200, _int(request.query.get("limit", 60), "limit")))
    txs = await _db(request).recent_txs(limit)
    return web.json_response({"txs": [tx_json(t) for t in txs]})


@routes.get("/api/quote")
async def quote(request: web.Request) -> web.Response:
    ctx = request.query.get("ctx", "general")
    ctx = ctx if ctx in QUOTE_CONTEXTS else "general"
    return web.json_response({"quote": motivation.pick(ctx, _db(request).settings.harsh)})


@routes.get("/api/photo/{photo_id}")
async def photo(request: web.Request) -> web.StreamResponse:
    """Фото цели: с диска, а если копии ещё нет — из Telegram (и сохраняем копию)."""
    app = _app(request)
    db = app.db
    photo_row = await db.get_photo(_int(request.match_info["photo_id"], "photo_id"))
    if photo_row is None:
        raise ApiError(404, "Фото не найдено")
    headers = {"Cache-Control": "private, max-age=86400"}
    local = goals_ui.local_file(db, photo_row.local_path)
    if local is not None and local.is_file():
        return web.FileResponse(local, headers=headers)
    bot: Bot | None = request.app[BOT]
    if bot is None:
        raise ApiError(404, "Фото недоступно")
    buf = io.BytesIO()
    try:
        await bot.download(photo_row.file_id, destination=buf)
    except Exception as e:  # сеть, устаревший file_id — показываем заглушку на фронте
        log.warning("Не удалось скачать фото %s: %s", photo_row.id, e)
        raise ApiError(404, "Фото недоступно") from e
    data = buf.getvalue()
    dest = app.config.photos_dir / f"goal_{photo_row.goal_id}" / f"{photo_row.id}.jpg"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        await db.update_photo(photo_row.id, local_path=dest.relative_to(goals_ui.data_dir(db)).as_posix())
    except (OSError, ValueError) as e:
        log.warning("Не удалось сохранить копию фото %s: %s", photo_row.id, e)
    return web.Response(body=data, content_type="image/jpeg", headers=headers)


# ─────────────────────────── изменения ───────────────────────────


def _quote_for(tx: Tx, wallet: int, harsh: int) -> str:
    if tx.kind == "income":
        return motivation.pick("income", harsh, seed=tx.id)
    if wallet < 0:
        return motivation.pick("overspent", harsh, seed=tx.id)
    tier = categories.get(tx.category).tier
    ctx = {"impulse": "expense_impulse", "growth": "expense_growth"}.get(tier, "expense_normal")
    return motivation.pick(ctx, harsh, seed=tx.id)


async def _celebrate(request: web.Request, goal_ids: tuple[int, ...] | list[int]) -> list[str]:
    """Достигнутые цели: праздник и в чате (с фото), и названия — для приложения."""
    app = _app(request)
    titles = []
    for gid in goal_ids:
        g = await app.db.get_goal(gid)
        if g is not None:
            titles.append(g.title)
    bot: Bot | None = request.app[BOT]
    if goal_ids and bot is not None and app.owner_id:
        spawn(goals_ui.celebrate_to(bot, app.owner_id, app.db, list(goal_ids)))
    return titles


@routes.post("/api/tx")
async def add_tx(request: web.Request) -> web.Response:
    app = _app(request)
    db = app.db
    body = await _body(request)
    kind = body.get("kind")
    if kind not in ("income", "expense"):
        raise ApiError(400, "Выбери: расход или доход")
    amount = parse_amount(str(body.get("amount", "")))
    if amount is None:
        raise ApiError(400, "Не понял сумму. Например: 350, 1.5к, 2 000")
    note = " ".join(str(body.get("note") or "").split())[:MAX_NOTE]
    today = clock.today()
    try:
        day_value = date.fromisoformat(body["day"]) if body.get("day") else today
    except (TypeError, ValueError) as e:
        raise ApiError(400, "Некорректная дата") from e
    if day_value > today:
        raise ApiError(400, "Будущее ещё не наступило. Записывай то, что уже случилось.")
    chosen = body.get("category")
    valid = {c.key for c in categories.for_kind(kind)}
    if chosen is not None and chosen not in valid:
        raise ApiError(400, "Неизвестная категория")
    category = chosen or await classify.resolve(db, note, kind)
    if chosen and note:
        await classify.remember(db, note, kind, chosen)  # выбрал сам — запоминаем
    created_at = int(clock.now().timestamp())
    completed: tuple[int, ...] = ()
    if kind == "income":
        res = await db.add_income(
            amount,
            category=category or categories.DEFAULT_INCOME,
            note=note or None,
            day=day_value,
            created_at=created_at,
        )
        tx_id, completed = res.tx_id, res.completed
    else:
        tx_id = await db.add_expense(
            amount,
            category=category or categories.DEFAULT_EXPENSE,
            note=note or None,
            day=day_value,
            created_at=created_at,
        )
    ai_pending = category is None and bool(note) and app.ai is not None
    if ai_pending and app.ai is not None:
        spawn(classify.refine(db, app.ai, tx_id, kind, note))
    tx = await db.get_tx(tx_id)
    assert tx is not None
    b = await db.balances()
    return web.json_response(
        {
            "tx": tx_json(tx),
            "quote": _quote_for(tx, b.wallet, db.settings.harsh),
            "wallet": b.wallet,
            "completed": await _celebrate(request, completed),
            "ai_pending": ai_pending,
        }
    )


@routes.delete("/api/tx/{tx_id}")
async def delete_tx(request: web.Request) -> web.Response:
    try:
        tx = await _db(request).delete_tx(_int(request.match_info["tx_id"], "tx_id"))
    except TxBlocked as e:
        raise ApiError(409, e.reason) from e
    if tx is None:
        raise ApiError(404, "Запись уже удалена")
    return web.json_response({"ok": True})


@routes.patch("/api/tx/{tx_id}")
async def patch_tx(request: web.Request) -> web.Response:
    db = _db(request)
    body = await _body(request)
    tx = await db.get_tx(_int(request.match_info["tx_id"], "tx_id"))
    if tx is None:
        raise ApiError(404, "Запись не найдена")
    category = body.get("category")
    if tx.kind not in ("income", "expense") or category not in {c.key for c in categories.for_kind(tx.kind)}:
        raise ApiError(400, "Эту категорию здесь выбрать нельзя")
    if not await db.set_category(tx.id, category):
        raise ApiError(409, "Категорию этой записи менять нельзя")
    await classify.remember(db, tx.note, tx.kind, category)
    updated = await db.get_tx(tx.id)
    assert updated is not None
    return web.json_response({"tx": tx_json(updated)})


@routes.post("/api/goals")
async def create_goal(request: web.Request) -> web.Response:
    db = _db(request)
    body = await _body(request)
    title = " ".join(str(body.get("title") or "").split())
    if not title or len(title) > MAX_TITLE:
        raise ApiError(400, f"Название цели — от 1 до {MAX_TITLE} символов")
    target = parse_amount(str(body.get("target", "")))
    if target is None:
        raise ApiError(400, "Не понял сумму цели. Например: 150к или 2кк")
    goal_id = await db.create_goal(title, target)
    goals, _ = await goals_payload(db)
    return web.json_response({"goal": next(g for g in goals if g["id"] == goal_id)})


@routes.post("/api/goals/{goal_id}/deposit")
async def deposit(request: web.Request) -> web.Response:
    db = _db(request)
    body = await _body(request)
    goal = await db.get_goal(_int(request.match_info["goal_id"], "goal_id"))
    if goal is None or goal.status != "active":
        raise ApiError(404, "Этой цели уже нет")
    src = body.get("src")
    if src not in ("wallet", "pool", "outside"):
        raise ApiError(400, "Выбери, откуда взять деньги")
    amount = parse_amount(str(body.get("amount", "")))
    if amount is None:
        raise ApiError(400, "Не понял сумму")
    note = {"wallet": "С расходов в цель", "pool": "Из свободных накоплений в цель", "outside": "Накоплено до бота"}
    res = await db.transfer(src, "goal", amount, dst_goal=goal.id, note=f"{note[src]} «{goal.title}»")
    if res is None:
        raise ApiError(409, "Столько денег там нет")
    goals, _ = await goals_payload(db)
    return web.json_response(
        {
            "goal": next(g for g in goals if g["id"] == goal.id),
            "tx_id": res.tx_id,
            "amount": res.amount,
            "quote": motivation.pick("deposit", db.settings.harsh),
            "completed": await _celebrate(request, res.completed),
        }
    )


def image_ext(data: bytes) -> str | None:
    for magic, ext in IMAGE_TYPES.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@routes.post("/api/goals/{goal_id}/photos")
async def upload_photo(request: web.Request) -> web.Response:
    """Фото цели прямо с телефона. Отправляем его и в чат: так у фото есть file_id Telegram."""
    app = _app(request)
    db = app.db
    goal = await db.get_goal(_int(request.match_info["goal_id"], "goal_id"))
    if goal is None or goal.status != "active":
        raise ApiError(404, "Этой цели уже нет")
    if (request.content_length or 0) > MAX_PHOTO:
        raise ApiError(413, "Фото больше 10 МБ — выбери поменьше")
    try:
        data = await request.read()
    except web.HTTPRequestEntityTooLarge as e:
        raise ApiError(413, "Фото больше 10 МБ — выбери поменьше") from e
    ext = image_ext(data)
    if not data or ext is None:
        raise ApiError(415, "Это не похоже на фото. Подойдут JPG, PNG или WEBP.")
    bot: Bot | None = request.app[BOT]
    if bot is None or not app.owner_id:
        raise ApiError(503, "Бот ещё не готов принять фото — попробуй через минуту")
    try:
        sent = await bot.send_photo(
            app.owner_id,
            BufferedInputFile(data, filename=f"goal.{ext}"),
            caption=f"📷 Новое фото цели «{esc(goal.title)}» — добавлено из приложения.",
            disable_notification=True,
        )
    except TelegramAPIError as e:
        log.warning("Не удалось отправить фото цели в Telegram: %s", e)
        raise ApiError(502, "Telegram не принял фото. Попробуй другое или повтори позже.") from e
    if not sent.photo:
        raise ApiError(502, "Telegram не принял фото. Попробуй другое.")
    largest = sent.photo[-1]
    photo_id = await db.add_photo(goal.id, largest.file_id, largest.file_unique_id)
    if photo_id is not None:
        dest = app.config.photos_dir / f"goal_{goal.id}" / f"{photo_id}.{ext}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            await db.update_photo(photo_id, local_path=dest.relative_to(goals_ui.data_dir(db)).as_posix())
        except (OSError, ValueError) as e:
            log.warning("Не удалось сохранить копию фото %s: %s", photo_id, e)
    goals, _ = await goals_payload(db)
    return web.json_response(
        {"goal": next((g for g in goals if g["id"] == goal.id), None), "added": photo_id is not None}
    )


@routes.delete("/api/photo/{photo_id}")
async def delete_photo(request: web.Request) -> web.Response:
    db = _db(request)
    photo_row = await db.delete_photo(_int(request.match_info["photo_id"], "photo_id"))
    if photo_row is None:
        raise ApiError(404, "Фото уже удалено")
    local = goals_ui.local_file(db, photo_row.local_path)
    if local is not None:
        try:
            local.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Не удалось удалить файл фото %s: %s", photo_row.id, e)
    goals, _ = await goals_payload(db)
    return web.json_response({"goal": next((g for g in goals if g["id"] == photo_row.goal_id), None)})
