"""Версия без сервера в настоящем браузере: Telegram и его облако заменены имитацией.

Необязательный тест (как test_webapp_ui.py): без Playwright или браузера — пропускается.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from .test_webapp_ui import THEME, png

playwright_api = pytest.importorskip("playwright.async_api")

DOCS = Path(__file__).resolve().parent.parent / "docs"

# Мост Telegram с облачным хранилищем (протокол как у настоящего клиента) и его лимитами.
FAKE_TELEGRAM = """
(() => {
  const KEY = '__tg_cloud__';
  const load = () => JSON.parse(localStorage.getItem(KEY) || '{}');
  const save = (d) => localStorage.setItem(KEY, JSON.stringify(d));
  window.__events = [];
  window.TelegramWebviewProxy = {
    postEvent(type, json) {
      window.__events.push(type);
      if (type !== 'web_app_invoke_custom_method') return;
      const req = JSON.parse(json);
      const p = req.params || {};
      const d = load();
      let result;
      let error;
      try {
        if (req.method === 'getStorageKeys') result = Object.keys(d);
        else if (req.method === 'getStorageValues') {
          result = {};
          for (const k of p.keys) result[k] = k in d ? d[k] : '';
        } else if (req.method === 'saveStorageValue') {
          if (!/^[A-Za-z0-9_-]{1,128}$/.test(p.key)) throw 'KEY_INVALID';
          if (typeof p.value !== 'string' || p.value.length > 4096) throw 'VALUE_TOO_LONG';
          if (!(p.key in d) && Object.keys(d).length >= 1024) throw 'KEYS_TOO_MANY';
          d[p.key] = p.value;
          save(d);
          result = true;
        } else if (req.method === 'deleteStorageValues') {
          for (const k of p.keys) delete d[k];
          save(d);
          result = true;
        } else error = 'UNKNOWN_METHOD';
      } catch (e) {
        error = String(e);
      }
      setTimeout(() => window.Telegram.WebView.receiveEvent('custom_method_invoked', { req_id: req.req_id, result, error }), 3);
    },
  };
})();
"""


@pytest_asyncio.fixture
async def pages_site() -> AsyncIterator[str]:
    app = web.Application()

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(DOCS / "index.html")

    app.router.add_get("/", index)
    app.router.add_static("/", DOCS)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


def launch_url(base: str, version: str = "8.0") -> str:
    params = {
        "tgWebAppData": "user=%7B%22id%22%3A1%7D&auth_date=1&hash=x",
        "tgWebAppVersion": version,
        "tgWebAppPlatform": "ios",
        "tgWebAppThemeParams": json.dumps(THEME),
    }
    return f"{base}/#{urlencode(params)}"


@pytest_asyncio.fixture
async def phone() -> AsyncIterator[Any]:
    path = os.getenv("CHROMIUM_PATH") or (
        "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
    )
    async with playwright_api.async_playwright() as p:
        try:
            browser = await (p.chromium.launch(executable_path=path) if path else p.chromium.launch())
        except Exception as e:
            pytest.skip(f"Chromium недоступен: {e}")
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, reduced_motion="reduce"
        )
        await context.add_init_script(FAKE_TELEGRAM)
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.errors = errors  # type: ignore[attr-defined]
        try:
            yield page
        finally:
            await browser.close()


async def text(page: Any, selector: str) -> str:
    return str(await page.locator(selector).first.inner_text()).replace("\xa0", " ")


async def toast(page: Any) -> str:
    await page.wait_for_selector("#toast:not([hidden])")
    value = await text(page, "#toast")
    await page.click("#toast")
    return value


async def test_serverless_app_in_telegram(phone: Any, pages_site: str) -> None:
    p = phone
    shots = Path(os.environ["UI_SHOTS"]) if os.getenv("UI_SHOTS") else None
    await p.goto(launch_url(pages_site))
    await p.wait_for_selector(".hero .big")
    assert "с чего начать" in (await text(p, "#view")).lower()  # первый запуск

    await p.click("text=💼 Мои деньги")
    await p.fill(".sheet .input.amount", "10 000")
    await p.click(".sheet .btn.primary")
    assert "Остаток сохранён" in await toast(p)
    assert "10 000 ₽" in await text(p, ".hero .big")

    await p.click("text=🎯 Поставить цель")
    await p.fill(".sheet input[placeholder^='Машина']", "Часы")
    await p.fill(".sheet input[placeholder='Сколько стоит']", "1000")
    await p.click(".sheet .btn.primary")
    assert "Цель поставлена" in await toast(p)
    await p.set_input_files(
        ".sheet input[type=file]",
        files=[{"name": "w.png", "mimeType": "image/png", "buffer": png(900, 600, (30, 90, 200))}],
    )
    assert "Фото добавлено" in await toast(p)
    await p.wait_for_selector(".sheet .carousel img[src^='blob:']")
    await p.click(".sheet-backdrop", position={"x": 20, "y": 20})

    # Доход 2000: 80% (1600) в цели — «Часы» (1000) собраны, остальное в копилку.
    await p.click("#fab")
    await p.fill(".sheet .input.amount", "2000")
    await p.fill(".sheet input[aria-label='Комментарий']", "зарплата")
    await p.wait_for_selector(".segment button.on.income")
    await p.click(".sheet .btn.income")
    assert "Цель собрана: Часы" in await toast(p)

    await p.click("[data-tab=goals]")
    await p.click(".card:has-text('Часы')")
    await p.click("text=🛒 Купил! Закрыть цель")
    await p.click(".sheet .confirm")
    assert "куплено" in await toast(p)

    # Настройки, «Хочу купить».
    await p.click("[data-tab=home]")
    await p.click("text=⚙️ Настройки")
    await p.click(".sheet .chip:has-text('70%')")
    await p.wait_for_selector(".sheet .chip.on:has-text('70%')")
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
        await p.screenshot(path=str(shots / "serverless-settings.png"))
    await p.click(".sheet-backdrop", position={"x": 20, "y": 20})
    await p.click("text=🛑 Хочу купить")
    await p.fill(".sheet input[placeholder='Что хочешь купить']", "кроссовки")
    await p.fill(".sheet input[placeholder='Сколько стоит']", "5000")
    await p.click(".sheet .btn.primary")
    await p.wait_for_selector(".sheet h3:has-text('СТОП')")
    if shots:
        await p.screenshot(path=str(shots / "serverless-want.png"))
    await p.click("text=💪 Просто не покупаю")
    assert "Не купил" in await toast(p)

    wallet = await text(p, ".hero .big")
    await p.reload()  # данные живут в облаке Telegram, а не на странице
    await p.wait_for_selector(".hero .big")
    assert await text(p, ".hero .big") == wallet
    assert "с чего начать" not in (await text(p, "#view")).lower()
    cloud = await p.evaluate("JSON.parse(localStorage.getItem('__tg_cloud__'))")
    assert any(k.startswith("p") for k in cloud) and "s" in cloud and "t0" in cloud
    assert all(len(v) <= 4096 for v in cloud.values())
    assert "web_app_ready" in await p.evaluate("window.__events")
    assert p.errors == []


async def test_old_telegram_and_plain_browser(phone: Any, pages_site: str) -> None:
    p = phone
    await p.goto(launch_url(pages_site, version="6.2"))
    await p.wait_for_selector(".fatal h2:has-text('Обнови Telegram')")
    await p.goto(pages_site + "/?plain")
    await p.evaluate("sessionStorage.clear()")
    await p.reload()
    await p.wait_for_selector(".fatal h2:has-text('Открой из Telegram')")
