"""Проверка, что запрос пришёл из Telegram от владельца бота (подпись initData)."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qsl

from aiogram.utils.web_app import check_webapp_signature

MAX_AGE = 24 * 3600  # приложение, открытое больше суток назад, нужно открыть заново


def user_id_from_init_data(
    init_data: str, bot_token: str, *, now: float | None = None, max_age: int = MAX_AGE
) -> int | None:
    """ID пользователя из подписанных Telegram данных. None — подпись неверна или данные устарели.

    Подделать подпись без токена бота невозможно, поэтому чужой человек (или сайт)
    не сможет прочитать или изменить твои данные, даже зная адрес приложения.
    """
    if not init_data or not check_webapp_signature(bot_token, init_data):
        return None
    fields = dict(parse_qsl(init_data))
    try:
        auth_date = int(fields.get("auth_date", "0"))
        uid = int(json.loads(fields.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        return None
    current = time.time() if now is None else now
    if auth_date <= 0 or current - auth_date > max_age:
        return None
    return uid
