"""Как бот выбирает категорию.

1. То, чему ты его научил: стоит один раз поправить категорию у «бенз 95» —
   и «бенз» дальше всегда будет «Транспортом». Мгновенно, бесплатно, без интернета.
2. Встроенный словарь (шаурма, пятёрочка, такси, wb…).
3. Необязательно — нейросеть (см. ai.py): только для того, что не узнали 1 и 2,
   и в фоне, чтобы запись появлялась без задержки.
"""

from __future__ import annotations

import re

from . import categories
from .db import Database

# Слова, которые ничего не говорят о категории: «купил хлеб» — главное слово «хлеб».
STOPWORDS = frozenset(
    "на в во за для и с со по от до из к ко у о об при без под над это мне себе тебе ему ей нам "
    "купил купила купили куплю взял взяла взяли оплата оплатил оплатила оплатили заплатил заплатила "
    "потратил потратила покупка покупки новый новая новое новые еще ещё просто опять снова руб рублей".split()
)
_TOKEN = re.compile(r"[a-zа-я0-9]+")


def tokens(note: str) -> list[str]:
    return _TOKEN.findall(note.lower().replace("ё", "е"))


def main_word(note: str) -> str | None:
    """Первое содержательное слово записи: «кофе с собой» → «кофе»."""
    for token in tokens(note):
        if len(token) >= 3 and not token.isdigit() and token not in STOPWORDS:
            return token
    return None


def keys_for(note: str | None) -> list[str]:
    """Ключи для памяти: фраза целиком, затем главное слово."""
    if not note:
        return []
    words = tokens(note)
    keys = [f"={' '.join(words)}"] if words else []
    word = main_word(note)
    if word:
        keys.append(f"~{word}")
    return keys


def _valid(category: str | None, kind: str) -> bool:
    return category is not None and any(c.key == category for c in categories.for_kind(kind))


async def resolve(db: Database, note: str | None, kind: str) -> str | None:
    """Категория из памяти или словаря. None — если не узнали."""
    if not note:
        return None
    learned = await db.recall(kind, keys_for(note))
    if _valid(learned, kind):
        return learned
    return categories.detect(note, kind)


async def remember(db: Database, note: str | None, kind: str, category: str, source: str = "user") -> None:
    """Запоминает выбор. От нейросети — только фразу целиком, чтобы не обобщать её догадки."""
    keys = keys_for(note)
    if source != "user":
        keys = keys[:1]
    if keys and _valid(category, kind):
        await db.learn(kind, keys, category, source)


async def guess_kind(db: Database, note: str | None) -> str:
    """Запись без знака: доход, если слова «доходные» («зп», «аванс») или ты так уже учил бота."""
    keys = keys_for(note)
    if keys:
        income = await db.recall("income", keys)
        expense = await db.recall("expense", keys)
        if income and not expense:
            return "income"
        if expense and not income:
            return "expense"
    return categories.guess_kind(note)
