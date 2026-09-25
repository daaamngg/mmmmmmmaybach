"""Встроенный сайт мини-приложения: раздаёт страницу и API, проверяет подпись Telegram.

Снаружи он доступен через Caddy (HTTPS), сам слушает только 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot
from aiohttp import web
from aiohttp.abc import AbstractResolver

from ..context import App
from . import api
from .auth import user_id_from_init_data
from .buttons import ensure_menu_button
from .keys import APP, BOT, NONCE

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Страница и скрипты только свои: никаких внешних CDN (в РФ они часто тормозят).
CSP = (
    "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'"
)


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except api.ApiError as e:
        return _json_error(e.status, e.message)


@web.middleware
async def owner_only(request: web.Request, handler: Handler) -> web.StreamResponse:
    """API — только для владельца бота: проверяем подпись Telegram у каждого запроса."""
    if not request.path.startswith("/api/"):
        return await handler(request)
    app = request.app[APP]
    header = request.headers.get("Authorization", "")
    init_data = header[4:] if header.startswith("tma ") else ""
    user_id = user_id_from_init_data(init_data, app.config.bot_token)
    if user_id is None:
        return _json_error(401, "Открой приложение заново из бота — сессия устарела.")
    if app.owner_id is None or user_id != app.owner_id:
        return _json_error(403, "Это личное приложение владельца бота.")
    return await handler(request)


@web.middleware
async def security_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/static/") and request.query.get("v"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _static_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.glob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def create_web_app(app: App, bot: Bot | None) -> web.Application:
    # Большие запросы — только фото целей; JSON-запросы ограничены в api._body.
    web_app = web.Application(middlewares=[security_headers, errors, owner_only], client_max_size=api.MAX_PHOTO + 4096)
    web_app[APP] = app
    web_app[BOT] = bot
    web_app[NONCE] = secrets.token_hex(8)
    version = _static_version()
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{v}}", version)

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=index_html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "id": request.app[NONCE]})

    web_app.router.add_get("/", index)
    web_app.router.add_get("/healthz", healthz)
    web_app.router.add_static("/static/", STATIC_DIR)
    web_app.add_routes(api.routes)
    return web_app


class _ThisServer(AbstractResolver):
    """Адрес домена — этот же сервер: проверяем HTTPS, не выходя в интернет.

    Некоторые хостинги не пускают сервер к собственному внешнему IP; сертификат при этом
    проверяется по-настоящему (по имени домена).
    """

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[Any]:
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


class WebServer:
    def __init__(self, app: App, bot: Bot) -> None:
        self.app = app
        self.bot = bot
        self.web_app = create_web_app(app, bot)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        config = self.app.config
        self._runner = web.AppRunner(self.web_app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, config.webapp_host, config.webapp_port).start()
        log.info("Сайт мини-приложения слушает %s:%s", config.webapp_host, config.webapp_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _reachable(self, url: str, *, local: bool = False) -> bool:
        """Открывается ли приложение по HTTPS с настоящим сертификатом и это именно наш бот."""
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            connector = aiohttp.TCPConnector(resolver=_ThisServer()) if local else None
            async with (
                aiohttp.ClientSession(timeout=timeout, connector=connector) as session,
                session.get(f"{url}/healthz", allow_redirects=False) as resp,
            ):
                data = json.loads(await resp.text())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
            return False
        return isinstance(data, dict) and data.get("id") == self.web_app[NONCE]

    async def check(self, url: str) -> bool:
        return await self._reachable(url) or await self._reachable(url, local=True)

    async def watch(self) -> None:
        """Ждёт, пока заработает HTTPS (Caddy получает сертификат), и включает кнопку в чате.

        Адресов может быть несколько (запасной домен на случай, если для основного
        центр сертификации временно не выдаёт сертификаты) — берём первый рабочий.
        """
        urls = self.app.config.webapp_urls
        if not urls:
            return
        delay = 5.0
        warned = False
        while True:
            for url in urls:
                if await self.check(url):
                    self.app.webapp_url = url
                    log.info("Мини-приложение доступно: %s", url)
                    await ensure_menu_button(self.bot, self.app)
                    return
            if not warned:
                warned = True
                log.warning(
                    "Мини-приложение пока недоступно (%s) — жду HTTPS-сертификат "
                    "(на сервере должны быть открыты порты 80 и 443). Проверка: finbot doctor",
                    ", ".join(urls),
                )
            await asyncio.sleep(delay)
            delay = min(120.0, delay * 1.3)
