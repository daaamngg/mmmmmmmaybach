"""Мини-приложение: подпись Telegram, API и включение кнопки в чате."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest_asyncio
from aiogram import Bot
from aiogram.methods import SendMessage, SendPhoto, SetChatMenuButton
from aiohttp.test_utils import TestClient, TestServer

from finbot import ui
from finbot.config import Config
from finbot.context import App
from finbot.db import Database
from finbot.webapp.auth import user_id_from_init_data
from finbot.webapp.server import WebServer, create_web_app

from .conftest import OWNER, STRANGER, TZ, FrozenClock, Harness
from .fake_telegram import FakeTelegram

TOKEN = "42:TEST"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"goal-photo"


def sign(user_id: int, *, token: str = TOKEN, auth_date: int | None = None, **extra: str) -> str:
    """initData так, как её подписывает Telegram."""
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Я"}, ensure_ascii=False, separators=(",", ":")),
        **extra,
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# ─────────────────────────── подпись ───────────────────────────


def test_signature_accepts_only_genuine_fresh_data() -> None:
    good = sign(OWNER)
    assert user_id_from_init_data(good, TOKEN) == OWNER
    assert user_id_from_init_data(sign(OWNER, signature="abc"), TOKEN) == OWNER  # новые поля Telegram
    assert user_id_from_init_data(good, "43:OTHER") is None  # подпись другого бота
    assert user_id_from_init_data(good.replace(str(OWNER), str(STRANGER)), TOKEN) is None  # подменили ID
    assert user_id_from_init_data("", TOKEN) is None
    assert user_id_from_init_data("hash=abc&auth_date=1", TOKEN) is None
    stale = sign(OWNER, auth_date=int(time.time()) - 2 * 86400)
    assert user_id_from_init_data(stale, TOKEN) is None


# ─────────────────────────── API ───────────────────────────


@dataclass
class Web:
    client: TestClient
    app: App
    tg: FakeTelegram
    db: Database

    async def call(
        self, method: str, path: str, *, uid: int | None = OWNER, headers: dict[str, str] | None = None, **kw: Any
    ) -> tuple[int, Any]:
        headers = dict(headers or {})
        if uid is not None:
            headers["Authorization"] = f"tma {sign(uid)}"
        async with self.client.request(method, path, headers=headers, **kw) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = await resp.json() if ctype.startswith("application/json") else await resp.read()
            return resp.status, body

    async def get(self, path: str, **kw: Any) -> Any:
        status, body = await self.call("GET", path, **kw)
        assert status == 200, body
        return body

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        status, body = await self.call("POST", path, json=data)
        assert status == 200, body
        return body


@pytest_asyncio.fixture
async def web(db: Database, frozen: FrozenClock, tmp_path: Path) -> AsyncIterator[Web]:
    config = Config(
        bot_token=TOKEN,
        owner_id=OWNER,
        data_dir=tmp_path,
        timezone=TZ,
        webapp_url="https://finbot.example",
        webapp_port=8080,
    )
    app = App(config=config, db=db)
    tg = FakeTelegram()
    bot = Bot(TOKEN, session=tg)
    client = TestClient(TestServer(create_web_app(app, bot)))
    await client.start_server()
    try:
        yield Web(client, app, tg, db)
    finally:
        await ui.drain()
        await client.close()


async def test_page_and_security_headers(web: Web) -> None:
    async with web.client.get("/") as resp:
        html = await resp.text()
        assert resp.status == 200
        assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "{{v}}" not in html and "/static/app.js?v=" in html
    async with web.client.get("/static/app.js?v=1") as resp:
        assert resp.status == 200 and "immutable" in resp.headers["Cache-Control"]
    health = await web.get("/healthz", uid=None)
    assert health["ok"] is True and health["id"]


async def test_api_requires_owner_signature(web: Web) -> None:
    status, body = await web.call("GET", "/api/state", uid=None)
    assert status == 401 and "заново" in body["error"]
    status, body = await web.call("GET", "/api/state", uid=STRANGER)
    assert status == 403 and "личное" in body["error"]
    status, _ = await web.call("POST", "/api/tx", uid=STRANGER, json={"kind": "expense", "amount": "100"})
    assert status == 403
    assert await web.db.count_tx() == 0


async def test_entries_goals_and_calendar(web: Web) -> None:
    state = await web.get("/api/state")
    assert state["currency"] == "₽" and state["goals"] == [] and state["today"] == "2026-09-25"

    goal = (await web.post("/api/goals", {"title": "Maybach", "target": "20кк"}))["goal"]
    assert goal["target"] == 2_000_000_000 and goal["saved"] == 0

    r = await web.post("/api/tx", {"kind": "income", "amount": "2 000", "note": "зарплата"})
    assert r["tx"]["category"] == "salary" and r["tx"]["saved"] == 160_000 and r["wallet"] == 40_000
    assert r["quote"] and r["completed"] == [] and r["ai_pending"] is False

    r = await web.post("/api/tx", {"kind": "expense", "amount": "350", "note": "шаурма"})
    assert r["tx"]["amount"] == 35_000 and r["tx"]["category"] == "cafe" and r["tx"]["impulse"] is True
    assert r["wallet"] == 5_000

    # Задним числом и с категорией, выбранной вручную, — бот её запоминает.
    r = await web.post(
        "/api/tx", {"kind": "expense", "amount": "1.5к", "note": "штуковина", "category": "home", "day": "2026-09-20"}
    )
    assert r["tx"]["day"] == "2026-09-20" and r["tx"]["amount"] == 150_000
    guess = await web.get("/api/classify?note=штуковина&kind=expense")
    assert guess == {"category": "home", "kind": "expense"}

    state = await web.get("/api/state")
    assert state["balances"]["wallet"] == -145_000 and state["balances"]["goals"] == 160_000
    assert state["month"]["income"] == 200_000 and state["month"]["expense"] == 185_000
    assert state["main_goal"] == goal["id"] and state["goals"][0]["saved"] == 160_000

    month = await web.get("/api/month?y=2026&m=9")
    assert month["days"]["2026-09-25"] == [200_000, 35_000] and month["days"]["2026-09-20"] == [0, 150_000]
    assert month["first_weekday"] == 1 and month["days_in_month"] == 30 and month["next"] is None
    assert {c["key"] for c in month["categories"]} == {"cafe", "home"}
    assert month["verdict"]

    day = await web.get("/api/day?d=2026-09-25")
    assert [t["kind"] for t in day["txs"]] == ["income", "expense"] and day["future"] is False
    history = await web.get("/api/history?limit=2")
    assert len(history["txs"]) == 2 and history["txs"][0]["note"] == "штуковина"


async def test_bad_input_is_explained(web: Web) -> None:
    for payload, status, words in [
        ({"kind": "expense", "amount": "много"}, 400, "сумму"),
        ({"kind": "gift", "amount": "100"}, 400, "расход или доход"),
        ({"kind": "expense", "amount": "100", "day": "2026-09-26"}, 400, "Будущее"),
        ({"kind": "expense", "amount": "100", "category": "salary"}, 400, "категория"),
    ]:
        status_, body = await web.call("POST", "/api/tx", json=payload)
        assert status_ == status and words in body["error"], body
    status, body = await web.call("POST", "/api/tx", data="не json", headers={"Content-Type": "application/json"})
    assert status == 400 and body["error"] == "Некорректный запрос"
    status, body = await web.call("POST", "/api/tx", data=b"{" + b" " * 70_000 + b"}")
    assert status == 413
    status, body = await web.call("GET", "/api/month?y=2026&m=13")
    assert status == 400
    assert await web.db.count_tx() == 0


async def test_edit_and_delete_entries(web: Web) -> None:
    tx = (await web.post("/api/tx", {"kind": "expense", "amount": "500", "note": "ершик"}))["tx"]
    status, body = await web.call("PATCH", f"/api/tx/{tx['id']}", json={"category": "home"})
    assert status == 200 and body["tx"]["category"] == "home"
    assert (await web.get("/api/classify?note=ершик&kind=expense"))["category"] == "home"
    status, body = await web.call("PATCH", f"/api/tx/{tx['id']}", json={"category": "salary"})
    assert status == 400

    status, _ = await web.call("DELETE", f"/api/tx/{tx['id']}")
    assert status == 200
    status, body = await web.call("DELETE", f"/api/tx/{tx['id']}")
    assert status == 404 and "удалена" in body["error"]

    # Доход, накопления с которого уже переложены в цель, удалить нельзя — копилка ушла бы в минус.
    income = (await web.post("/api/tx", {"kind": "income", "amount": "1000"}))["tx"]
    goal = (await web.post("/api/goals", {"title": "Часы", "target": "5000"}))["goal"]
    await web.post(f"/api/goals/{goal['id']}/deposit", {"amount": "800", "src": "pool"})
    status, body = await web.call("DELETE", f"/api/tx/{income['id']}")
    assert status == 409 and "уже ушли" in body["error"]


async def test_deposit_and_celebration_in_chat(web: Web) -> None:
    await web.post("/api/tx", {"kind": "income", "amount": "1000"})  # целей нет: 800 в копилку, 200 на расходы
    goal = (await web.post("/api/goals", {"title": "Часы", "target": "1000"}))["goal"]
    status, body = await web.call("POST", f"/api/goals/{goal['id']}/deposit", json={"amount": "500", "src": "wallet"})
    assert status == 409 and "нет" in body["error"]  # на расходах всего 200

    r = await web.post(f"/api/goals/{goal['id']}/deposit", {"amount": "400", "src": "outside"})
    assert r["goal"]["saved"] == 40_000 and r["amount"] == 40_000 and r["completed"] == []
    assert (await web.get("/api/state"))["balances"]["wallet"] == 20_000  # «отложено раньше» не трогает кошелёк

    r = await web.post(f"/api/goals/{goal['id']}/deposit", {"amount": "200", "src": "wallet"})
    assert r["goal"]["saved"] == 60_000 and r["completed"] == []
    r = await web.post(f"/api/goals/{goal['id']}/deposit", {"amount": "400", "src": "pool"})
    assert r["goal"]["reached"] is True and r["completed"] == ["Часы"]
    await ui.drain()
    texts = [c.text for c in web.tg.calls if isinstance(c, SendMessage)]
    assert any("Часы" in t for t in texts), texts  # праздник пришёл и в чат


async def test_goal_photos_upload_view_delete(web: Web) -> None:
    goal = (await web.post("/api/goals", {"title": "Квартира", "target": "5кк"}))["goal"]
    status, body = await web.call("POST", f"/api/goals/{goal['id']}/photos", data=b"GIF89a...")
    assert status == 415 and "фото" in body["error"]

    status, body = await web.call("POST", f"/api/goals/{goal['id']}/photos", data=JPEG)
    assert status == 200 and body["added"] is True
    photo_id = body["goal"]["photos"][0]
    sent = [c for c in web.tg.calls if isinstance(c, SendPhoto)]
    assert len(sent) == 1 and sent[0].chat_id == OWNER and "Квартира" in (sent[0].caption or "")
    stored = await web.db.get_photo(photo_id)
    assert stored is not None and stored.file_id.startswith("uploaded_") and stored.local_path
    local = web.app.config.data_dir / stored.local_path
    assert local.read_bytes() == JPEG

    status, data = await web.call("GET", f"/api/photo/{photo_id}")
    assert status == 200 and data == JPEG
    state = await web.get("/api/state")
    assert state["goals"][0]["photos"] == [photo_id]

    status, body = await web.call("DELETE", f"/api/photo/{photo_id}")
    assert status == 200 and body["goal"]["photos"] == []
    assert not local.exists()
    status, _ = await web.call("GET", f"/api/photo/{photo_id}")
    assert status == 404


async def test_photo_without_local_copy_is_fetched_from_telegram(web: Web) -> None:
    goal_id = await web.db.create_goal("BMW", 100_000_000)
    photo_id = await web.db.add_photo(goal_id, "tg_file_1", "u_tg_file_1")
    assert photo_id is not None
    status, data = await web.call("GET", f"/api/photo/{photo_id}")
    assert status == 200 and data.startswith(b"\xff\xd8\xff")
    stored = await web.db.get_photo(photo_id)
    assert stored is not None and stored.local_path  # копия сохранилась на диск


class FakeAI:
    title = "Fake (test)"
    status = "работает ✅"

    async def categorize(self, note: str, kind: str) -> str | None:
        return "home"


async def test_unknown_word_goes_to_ai_in_background(web: Web) -> None:
    web.app.ai = FakeAI()  # type: ignore[assignment]
    r = await web.post("/api/tx", {"kind": "expense", "amount": "300", "note": "ершик для унитаза"})
    assert r["ai_pending"] is True and r["tx"]["category"] == "other"
    await ui.drain()
    assert (await web.get("/api/history"))["txs"][0]["category"] == "home"
    assert (await web.get("/api/state"))["ai"] == "работает ✅"


# ─────────────────────────── кнопка в чате ───────────────────────────


async def test_watch_enables_menu_button_when_https_answers(db: Database, frozen: FrozenClock, tmp_path: Path) -> None:
    config = Config(bot_token=TOKEN, owner_id=OWNER, data_dir=tmp_path, timezone=TZ, webapp_port=0)
    app = App(config=config, db=db)
    tg = FakeTelegram()
    server = WebServer(app, Bot(TOKEN, session=tg))
    await server.start()
    try:
        assert server._runner is not None
        port = server._runner.addresses[0][1]
        # Чужой сервер на том же адресе не считается: ответ должен содержать наш секрет.
        assert not await server._reachable(f"http://127.0.0.1:{port}/nope")
        # Первый адрес не отвечает (например, нет сертификата) — бот берёт запасной.
        app.config = replace(config, webapp_url=f"http://127.0.0.1:1,http://127.0.0.1:{port}")
        await server.watch()
    finally:
        await server.stop()
    assert app.webapp_ready is True and app.webapp_url == f"http://127.0.0.1:{port}"
    buttons = [c for c in tg.calls if isinstance(c, SetChatMenuButton)]
    assert len(buttons) == 1 and buttons[0].chat_id == OWNER
    assert buttons[0].menu_button.web_app.url == f"http://127.0.0.1:{port}"  # type: ignore[union-attr]


async def test_app_command_in_chat(chat: Harness) -> None:
    await chat.send("/start")
    await chat.send("/app")
    assert "finbot webapp on" in chat.last_text()
    await chat.send("/settings")
    assert "📱 Приложение: не включено" in chat.last_text()

    chat.app.config = replace(chat.app.config, webapp_url="https://1-2-3-4.sslip.io")
    await chat.send("/app")
    assert "finbot doctor" in chat.last_text()  # сертификат ещё не получен
    await chat.send("/settings")
    assert "📱 Приложение: запускается" in chat.last_text()

    chat.app.webapp_url = "https://1-2-3-4.sslip.io"  # проверка HTTPS прошла
    await chat.send("/app")
    button = chat.last().reply_markup.inline_keyboard[0][0]  # type: ignore[union-attr]
    assert button.web_app is not None and button.web_app.url == "https://1-2-3-4.sslip.io"
    await chat.send("/start")
    assert chat.last().reply_markup.inline_keyboard[0][0].web_app is not None  # type: ignore[union-attr]
    menu = [c for c in chat.tg.calls if isinstance(c, SetChatMenuButton)]
    assert menu and menu[-1].chat_id == OWNER
    await chat.send("/settings")
    assert "📱 Приложение: работает ✅" in chat.last_text()
