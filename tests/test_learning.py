"""Бот запоминает категории, которые ты выбираешь, и нейросеть (если включена)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from aiohttp import web

from finbot import classify
from finbot.ai import AICategorizer, AIConfigError, make_config, parse_category
from finbot.db import SCHEMA_V1, Database


def test_keys_and_main_word() -> None:
    assert classify.main_word("купил хлеб в магазине") == "хлеб"
    assert classify.main_word("Бенз 95") == "бенз"
    assert classify.main_word("на 100") is None
    assert classify.keys_for("Кофе с собой!") == ["=кофе с собой", "~кофе"]
    assert classify.keys_for("") == []


async def test_user_choice_beats_dictionary_and_ai(db: Database, frozen) -> None:
    assert await classify.resolve(db, "шаурма", "expense") == "cafe"  # словарь
    assert await classify.resolve(db, "бенз 95", "expense") is None
    await classify.remember(db, "бенз 95", "expense", "transport")
    assert await classify.resolve(db, "бенз 95", "expense") == "transport"
    assert await classify.resolve(db, "бенз 92 полный бак", "expense") == "transport"  # по главному слову
    # Твой выбор главнее словаря
    await classify.remember(db, "шаурма", "expense", "food")
    assert await classify.resolve(db, "шаурма", "expense") == "food"
    # Нейросеть не может перезаписать то, чему научил ты…
    await classify.remember(db, "шаурма", "expense", "fun", source="ai")
    assert await classify.resolve(db, "шаурма", "expense") == "food"
    # …и запоминает только фразу целиком, не обобщая слово.
    await classify.remember(db, "мыло для рук", "expense", "home", source="ai")
    assert await classify.resolve(db, "мыло для рук", "expense") == "home"
    assert await classify.resolve(db, "мыло хозяйственное", "expense") is None
    # Мусор в категории не принимается.
    await classify.remember(db, "штука", "expense", "salary")
    assert await classify.resolve(db, "штука", "expense") is None
    assert await db.learned_count() == 5


async def test_guess_kind_uses_memory(db: Database, frozen) -> None:
    assert await classify.guess_kind(db, "калым на даче") == "expense"
    await classify.remember(db, "калым на даче", "income", "side")
    assert await classify.guess_kind(db, "калым на даче") == "income"
    assert await classify.guess_kind(db, "калым у соседа") == "income"


async def test_migration_from_v1_keeps_data(tmp_path: Path, frozen) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1 + "PRAGMA user_version = 1;")
    conn.execute("INSERT INTO settings(key, value) VALUES ('goal_pct', '70')")
    conn.execute(
        "INSERT INTO tx(kind, amount, category, note, day, created_at) VALUES ('expense', 500, 'cafe', 'кофе', '2026-09-01', 0)"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    await db.open()
    assert db.settings.goal_pct == 70
    assert await db.count_tx() == 1
    await classify.remember(db, "кофе", "expense", "food")
    assert await db.learned_count() == 2
    await db.close()
    version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert version == 2


def test_groq_gpt_oss_gets_low_reasoning_effort() -> None:
    groq = AICategorizer(make_config("gsk_abc"))  # type: ignore[arg-type]
    body = groq.payload("ершик", "expense")
    assert body["model"] == "openai/gpt-oss-20b" and body["reasoning_effort"] == "low"
    assert "ершик" in body["messages"][1]["content"]
    other = AICategorizer(make_config("sk-or-v1-abc"))  # type: ignore[arg-type]
    assert "reasoning_effort" not in other.payload("ершик", "expense")
    custom = AICategorizer(make_config("gsk_abc", model="qwen/qwen3-32b"))  # type: ignore[arg-type]
    assert "reasoning_effort" not in custom.payload("ершик", "expense")


def test_ai_config_presets() -> None:
    assert make_config("") is None
    groq = make_config("gsk_abc")
    assert groq is not None and "groq" in groq.base_url and groq.model and groq.name == "Groq"
    router = make_config("sk-or-v1-abc")
    assert router is not None and "openrouter" in router.base_url and router.name == "OpenRouter"
    custom = make_config("key", "http://localhost:11434/v1/", "qwen2.5:3b")
    assert custom is not None and custom.base_url == "http://localhost:11434/v1" and custom.name == "localhost"
    with pytest.raises(AIConfigError):
        make_config("unknown-key")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("food", "food"),
        (" Food\n", "food"),
        ("<think>может cafe? нет</think>food", "food"),
        ("Категория: transport.", "transport"),
        ("Продукты", "food"),
        ("seafood", None),
        ("не знаю", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_category(answer: str | None, expected: str | None) -> None:
    assert parse_category(answer, "expense") == expected


def test_parse_income_category() -> None:
    assert parse_category("other_in", "income") == "other_in"
    assert parse_category("salary", "income") == "salary"
    assert parse_category("food", "income") is None


async def _fake_openai(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/v1"


async def test_ai_client_against_fake_api() -> None:
    seen: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append({"auth": request.headers.get("Authorization"), **body})
        note = body["messages"][1]["content"]
        auth = request.headers.get("Authorization", "")
        if "ключ-плохой" in auth:
            return web.json_response({"error": "invalid key"}, status=401)
        if "лимит" in note:
            return web.json_response({"error": "rate limit"}, status=429)
        if "старая-модель" in body["model"]:
            return web.json_response({"error": {"code": "model_decommissioned"}}, status=400)
        if "медленно" in note:
            await asyncio.sleep(2)
        if "мусор" in note:
            return web.json_response({"unexpected": True})
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "<think>…</think> home"}}]})

    runner, url = await _fake_openai(handler)
    try:
        cfg = make_config("ключ-хороший", url, "test-model")
        assert cfg is not None
        ai = AICategorizer(cfg, timeout=1, trust_env=False)
        assert "ждёт" in ai.status
        assert await ai.categorize("ершик для унитаза", "expense") == "home"
        assert ai.status == "работает ✅"
        assert seen[0]["auth"] == "Bearer ключ-хороший" and seen[0]["model"] == "test-model"
        assert "ершик для унитаза" in seen[0]["messages"][1]["content"]
        assert await ai.categorize("мусор", "expense") is None
        assert "непонятный ответ" in ai.status
        assert await ai.categorize("медленно", "expense") is None  # таймаут — не падаем
        assert "нет связи" in ai.status
        assert await ai.categorize("лимит", "expense") is None
        assert "лимит" in ai.status
        assert await ai.categorize("снова ершик", "expense") == "home"
        assert ai.status == "работает ✅"  # ошибка ушла, как только сервис снова ответил
        await ai.close()
        bad = AICategorizer(make_config("ключ-плохой", url, "m"), trust_env=False)  # type: ignore[arg-type]
        assert await bad.categorize("что-то", "expense") is None
        assert "ключ не принят" in bad.status
        await bad.close()
        old = AICategorizer(make_config("ключ-хороший", url, "старая-модель"), trust_env=False)  # type: ignore[arg-type]
        assert await old.categorize("что-то", "expense") is None
        assert "AI_MODEL" in old.status
        await old.close()
    finally:
        await runner.cleanup()
    # Сервис недоступен вообще — тоже не падаем.
    down = AICategorizer(make_config("k", url, "m"), timeout=1, trust_env=False)  # type: ignore[arg-type]
    assert await down.categorize("что-то", "expense") is None
    await down.close()


async def test_startup_probe_reports_in_log(caplog: pytest.LogCaptureFixture) -> None:
    from finbot.ai import probe

    async def ok(request: web.Request) -> web.Response:
        return web.json_response({"choices": [{"message": {"content": "cafe"}}]})

    async def denied(request: web.Request) -> web.Response:
        return web.json_response({"error": "bad key"}, status=401)

    for handler, expected in ((ok, "Нейросеть отвечает"), (denied, "Нейросеть не работает: ключ не принят")):
        runner, url = await _fake_openai(handler)
        ai = AICategorizer(make_config("k", url, "m"), trust_env=False)  # type: ignore[arg-type]
        caplog.clear()
        with caplog.at_level("INFO", logger="finbot.ai"):
            await probe(ai)
        await ai.close()
        await runner.cleanup()
        assert expected in caplog.text, caplog.text
