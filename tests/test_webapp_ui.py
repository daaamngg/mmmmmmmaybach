"""Мини-приложение в настоящем браузере (Chromium через Playwright).

Необязательный тест для разработки: если Playwright или браузер не установлены — пропускается.
    pip install playwright && playwright install chromium
Путь к своему Chromium: CHROMIUM_PATH=/path/to/chrome. Скриншоты: UI_SHOTS=папка.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from aiogram import Bot
from aiohttp.test_utils import TestServer

from finbot import ui
from finbot.config import Config
from finbot.context import App
from finbot.db import Database
from finbot.webapp.server import create_web_app

from .conftest import OWNER, STRANGER, TZ, FrozenClock
from .fake_telegram import FakeTelegram
from .test_webapp import TOKEN, sign

playwright_api = pytest.importorskip("playwright.async_api")

THEME = {
    "bg_color": "#212121",
    "secondary_bg_color": "#0f0f0f",
    "text_color": "#ffffff",
    "hint_color": "#aaaaaa",
    "link_color": "#8774e1",
    "button_color": "#8774e1",
    "button_text_color": "#ffffff",
    "section_bg_color": "#212121",
}


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Настоящая картинка без Pillow."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def launch_url(base: str, uid: int = OWNER) -> str:
    params = {
        "tgWebAppData": sign(uid),
        "tgWebAppVersion": "8.0",
        "tgWebAppPlatform": "android",
        "tgWebAppThemeParams": json.dumps(THEME),
    }
    return f"{base}/#{urlencode(params)}"


@pytest_asyncio.fixture
async def browser() -> AsyncIterator[Any]:
    path = os.getenv("CHROMIUM_PATH") or (
        "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
    )
    async with playwright_api.async_playwright() as p:
        try:
            b = await p.chromium.launch(executable_path=path) if path else await p.chromium.launch()
        except Exception as e:  # браузер не установлен
            pytest.skip(f"Chromium недоступен: {e}")
        try:
            yield b
        finally:
            await b.close()


@pytest_asyncio.fixture
async def site(db: Database, frozen: FrozenClock, tmp_path: Path) -> AsyncIterator[tuple[str, App]]:
    config = Config(bot_token=TOKEN, owner_id=OWNER, data_dir=tmp_path, timezone=TZ, webapp_url="https://x.example")
    app = App(config=config, db=db)
    server = TestServer(create_web_app(app, Bot(TOKEN, session=FakeTelegram())), host="127.0.0.1")
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/"), app
    finally:
        await ui.drain()
        await server.close()


class Page:
    def __init__(self, page: Any, shots: Path | None) -> None:
        self.page = page
        self.shots = shots
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        page.on("response", lambda r: r.status >= 400 and self.errors.append(f"{r.status} {r.url}"))

    async def shot(self, name: str) -> None:
        if self.shots:
            await self.page.screenshot(path=str(self.shots / f"{name}.png"), full_page=True)

    async def text(self, selector: str) -> str:
        """Видимый текст; неразрывные пробелы (в суммах) — как обычные."""
        return str(await self.page.locator(selector).first.inner_text()).replace("\xa0", " ")

    async def toast(self) -> str:
        await self.page.wait_for_selector("#toast:not([hidden])")
        text = await self.text("#toast")
        await self.page.click("#toast")
        return text


@pytest_asyncio.fixture
async def page(browser: Any) -> AsyncIterator[Page]:
    context = await browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",  # без анимаций: скриншоты и клики стабильны
    )
    shots = Path(os.environ["UI_SHOTS"]) if os.getenv("UI_SHOTS") else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    p = Page(await context.new_page(), shots)
    try:
        yield p
    finally:
        await context.close()


async def test_full_flow_in_browser(page: Page, site: tuple[str, App]) -> None:
    base, app = site
    db = app.db
    p = page.page
    await p.goto(launch_url(base))
    await p.wait_for_selector(".hero .big")
    assert "0 ₽" in await page.text(".hero .big")
    assert await p.locator("#tabs").is_visible() and await p.locator("#fab").is_visible()
    await page.shot("01-home-empty")

    # Доход: бот сам понимает, что «зарплата» — доход, и показывает деление 80/20.
    await p.click("#fab")
    await p.fill(".sheet .input.amount", "50 000")
    await p.fill(".sheet input[aria-label='Комментарий']", "зарплата")
    await p.wait_for_selector(".segment button.on.income")
    assert "40 000 ₽ в накопления" in await page.text(".sheet .form-hint")
    await page.shot("02-add-income")
    await p.click(".sheet .btn.income")
    assert "+50 000 ₽" in await page.toast()
    assert (await db.balances()).wallet == 1_000_000

    # Цель с фото.
    await p.click("[data-tab=goals]")
    await p.click("text=＋ Новая цель")
    await p.fill(".sheet input[placeholder^='Машина']", "Maybach")
    await p.fill(".sheet input[placeholder='Сколько стоит']", "20кк")
    await p.click(".sheet .btn.primary")
    assert "Цель поставлена" in await page.toast()
    await p.set_input_files(
        ".sheet input[type=file]",
        files=[{"name": "car.png", "mimeType": "image/png", "buffer": png(320, 200, (200, 30, 30))}],
    )
    assert "Фото добавлено" in await page.toast()
    await p.wait_for_selector(".sheet .carousel img[src^='blob:']")
    goal = (await db.goals())[0]
    assert goal.photos == 1
    # Пополнение из копилки (там 40 000 — у цели не было доли при зарплате).
    await p.fill(".sheet input[placeholder='Сколько положить']", "10000")
    await p.click(".sheet .btn.primary")
    assert "+10 000 ₽" in await page.toast()
    await page.shot("03-goal-sheet")
    await p.click(".sheet-backdrop", position={"x": 20, "y": 20})
    await p.wait_for_selector(".thumb img[src^='blob:']")
    await page.shot("04-goals")

    # Расход с категорией-подсказкой и предупреждением о минусе.
    await p.click("[data-tab=home]")
    await p.click(".btn.expense")
    await p.fill(".sheet .input.amount", "350")
    await p.fill(".sheet input[aria-label='Комментарий']", "шаурма")
    await p.wait_for_selector(".sheet .cats .chip.on")
    assert "Кафе" in await page.text(".sheet .cats .chip.on")
    await p.click(".sheet .btn.expense")
    assert "−350 ₽" in await page.toast()
    await page.shot("05-home")

    # Незнакомое слово: выбираем категорию сами — бот запоминает.
    await p.click("#fab")
    await p.fill(".sheet .input.amount", "1200")
    await p.fill(".sheet input[aria-label='Комментарий']", "штуковина")
    await p.click(".sheet .cats .chip:has-text('Жильё')")
    await p.click(".sheet .btn.expense")
    await page.toast()
    assert (await db.recent_txs(1))[0].category == "home"

    # Календарь и день.
    await p.click("[data-tab=calendar]")
    await p.wait_for_selector(".cell.today")
    assert "−1,6к" in await page.text(".cell.today")
    await page.shot("06-calendar")
    await p.click(".cell.today")
    await p.wait_for_selector(".sheet .item")
    assert await p.locator(".sheet .item").count() == 3
    await page.shot("07-day")

    # История: меняем категорию и удаляем запись.
    await p.click(".sheet-backdrop", position={"x": 20, "y": 20})
    await p.click("[data-tab=history]")
    await p.click(".item:has-text('штуковина')")
    await p.click(".sheet .chip:has-text('Продукты')")
    assert "Категория изменена" in await page.toast()
    assert (await db.recent_txs(1))[0].category == "food"
    await p.click(".sheet .btn.danger")
    await p.click(".sheet .btn.danger.confirm")
    assert "Запись удалена" in await page.toast()
    assert await db.count_tx() == 3  # доход, пополнение цели, шаурма
    await page.shot("08-history")

    assert page.errors == []


async def test_opened_outside_telegram_or_by_stranger(page: Page, site: tuple[str, App]) -> None:
    base, _ = site
    p = page.page
    await p.goto(base + "/")
    await p.wait_for_selector(".fatal")
    assert "Открой из Telegram" in await page.text(".fatal h2")
    await page.shot("09-no-telegram")

    await p.goto(launch_url(base, uid=STRANGER))
    await p.reload()
    await p.wait_for_selector(".fatal h2:has-text('Вход закрыт')")
    assert await p.locator("#tabs").is_hidden()
    await page.shot("10-stranger")
