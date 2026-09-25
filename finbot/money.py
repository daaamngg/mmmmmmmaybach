"""Деньги: хранение в копейках, разбор и форматирование сумм, распределение.

Все суммы внутри бота — целые числа в копейках (центах). Так нет ошибок
округления float, а распределение по целям всегда сходится до копейки.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NBSP = " "
MAX_AMOUNT = 10**13  # 100 млрд — защита от случайных опечаток

# Число: «2000», «2 000», «2.000», «1,5», «15 000,50».
# Разделитель + ровно 3 цифры = разряды, 1–2 цифры = копейки.
_SEP = "[   .,']"
NUM = rf"(?:\d{{1,3}}(?:{_SEP}\d{{3}})+(?!\d)|\d+)(?:[.,]\d{{1,2}}(?!\d))?"
# Множители: 2к, 2k, 2 тыс, 1.5кк, 2 млн, 3 ляма.
SUF = r"(?:кк|kk|млн\.?|миллион(?:а|ов)?|лям(?:а|ов)?|тыс(?:\.|яч[аи]?)?|тыщ[аи]?|к|k|т)"
# Валюта после числа просто игнорируется: 500р, 500 руб, 5$, 100 грн.
CUR = (
    r"(?:₽|руб(?:\.|лей|ля|ль)?|р\.?|\$|usd|долл(?:\.|ар(?:ов|а)?)?|€|eur|евро"
    r"|₴|грн\.?|₸|тг|тенге|сом|br|byn)"
)

_AMOUNT_RE = re.compile(rf"^\s*\+?\s*(?P<num>{NUM})\s?(?P<suf>{SUF})?\s?(?:{CUR})?\s*$", re.I)
_NUM_PARTS_RE = re.compile(r"(\d+(?:[.,]\d{3})*)(?:[.,](\d{1,2}))?")


def multiplier(suffix: str | None) -> int:
    if not suffix:
        return 1
    s = suffix.lower().rstrip(".")
    if s in ("кк", "kk", "млн") or s.startswith(("миллион", "лям")):
        return 1_000_000
    return 1_000


def to_minor(num: str, suffix: str | None = None) -> int | None:
    """Строка числа (+ множитель) → копейки. None, если не получилось."""
    s = re.sub("[   ']", "", num)
    m = _NUM_PARTS_RE.fullmatch(s)
    if not m:
        return None
    whole = re.sub("[.,]", "", m.group(1))
    frac = m.group(2) or "0"
    try:
        value = Decimal(f"{whole}.{frac}") * multiplier(suffix) * 100
    except InvalidOperation:
        return None
    minor = int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if minor <= 0 or minor > MAX_AMOUNT:
        return None
    return minor


def parse_amount(text: str) -> int | None:
    """Разбирает сумму, введённую отдельным сообщением: «2000», «150к», «1,5 млн»."""
    m = _AMOUNT_RE.match(text or "")
    if not m:
        return None
    return to_minor(m.group("num"), m.group("suf"))


def fmt_amount(value: int) -> str:
    """12345600 → «123 456», 1050 → «10,50». Минус — настоящий «−»."""
    major, minor = divmod(abs(value), 100)
    s = f"{major:,}".replace(",", NBSP)
    if minor:
        s += f",{minor:02d}"
    return ("−" if value < 0 else "") + s


def money(value: int, cur: str, signed: bool = False) -> str:
    s = fmt_amount(value)
    if signed and value > 0:
        s = "+" + s
    return f"{s}{NBSP}{cur}"


def short_money(value: int) -> str:
    """Компактная запись для тесных мест: 950, 12,5к, 1,2 млн."""
    major = abs(value) // 100
    sign = "−" if value < 0 else ""
    if major < 10_000:
        return sign + f"{major:,}".replace(",", NBSP)
    if major < 1_000_000:
        v = major / 1000
        s = f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}"
        return f"{sign}{s.replace('.', ',')}к"
    v = major / 1_000_000
    s = f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}"
    return f"{sign}{s.replace('.', ',')}{NBSP}млн"


def pct_part(total: int, pct: int) -> int:
    """Сколько из суммы уходит в цели при заданном проценте.

    Если сумма в целых рублях — доля тоже округляется до целых рублей.
    """
    if pct <= 0 or total <= 0:
        return 0
    if pct >= 100:
        return total
    q = 100 if total % 100 == 0 else 1
    return (total * pct + 50 * q) // (100 * q) * q


def split_weighted(total: int, weights: list[int]) -> list[int]:
    """Делит сумму пропорционально весам без потери копеек (метод наибольших остатков).

    По возможности делит целыми рублями, копеечный хвост отдаёт самой «тяжёлой» доле.
    """
    n = len(weights)
    if n == 0:
        return []
    wsum = sum(weights)
    if wsum <= 0 or any(w < 0 for w in weights):
        raise ValueError("weights must be positive")
    q = 100 if total >= 100 else 1
    units, rest = divmod(total, q)
    base = [units * w // wsum for w in weights]
    rems = [units * w % wsum for w in weights]
    left = units - sum(base)
    order = sorted(range(n), key=lambda i: (-rems[i], -weights[i], i))
    for i in order[:left]:
        base[i] += 1
    result = [b * q for b in base]
    if rest:
        top = min(range(n), key=lambda i: (-weights[i], i))
        result[top] += rest
    return result


def allocate_capped(total: int, items: list[tuple[int, int, int]]) -> tuple[dict[int, int], int]:
    """Распределяет сумму по целям пропорционально весам, не переполняя цели.

    items — список (ключ, вес, сколько ещё влезает). Излишек заполненной цели
    перетекает в остальные. Возвращает (распределение, нераспределённый остаток).
    """
    alloc = {key: 0 for key, _, _ in items}
    active = [(k, w, room) for k, w, room in items if w > 0 and room > 0]
    remaining = total
    while remaining > 0 and active:
        shares = split_weighted(remaining, [w for _, w, _ in active])
        overflow = 0
        still_open = []
        for (key, weight, room), share in zip(active, shares, strict=True):
            give = min(share, room - alloc[key])
            alloc[key] += give
            overflow += share - give
            if alloc[key] < room:
                still_open.append((key, weight, room))
        if overflow == remaining:  # никто ничего не взял — выходим
            break
        remaining = overflow
        active = still_open
    return alloc, remaining
