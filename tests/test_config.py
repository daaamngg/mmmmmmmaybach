from __future__ import annotations

from pathlib import Path

import pytest

from finbot.config import ConfigError, load_config


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **values: str) -> Path:
    for key in ("BOT_TOKEN", "OWNER_ID", "TIMEZONE", "DATA_DIR", "PROXY"):
        # setenv+delenv: после теста monkeypatch вернёт окружение как было,
        # даже если load_dotenv что-то в него записал.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    return path


def test_valid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, BOT_TOKEN="123:abc", OWNER_ID="42", TIMEZONE="Europe/Moscow", DATA_DIR="mydata")
    cfg = load_config(env)
    assert cfg.bot_token == "123:abc" and cfg.owner_id == 42
    assert cfg.timezone is not None and cfg.data_dir.name == "mydata" and cfg.data_dir.is_absolute()
    assert cfg.db_path.name == "finbot.db" and cfg.proxy is None


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({}, "BOT_TOKEN"),
        ({"BOT_TOKEN": "nonsense"}, "BOT_TOKEN"),
        ({"BOT_TOKEN": "1:a", "OWNER_ID": "me"}, "OWNER_ID"),
        ({"BOT_TOKEN": "1:a", "TIMEZONE": "Mars/Base"}, "TIMEZONE"),
    ],
)
def test_bad_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: dict[str, str], message: str) -> None:
    env = _env(tmp_path, monkeypatch, **values)
    with pytest.raises(ConfigError, match=message):
        load_config(env)


def test_run_exits_with_code_2_on_bad_config(tmp_path: Path) -> None:
    """Неверная настройка → код 2: systemd не будет перезапускать бот по кругу."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "OWNER_ID", "TIMEZONE", "DATA_DIR", "PROXY")}
    env["DATA_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(root / "run.py")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "BOT_TOKEN" in result.stdout
