"""Ключи, по которым сайт мини-приложения хранит общие объекты."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from aiogram import Bot

    from ..context import App

APP: web.AppKey[App] = web.AppKey("finbot_app")
BOT: web.AppKey[Bot | None] = web.AppKey("finbot_bot")
NONCE: web.AppKey[str] = web.AppKey("finbot_nonce")
