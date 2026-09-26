"""Версия мини-приложения без сервера — для GitHub Pages.

Та же страница, что отдаёт бот, но данные хранятся в облаке Telegram (CloudStorage),
а вся логика денег работает прямо в приложении (static/core.js + static/local.js).
Словари категорий и фразы мотивации берутся отсюда, из Python, — обе версии совпадают.

Собрать папку docs/:  python -m finbot.webapp.pages
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import categories, classify, motivation

STATIC_DIR = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
# Что копируется в docs/ как есть (index.html для Pages собирается отдельно).
ASSETS = ("app.css", "app.js", "core.js", "local.js")


def _category(c: categories.Category) -> dict[str, Any]:
    return {"key": c.key, "emoji": c.emoji, "name": c.name, "kind": c.kind, "tier": c.tier}


def export_data() -> dict[str, Any]:
    """Всё, что нужно логике в браузере, в виде JSON."""
    expense_tokens, expense_phrases = categories._EXPENSE_INDEX
    income_tokens, income_phrases = categories._INCOME_INDEX
    return {
        "expense": [_category(c) for c in categories.EXPENSE],
        "income": [_category(c) for c in categories.INCOME],
        "goal_purchase": _category(categories.GOAL_PURCHASE),
        "default_expense": categories.DEFAULT_EXPENSE,
        "default_income": categories.DEFAULT_INCOME,
        "impulse": list(categories.IMPULSE_KEYS),
        # Уже отсортированы, как в Python: длинные слова раньше коротких.
        "index": {
            "expense": {"tokens": expense_tokens, "phrases": expense_phrases},
            "income": {"tokens": income_tokens, "phrases": income_phrases},
        },
        "stopwords": sorted(classify.STOPWORDS),
        "quotes": {"1": motivation._Q1, "2": motivation._Q2},
    }


def data_js() -> str:
    payload = json.dumps(export_data(), ensure_ascii=False, separators=(",", ":"))
    return (
        "/* Создано командой python -m finbot.webapp.pages — не редактируй вручную. */\n"
        "(function (root, data) {\n"
        "  if (typeof module === 'object' && module.exports) module.exports = data;\n"
        "  else root.FinData = data;\n"
        f"}})(typeof globalThis !== 'undefined' ? globalThis : this, {payload});\n"
    )


def _version(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update(files[name].encode())
    return digest.hexdigest()[:12]


def site_files() -> dict[str, str]:
    """Содержимое папки docs/: {имя файла: текст}."""
    files = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in ASSETS}
    files["data.js"] = data_js()
    version = _version(files)
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Пути относительные (сайт живёт в подпапке github.io/<репозиторий>/), скрипты логики — перед app.js.
    index = index.replace('href="/static/app.css?v={{v}}"', f'href="app.css?v={version}"')
    index = index.replace(
        '<script src="/static/app.js?v={{v}}" defer></script>',
        "\n  ".join(
            f'<script src="{name}?v={version}" defer></script>' for name in ("data.js", "core.js", "local.js", "app.js")
        ),
    )
    # Внешних запросов у страницы нет: только свои файлы и мост Telegram.
    csp = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'"
    )
    index = index.replace(
        '<meta name="color-scheme" content="light dark">',
        f'<meta name="color-scheme" content="light dark">\n  <meta http-equiv="Content-Security-Policy" content="{csp}">',
    )
    assert "{{v}}" not in index and "/static/" not in index, "index.html изменился — обнови pages.py"
    files["index.html"] = index
    files[".nojekyll"] = ""  # GitHub Pages отдаёт файлы как есть
    return files


def build(out_dir: Path = DOCS_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in site_files().items():
        path = out_dir / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)
    return written


if __name__ == "__main__":
    changed = build()
    print("docs/ обновлена:", ", ".join(p.name for p in changed) if changed else "без изменений")
