from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Message, Update

from finbot import clock, ui
from finbot.app import build_dispatcher
from finbot.config import Config
from finbot.context import App
from finbot.db import Database

from .fake_telegram import FakeTelegram

OWNER = 777_000
STRANGER = 555_000
TZ = timezone(timedelta(hours=3))


class FrozenClock:
    """Управляемое время: clock.now() возвращает self.value."""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def now(self) -> datetime:
        return self.value

    def advance(self, **kw: float) -> None:
        self.value += timedelta(**kw)


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    fc = FrozenClock(datetime(2026, 9, 25, 14, 0, tzinfo=TZ))
    monkeypatch.setattr(clock, "_tz", TZ)
    monkeypatch.setattr(clock, "now", fc.now)
    return fc


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.open()
    yield database
    await database.close()


_DISPATCHER: Dispatcher | None = None


def shared_dispatcher(app: App) -> Dispatcher:
    """Роутеры aiogram подключаются к диспетчеру один раз, поэтому он общий на все тесты."""
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = build_dispatcher(app)
    _DISPATCHER.workflow_data.update(app=app, db=app.db)
    return _DISPATCHER


class Harness:
    """Чат с ботом: отправка сообщений, нажатия кнопок, проверка ответов."""

    def __init__(self, dp: Dispatcher, bot: Bot, tg: FakeTelegram, app: App, clock_: FrozenClock) -> None:
        self.dp = dp
        self.bot = bot
        self.tg = tg
        self.app = app
        self.db = app.db
        self.clock = clock_
        self._ids = itertools.count(1)
        self._user_mid = itertools.count(1)
        self.callbacks = 0

    def _user(self, uid: int) -> dict[str, Any]:
        return {"id": uid, "is_bot": False, "first_name": "Я" if uid == OWNER else "Чужой"}

    async def _feed(self, **payload: Any) -> None:
        # У каждого теста своё хранилище состояний, чтобы тесты не влияли друг на друга.
        update = Update.model_validate({"update_id": next(self._ids), **payload}, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)
        await ui.drain()

    async def send(self, text: str | None = None, *, photo: str | None = None, uid: int = OWNER, **extra: Any) -> None:
        msg: dict[str, Any] = {
            "message_id": next(self._user_mid),
            "date": int(self.clock.value.timestamp()),
            "chat": {"id": uid, "type": "private", "first_name": "Я"},
            "from": self._user(uid),
            **extra,
        }
        if text is not None:
            msg["text"] = text
        if photo is not None:
            msg["photo"] = [
                {"file_id": f"{photo}_s", "file_unique_id": f"u_{photo}_s", "width": 90, "height": 90},
                {"file_id": photo, "file_unique_id": f"u_{photo}", "width": 1280, "height": 960},
            ]
        await self._feed(message=msg)

    def find(self, text_part: str, exact: bool = False) -> tuple[Message, str]:
        """Последнее видимое сообщение с кнопкой, в тексте которой есть text_part."""
        for msg in reversed(self.tg.live(OWNER)):
            if msg.reply_markup:
                for row in msg.reply_markup.inline_keyboard:
                    for button in row:
                        hit = button.text == text_part if exact else text_part in button.text
                        if hit and button.callback_data:
                            return msg, button.callback_data
        raise AssertionError(f"Кнопка «{text_part}» не найдена. Видно:\n{self.screen()}")

    async def click(self, text_part: str, exact: bool = False) -> None:
        msg, data = self.find(text_part, exact)
        await self.click_data(msg, data)

    async def click_data(self, msg: Message, data: str, uid: int = OWNER) -> None:
        self.callbacks += 1
        await self._feed(
            callback_query={
                "id": f"cq{next(self._ids)}",
                "from": self._user(uid),
                "chat_instance": "ci",
                "message": msg.model_dump(exclude_none=True, by_alias=True),
                "data": data,
            }
        )

    # ── проверки ──

    def last(self) -> Message:
        return self.tg.live(OWNER)[-1]

    def last_text(self) -> str:
        msg = self.last()
        return msg.text or msg.caption or ""

    def screen(self, n: int = 3) -> str:
        parts = []
        for msg in self.tg.live(OWNER)[-n:]:
            buttons = []
            if msg.reply_markup:
                buttons = [b.text for row in msg.reply_markup.inline_keyboard for b in row]
            parts.append(f"[{msg.message_id}] {'📷 ' if msg.photo else ''}{msg.text or msg.caption}\n  {buttons}")
        return "\n".join(parts)

    def all_text(self) -> str:
        return "\n".join((m.text or m.caption or "") for m in self.tg.live(OWNER))

    def sent_texts(self) -> list[str]:
        return [c.text for c in self.tg.calls if isinstance(c, SendMessage)]

    def assert_healthy(self) -> None:
        errors = [t for t in self.sent_texts() if "Что-то пошло не так" in t]
        assert not errors, "Бот упал при обработке — смотри лог"
        answers = [c for c in self.tg.calls if isinstance(c, AnswerCallbackQuery)]
        assert len(answers) == self.callbacks, f"ответов на кнопки {len(answers)}, нажатий {self.callbacks}"


@pytest_asyncio.fixture
async def chat(db: Database, frozen: FrozenClock, tmp_path: Path) -> AsyncIterator[Harness]:
    config = Config(bot_token="42:TEST", owner_id=None, data_dir=tmp_path, timezone=TZ)
    app = App(config=config, db=db, photo_debounce=0.01)
    dp = shared_dispatcher(app)
    from aiogram.fsm.storage.memory import MemoryStorage

    dp.fsm.storage = MemoryStorage()
    tg = FakeTelegram()
    bot = Bot("42:TEST", session=tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    harness = Harness(dp, bot, tg, app, frozen)
    yield harness
    await ui.drain()
    harness.assert_healthy()


@pytest.fixture
def today() -> Iterator[date]:
    yield date(2026, 9, 25)
