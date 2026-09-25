"""Имитация Telegram Bot API для тестов: бот работает по-настоящему, но без сети.

FakeTelegram хранит отправленные сообщения и ведёт себя как Telegram в важных мелочах:
ошибка «message is not modified», нельзя редактировать текст у фото, удалённое
сообщение нельзя отредактировать. Проверяет HTML-разметку и лимиты длины.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from html.parser import HTMLParser
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, InputFile, Message

BOT_ID = 42
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}


class _HtmlCheck(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.visible: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"bad closing </{tag}>, open: {self.stack}")
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.visible.append(data)


def check_html(text: str, limit: int) -> str:
    """Проверяет разметку как Telegram и возвращает видимый текст."""
    parser = _HtmlCheck()
    parser.feed(text)
    parser.close()
    if parser.stack:
        parser.errors.append(f"unclosed tags: {parser.stack}")
    assert not parser.errors, f"HTML errors {parser.errors} in:\n{text}"
    visible = "".join(parser.visible)
    assert len(visible.strip()) > 0, f"empty message: {text!r}"
    assert len(visible) <= limit, f"message too long ({len(visible)} > {limit})"
    return visible


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.messages: dict[tuple[int, int], Message] = {}
        self.deleted: set[tuple[int, int]] = set()
        self._mid = 10_000
        self._files = 0

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b"\xff\xd8\xff\xe0fake-jpeg"

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        handler = getattr(self, f"_{type(method).__name__}", None)
        if handler is None:
            return True
        return handler(bot, method)

    # ── helpers ──

    def _file_id(self, media: Any) -> tuple[str, str]:
        if isinstance(media, InputFile):
            self._files += 1
            fid = f"uploaded_{self._files}"
        else:
            fid = str(media)
        return fid, f"u_{fid}"

    def _store(self, bot: Bot, chat_id: int, message_id: int, **fields: Any) -> Message:
        markup = fields.pop("reply_markup", None)
        data: dict[str, Any] = {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private", "first_name": "Me"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "FinBot"},
            **fields,
        }
        if isinstance(markup, InlineKeyboardMarkup):
            data["reply_markup"] = markup.model_dump(exclude_none=True)
        msg = Message.model_validate(data, context={"bot": bot})
        self.messages[(chat_id, message_id)] = msg
        return msg

    def _new(self, bot: Bot, chat_id: int, **fields: Any) -> Message:
        self._mid += 1
        return self._store(bot, chat_id, self._mid, **fields)

    def _existing(self, method: Any) -> Message:
        key = (int(method.chat_id), int(method.message_id))
        if key not in self.messages or key in self.deleted:
            raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        return self.messages[key]

    @staticmethod
    def _same_markup(msg: Message, markup: InlineKeyboardMarkup | None) -> bool:
        old = msg.reply_markup.model_dump(exclude_none=True) if msg.reply_markup else None
        new = markup.model_dump(exclude_none=True) if markup else None
        return old == new

    def _photo(self, media: Any) -> list[dict[str, Any]]:
        fid, uid = self._file_id(media)
        return [{"file_id": fid, "file_unique_id": uid, "width": 1280, "height": 960}]

    # ── методы API ──

    def _AnswerCallbackQuery(self, bot: Bot, m: Any) -> bool:
        assert m.text is None or len(m.text) <= 200, f"callback answer too long: {m.text!r}"
        return True

    def _GetMe(self, bot: Bot, method: Any) -> Any:
        from aiogram.types import User

        return User(id=BOT_ID, is_bot=True, first_name="FinBot", username="finbot_test_bot")

    def _SendMessage(self, bot: Bot, m: Any) -> Message:
        check_html(m.text, 4096)
        return self._new(bot, int(m.chat_id), text=m.text, reply_markup=m.reply_markup)

    def _SendPhoto(self, bot: Bot, m: Any) -> Message:
        if m.caption:
            check_html(m.caption, 1024)
        if isinstance(m.photo, str) and m.photo.startswith("broken"):
            raise TelegramBadRequest(method=m, message="Bad Request: wrong file identifier/HTTP URL specified")
        return self._new(
            bot, int(m.chat_id), photo=self._photo(m.photo), caption=m.caption, reply_markup=m.reply_markup
        )

    def _SendDocument(self, bot: Bot, m: Any) -> Message:
        fid, uid = self._file_id(m.document)
        name = getattr(m.document, "filename", None) or "file"
        return self._new(
            bot,
            int(m.chat_id),
            document={"file_id": fid, "file_unique_id": uid, "file_name": name},
            caption=m.caption,
        )

    def _EditMessageText(self, bot: Bot, m: Any) -> Message:
        msg = self._existing(m)
        if msg.photo:
            raise TelegramBadRequest(method=m, message="Bad Request: there is no text in the message to edit")
        check_html(m.text, 4096)
        if msg.text == m.text and self._same_markup(msg, m.reply_markup):
            raise TelegramBadRequest(method=m, message="Bad Request: message is not modified")
        return self._store(bot, msg.chat.id, msg.message_id, text=m.text, reply_markup=m.reply_markup)

    def _EditMessageCaption(self, bot: Bot, m: Any) -> Message:
        msg = self._existing(m)
        if not msg.photo:
            raise TelegramBadRequest(method=m, message="Bad Request: there is no caption in the message to edit")
        check_html(m.caption or " ", 1024)
        if msg.caption == m.caption and self._same_markup(msg, m.reply_markup):
            raise TelegramBadRequest(method=m, message="Bad Request: message is not modified")
        photo = [p.model_dump(exclude_none=True) for p in msg.photo]
        return self._store(
            bot, msg.chat.id, msg.message_id, photo=photo, caption=m.caption, reply_markup=m.reply_markup
        )

    def _EditMessageMedia(self, bot: Bot, m: Any) -> Message:
        msg = self._existing(m)
        if not msg.photo:
            raise TelegramBadRequest(method=m, message="Bad Request: message can't be edited")
        media = m.media
        if media.caption:
            check_html(media.caption, 1024)
        if isinstance(media.media, str) and media.media.startswith("broken"):
            raise TelegramBadRequest(method=m, message="Bad Request: wrong file identifier/HTTP URL specified")
        return self._store(
            bot,
            msg.chat.id,
            msg.message_id,
            photo=self._photo(media.media),
            caption=media.caption,
            reply_markup=m.reply_markup,
        )

    def _EditMessageReplyMarkup(self, bot: Bot, m: Any) -> Message:
        msg = self._existing(m)
        if self._same_markup(msg, m.reply_markup):
            raise TelegramBadRequest(method=m, message="Bad Request: message is not modified")
        fields: dict[str, Any] = {"reply_markup": m.reply_markup}
        if msg.photo:
            fields.update(photo=[p.model_dump(exclude_none=True) for p in msg.photo], caption=msg.caption)
        else:
            fields.update(text=msg.text)
        return self._store(bot, msg.chat.id, msg.message_id, **fields)

    def _DeleteMessage(self, bot: Bot, m: Any) -> bool:
        key = (int(m.chat_id), int(m.message_id))
        if key not in self.messages or key in self.deleted:
            raise TelegramBadRequest(method=m, message="Bad Request: message to delete not found")
        self.deleted.add(key)
        return True

    def _GetFile(self, bot: Bot, m: Any) -> Any:
        from aiogram.types import File

        return File(file_id=m.file_id, file_unique_id=f"u_{m.file_id}", file_path=f"photos/{m.file_id}.jpg")

    # ── удобства для тестов ──

    def live(self, chat_id: int) -> list[Message]:
        """Сообщения бота, которые сейчас видны в чате (без удалённых), по порядку."""
        return [
            msg
            for (cid, mid), msg in sorted(self.messages.items(), key=lambda kv: kv[0][1])
            if cid == chat_id and (cid, mid) not in self.deleted
        ]
