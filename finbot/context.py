"""Общие объекты приложения, которые получают обработчики."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ai import AICategorizer
from .config import Config
from .db import Database
from .ui import Debouncer


@dataclass
class App:
    config: Config
    db: Database
    photo_debounce: float = 1.2
    # Фото, присланные «просто так» (без диалога): chat_id → [(file_id, file_unique_id)]
    pending_photos: dict[int, list[tuple[str, str | None]]] = field(default_factory=dict)
    photos_added: dict[int, int] = field(default_factory=dict)
    ai: AICategorizer | None = None  # нейросеть для категорий (если задан AI_API_KEY)
    webapp_url: str | None = None  # адрес мини-приложения, который точно открывается (проверен)
    debouncer: Debouncer = field(init=False)

    def __post_init__(self) -> None:
        self.debouncer = Debouncer(self.photo_debounce)

    @property
    def owner_id(self) -> int | None:
        return self.config.owner_id or self.db.settings.owner_id

    @property
    def webapp_ready(self) -> bool:
        return self.webapp_url is not None
