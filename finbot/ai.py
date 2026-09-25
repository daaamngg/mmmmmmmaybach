"""Необязательная нейросеть для категорий (любой OpenAI-совместимый API).

Включается одной строкой в .env: AI_API_KEY=... По ключу бот сам понимает провайдера:
  gsk_…   → Groq       (бесплатно, без карты: console.groq.com/keys)
  sk-or-… → OpenRouter (бесплатные модели: openrouter.ai/keys)
Любой другой сервис — задать AI_BASE_URL и AI_MODEL (например, свой Ollama).

Нейросеть получает только текст комментария («штуковина для ванной») — без сумм,
дат и чего-либо ещё. Ответ проверяется по списку категорий, так что сломать учёт
она не может: в худшем случае запись останется в «Прочем».
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from . import categories

log = logging.getLogger(__name__)

# префикс ключа → (адрес API, модель, название)
PRESETS: dict[str, tuple[str, str, str]] = {
    "gsk_": ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b", "Groq"),
    "sk-or-": ("https://openrouter.ai/api/v1", "openrouter/free", "OpenRouter"),
}

SYSTEM_PROMPT = (
    "Ты помогаешь вести личный бюджет: определяешь категорию записи по её описанию. "
    "Отвечай ровно одним словом — ключом категории из списка, без пояснений."
)

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


@dataclass(frozen=True)
class AIConfig:
    api_key: str
    base_url: str
    model: str
    name: str


class AIConfigError(ValueError):
    pass


def make_config(api_key: str | None, base_url: str | None = None, model: str | None = None) -> AIConfig | None:
    """Настройки нейросети из .env. None — нейросеть выключена."""
    key = (api_key or "").strip()
    if not key:
        return None
    preset = next((p for prefix, p in PRESETS.items() if key.startswith(prefix)), None)
    url = (base_url or "").strip() or (preset[0] if preset else "")
    name_model = (model or "").strip() or (preset[1] if preset else "")
    if not url or not name_model:
        raise AIConfigError(
            "Не знаю, к какому сервису этот AI_API_KEY. Укажи в .env AI_BASE_URL и AI_MODEL "
            "(или возьми бесплатный ключ Groq/OpenRouter)."
        )
    title = preset[2] if preset and not (base_url or "").strip() else (urlparse(url).hostname or url)
    return AIConfig(api_key=key, base_url=url.rstrip("/"), model=name_model, name=title)


def build_prompt(note: str, kind: str) -> str:
    what = "доходе" if kind == "income" else "расходе"
    default = categories.default_for(kind)
    lines = [f"Запись о {what}: «{note}»", "", "Категории (ключ — описание):"]
    for c in categories.for_kind(kind):
        hint = " (если ничего не подходит)" if c.key == default else ""
        lines.append(f"{c.key} — {c.name}{hint}")
    lines += ["", "Ответ (только ключ):"]
    return "\n".join(lines)


def parse_category(text: str | None, kind: str) -> str | None:
    """Достаёт ключ категории из ответа модели. None — если ответ непонятный."""
    if not text:
        return None
    text = _THINK.sub(" ", text).lower()
    options = categories.for_kind(kind)
    keys = [c.key for c in options]
    found = re.findall(r"(?<![a-z_])(" + "|".join(sorted(keys, key=len, reverse=True)) + r")(?![a-z_])", text)
    if found:
        return found[-1]
    # Иногда модель отвечает названием, а не ключом.
    for c in options:
        if c.name.lower() in text:
            return c.key
    return None


class AICategorizer:
    def __init__(self, config: AIConfig, *, timeout: float = 20, trust_env: bool = True) -> None:
        self.config = config
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._trust_env = trust_env
        self._session: aiohttp.ClientSession | None = None
        self._warned: set[str] = set()
        # Для экрана настроек: сколько раз ответила и чем закончилась последняя попытка.
        self.answered = 0
        self.last_error: str | None = None

    @property
    def title(self) -> str:
        return f"{self.config.name} ({self.config.model})"

    @property
    def status(self) -> str:
        if self.last_error:
            return f"⚠️ {self.last_error}"
        return "работает ✅" if self.answered else "подключена, ждёт незнакомых слов"

    def payload(self, note: str, kind: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(note, kind)},
            ],
            "temperature": 0,
            "max_tokens": 800,  # с запасом: «думающие» модели тратят токены на рассуждение
        }
        if "groq.com" in self.config.base_url and "gpt-oss" in self.config.model:
            # Без этого gpt-oss на Groq может «задуматься» и вернуть пустой ответ.
            body["reasoning_effort"] = "low"
        return body

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, trust_env=self._trust_env)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _fail(self, key: str, status: str, message: str, *args: Any) -> None:
        """Запоминает ошибку для настроек; в лог одинаковые ошибки пишем один раз."""
        self.last_error = status
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    async def categorize(self, note: str, kind: str) -> str | None:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        try:
            session = await self._get_session()
            url = f"{self.config.base_url}/chat/completions"
            async with session.post(url, json=self.payload(note, kind), headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    if resp.status in (401, 403):
                        status = "ключ не принят — проверь его (finbot ai)"
                    elif resp.status == 429:
                        status = "исчерпан бесплатный лимит, попробую позже"
                    elif resp.status in (400, 404) and "model" in body.lower():
                        status = "модель недоступна — укажи другую в AI_MODEL"
                    else:
                        status = f"сервис ответил ошибкой {resp.status}"
                    self._fail(f"http{resp.status}", status, "Нейросеть ответила %s: %s", resp.status, body)
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self._fail(type(e).__name__, "нет связи с сервисом", "Нейросеть недоступна: %r", e)
            return None
        try:
            content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            self._fail("format", "непонятный ответ сервиса", "Непонятный ответ нейросети: %.300s", data)
            return None
        self.answered += 1
        self.last_error = None
        return parse_category(content, kind)


PROBE_NOTE = "капучино в кофейне"


async def probe(ai: AICategorizer) -> None:
    """Пробный запрос при запуске: сразу видно в логе, работает ли ключ."""
    category = await ai.categorize(PROBE_NOTE, "expense")
    if ai.last_error:
        log.warning("Нейросеть не работает: %s", ai.last_error)
    else:
        log.info("Нейросеть отвечает ✅ (проверка: «%s» → %s)", PROBE_NOTE, category or "не поняла")
