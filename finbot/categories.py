"""Категории доходов и расходов + автоопределение по словам из комментария."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    emoji: str
    name: str
    kind: str  # income | expense
    # impulse — трата «на хотелки» (бот ругается жёстче всего),
    # essential — необходимое, growth — вложение в себя, neutral — остальное.
    tier: str = "neutral"

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


EXPENSE: list[Category] = [
    Category("food", "🛒", "Продукты", "expense", "essential"),
    Category("cafe", "🍔", "Кафе и доставка", "expense", "impulse"),
    Category("transport", "🚕", "Транспорт", "expense", "essential"),
    Category("home", "🏠", "Жильё и счета", "expense", "essential"),
    Category("subs", "📱", "Связь и подписки", "expense"),
    Category("health", "💊", "Здоровье", "expense", "essential"),
    Category("clothes", "👕", "Одежда", "expense"),
    Category("fun", "🎮", "Развлечения", "expense", "impulse"),
    Category("shopping", "🛍", "Шопинг", "expense", "impulse"),
    Category("vice", "🚬", "Вредные привычки", "expense", "impulse"),
    Category("growth", "💪", "Спорт и обучение", "expense", "growth"),
    Category("gifts", "🎁", "Подарки", "expense"),
    Category("debt", "💳", "Кредиты, долги, штрафы", "expense", "essential"),
    Category("other", "📦", "Прочее", "expense"),
]

INCOME: list[Category] = [
    Category("salary", "💼", "Зарплата", "income"),
    Category("side", "💻", "Подработка", "income"),
    Category("business", "🤝", "Бизнес и продажи", "income"),
    Category("gift_in", "🎁", "Подарок", "income"),
    Category("invest", "📈", "Инвестиции и проценты", "income"),
    Category("other_in", "💰", "Другое", "income"),
]

# Служебная категория: покупка накопленной цели (не выбирается вручную).
GOAL_PURCHASE = Category("goal", "🏆", "Покупка цели", "expense")

DEFAULT_EXPENSE = "other"
DEFAULT_INCOME = "other_in"

_ALL = {c.key: c for c in [*EXPENSE, *INCOME, GOAL_PURCHASE]}
IMPULSE_KEYS: tuple[str, ...] = tuple(c.key for c in EXPENSE if c.tier == "impulse")


def get(key: str | None) -> Category:
    if key and key in _ALL:
        return _ALL[key]
    return _ALL[DEFAULT_EXPENSE]


def for_kind(kind: str) -> list[Category]:
    return INCOME if kind == "income" else EXPENSE


def default_for(kind: str) -> str:
    return DEFAULT_INCOME if kind == "income" else DEFAULT_EXPENSE


# Ключевые слова. Слово длиной < 4 символов должно совпасть целиком,
# длинное — работает как начало слова («шаурм» ловит «шаурма», «шаурмы»…).
# Фразы с пробелами ищутся как подстрока.
_EXPENSE_WORDS: dict[str, str] = {
    "food": (
        "продукт магазин пятерочк пятерка магнит ашан лента перекрест вкусвилл дикси окей "
        "спар бристол красное-белое чижик светофор лавка сбермаркет купер самокат метро-кэш "
        "хлеб молок мясо курица овощ фрукт яйца яйцо крупа гречк макарон сыр вода бакалея клубник "
        "продуктовый супермаркет гипермаркет рынок"
    ),
    "cafe": (
        "кафе кофе кофейн латте капучино раф ресторан рест доставк шаурм шаверм бургер пицц "
        "суши ролл роллы макдак макдональдс мак вкусно-и-точка вкусноиточка kfc кфс ростикс "
        "бк додо фастфуд фаст-фуд столов обед ужин завтрак перекус снек чипс шоколад "
        "шоколадк сладост мороженое энергетик газировк кола пепси старбакс starbucks "
        "кулинари пекарн выпечк пирож хотдог хот-дог донер кебаб лапш вок бистро "
        "ланч бизнес-ланч фудкорт"
    ),
    "transport": (
        "такси uber убер яндекс-го метро автобус трамва троллейбус электричк маршрутк "
        "проезд транспорт бензин заправк топлив дизел парковк каршеринг делимобиль "
        "ситидрайв поезд ржд авиабилет самолет мойка шиномонтаж автосервис тройка азс"
    ),
    "home": (
        "аренд квартир ипотек коммунал коммуналк жкх квартплат электричеств свет газ "
        "отоплен ремонт мебел икеа хофф hoff леруа бытов посуд уборк химия стирк порошок"
    ),
    "subs": (
        "связь телефон мобильн мтс билайн мегафон теле2 tele2 йота yota интернет подписк "
        "netflix нетфликс spotify спотифай youtube ютуб яндекс-плюс icloud айклауд "
        "chatgpt кинопоиск иви окко ivi okko vpn впн премиум premium хостинг домен"
    ),
    "health": (
        "аптек лекарств таблет врач доктор клиник стоматолог зуб анализ больниц витамин "
        "медицин массаж оптик линз очки терапевт узи мрт зубы зубн"
    ),
    "clothes": (
        "одежд кроссовк кросы кроссы обув куртк футболк джинс штан брюк худи толстовк "
        "носк трус шапк пальто рубашк платье юбк zara зара uniqlo nike найк adidas адидас "
        "lamoda ламода спортмастер ботинк кед свитер"
    ),
    "fun": (
        "кино фильм игра игры игру steam стим playstation xbox клуб бар концерт театр музей "
        "боулинг квест караоке вечеринк тусовк тусовка развлечен аттракцион донат twitch "
        "бильярд пейнтбол аквапарк отпуск отель гостиниц путешеств тур"
    ),
    "shopping": (
        "wb вб вайлдберриз wildberries озон ozon али aliexpress алиэкспресс маркет мегамаркет "
        "шопинг покупк гаджет наушник айфон iphone airpods часы духи парфюм косметик "
        "техник dns днс мвидео м.видео эльдорадо фикс-прайс летуаль декор сувенир игрушк "
        "аксессуар чехол"
    ),
    "vice": (
        "сигарет сиги табак вейп жижа айкос iqos стики пиво пивас алкогол водк вино виски "
        "коньяк ром текила шампанск сидр бухло бухл кальян казино ставк ставка букмекер "
        "лотере 1xbet фонбет винлайн"
    ),
    "growth": (
        "спортзал зал фитнес абонемент тренер тренировк бассейн книг курс курсы обучен "
        "учеба репетитор вебинар семинар протеин креатин спортпит английск udemy skillbox книжк "
        "скиллбокс нетология coursera stepik практикум бокс единоборств йога"
    ),
    "gifts": "подарок подарк цвет цветы букет днюх юбилей сюрприз",
    "debt": "кредит долг займ микрозайм рассрочк штраф налог пени",
}

_INCOME_WORDS: dict[str, str] = {
    "salary": "зп зарплат аванс оклад получк преми премия отпускн больничн",
    "side": "подработк фриланс freelance заказ халтур шабашк чаевые чаев",
    "business": "бизнес продаж продал продала выручк клиент авито avito перепродаж",
    "gift_in": "подарили подарок подарк днюх",
    "invest": "дивиденд процент вклад кэшбэк кешбек кэшбек cashback инвест акци крипт биткоин btc",
}


def _compile(words: dict[str, str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    tokens: list[tuple[str, str]] = []
    phrases: list[tuple[str, str]] = []
    for key, line in words.items():
        for w in line.split():
            w = w.replace("ё", "е")
            if "-" in w:
                # «яндекс-го», «вкусно-и-точка» — ловим и через дефис, и через пробел
                phrases.append((w.replace("-", " "), key))
            tokens.append((w, key))
    # Длинные слова проверяем раньше коротких («кинопоиск» раньше «кино»).
    tokens.sort(key=lambda t: -len(t[0]))
    phrases.sort(key=lambda t: -len(t[0]))
    return tokens, phrases


_EXPENSE_INDEX = _compile(_EXPENSE_WORDS)
_INCOME_INDEX = _compile(_INCOME_WORDS)
_WORD_RE = re.compile(r"[a-zа-я0-9][a-zа-я0-9.\-]*", re.I)


def _normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def detect(note: str | None, kind: str) -> str | None:
    """Угадывает категорию по комментарию. None — если не узнал."""
    if not note:
        return None
    text = _normalize(note)
    tokens, phrases = _INCOME_INDEX if kind == "income" else _EXPENSE_INDEX
    for phrase, key in phrases:
        if phrase in text:
            return key
    words = _WORD_RE.findall(text)
    for kw, key in tokens:
        for word in words:
            if (len(kw) < 4 and word == kw) or (len(kw) >= 4 and word.startswith(kw)):
                return key
    return None


def guess_kind(note: str | None) -> str:
    """Для записи без знака: «зарплата 50000» — доход, всё остальное — расход."""
    if detect(note, "income") and not detect(note, "expense"):
        return "income"
    return "expense"
