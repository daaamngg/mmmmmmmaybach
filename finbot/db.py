"""Хранилище на SQLite.

Как устроены деньги:
  * ``tx`` — записи (доход, расход, перевод, корректировка);
  * ``moves`` — движения денег по «кошелькам»: ``wallet`` (на расходы),
    ``pool`` (свободные накопления) и ``goal`` (конкретная цель).

Балансы не хранятся, а всегда считаются как сумма движений. Поэтому отмена
любой записи (удаление tx каскадом удаляет её движения) мгновенно и точно
возвращает все балансы — рассинхрона не бывает.

Вся работа с базой идёт в одном отдельном потоке: каждый публичный метод —
одна атомарная транзакция. Двойное нажатие кнопки не может испортить данные,
а бот при этом не подвисает.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar

from . import clock
from .money import allocate_capped, pct_part

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    target      INTEGER NOT NULL CHECK (target > 0),
    weight      INTEGER NOT NULL DEFAULT 1 CHECK (weight >= 0),
    status      TEXT    NOT NULL DEFAULT 'active',  -- active | bought | deleted
    deadline    TEXT,
    created_at  INTEGER NOT NULL,
    closed_at   INTEGER,
    close_tx_id INTEGER                              -- запись, которой цель закрыта
);

CREATE TABLE IF NOT EXISTS goal_photos (
    id             INTEGER PRIMARY KEY,
    goal_id        INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    file_id        TEXT    NOT NULL,
    file_unique_id TEXT,
    local_path     TEXT,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_goal ON goal_photos(goal_id);

CREATE TABLE IF NOT EXISTS tx (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN ('income', 'expense', 'transfer', 'adjust')),
    amount     INTEGER NOT NULL CHECK (amount >= 0),
    category   TEXT,
    note       TEXT,
    day        TEXT    NOT NULL,                     -- YYYY-MM-DD, локальная дата
    created_at INTEGER NOT NULL,
    goal_pct   INTEGER                               -- для дохода: % в цели
);
CREATE INDEX IF NOT EXISTS idx_tx_day ON tx(day);
CREATE INDEX IF NOT EXISTS idx_tx_kind_day ON tx(kind, day);

CREATE TABLE IF NOT EXISTS moves (
    id      INTEGER PRIMARY KEY,
    tx_id   INTEGER NOT NULL REFERENCES tx(id) ON DELETE CASCADE,
    account TEXT    NOT NULL CHECK (account IN ('wallet', 'pool', 'goal')),
    goal_id INTEGER REFERENCES goals(id),
    amount  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moves_tx ON moves(tx_id);
CREATE INDEX IF NOT EXISTS idx_moves_goal ON moves(goal_id);
CREATE INDEX IF NOT EXISTS idx_moves_balance ON moves(account, goal_id, amount);

CREATE TABLE IF NOT EXISTS wishes (
    id         INTEGER PRIMARY KEY,
    title      TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending | thinking | reminded | resisted | bought
    created_at INTEGER NOT NULL,
    remind_at  INTEGER,
    decided_at INTEGER,
    tx_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wishes_status ON wishes(status, remind_at);
"""

# v2: бот запоминает, какие категории ты выбираешь для своих слов.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS learned (
    kind       TEXT    NOT NULL,                     -- income | expense
    key        TEXT    NOT NULL,                     -- «=фраза целиком» или «~главное слово»
    category   TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'user',      -- user | ai
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);
"""

MIGRATIONS = [SCHEMA_V1, SCHEMA_V2]
OPEN_WISH = ("pending", "thinking", "reminded")


# ─────────────────────────────── модели ───────────────────────────────


@dataclass(frozen=True)
class Settings:
    goal_pct: int = 80
    currency: str = "₽"
    harsh: int = 1
    morning_on: bool = True
    morning_time: str = "09:00"
    evening_on: bool = True
    evening_time: str = "21:00"
    owner_id: int | None = None
    last_morning: str = ""
    last_evening: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, str]) -> Settings:
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            v = raw[f.name]
            try:
                if isinstance(f.default, bool):
                    values[f.name] = v == "1"
                elif f.name == "owner_id":
                    values[f.name] = int(v) if v else None
                elif isinstance(f.default, int):
                    values[f.name] = int(v)
                else:
                    values[f.name] = v
            except ValueError:
                continue
        return cls(**values)

    @staticmethod
    def encode(value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        if value is None:
            return ""
        return str(value)


@dataclass(frozen=True)
class Goal:
    id: int
    title: str
    target: int
    weight: int
    status: str
    deadline: date | None
    created_at: int
    closed_at: int | None
    close_tx_id: int | None
    saved: int
    photos: int

    @property
    def reached(self) -> bool:
        return self.saved >= self.target

    @property
    def remaining(self) -> int:
        return max(0, self.target - self.saved)

    @property
    def progress(self) -> float:
        return max(0.0, self.saved / self.target) if self.target > 0 else 0.0

    @property
    def receives(self) -> bool:
        """Получает ли цель автоматические отчисления с доходов."""
        return self.status == "active" and self.weight > 0 and not self.reached


@dataclass(frozen=True)
class Photo:
    id: int
    goal_id: int
    file_id: str
    file_unique_id: str | None
    local_path: str | None


@dataclass(frozen=True)
class Move:
    account: str
    goal_id: int | None
    amount: int


@dataclass(frozen=True)
class Tx:
    id: int
    kind: str
    amount: int
    category: str | None
    note: str | None
    day: date
    created_at: int
    goal_pct: int | None
    moves: tuple[Move, ...] = ()

    @property
    def to_goals(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for m in self.moves:
            if m.account == "goal" and m.goal_id is not None:
                out[m.goal_id] = out.get(m.goal_id, 0) + m.amount
        return out

    @property
    def to_pool(self) -> int:
        return sum(m.amount for m in self.moves if m.account == "pool")

    @property
    def to_wallet(self) -> int:
        return sum(m.amount for m in self.moves if m.account == "wallet")


@dataclass(frozen=True)
class Balances:
    wallet: int  # на расходы
    pool: int  # свободные накопления (не привязаны к цели)
    goals: dict[int, int]  # накоплено по каждой цели (включая закрытые)
    in_goals: int  # сумма по активным целям

    @property
    def savings(self) -> int:
        return self.pool + self.in_goals

    @property
    def total(self) -> int:
        return self.wallet + self.savings


@dataclass(frozen=True)
class IncomeResult:
    tx_id: int
    to_goals: dict[int, int]
    to_pool: int
    to_wallet: int
    completed: tuple[int, ...]  # цели, которые этим доходом достигнуты


@dataclass(frozen=True)
class TransferResult:
    tx_id: int | None
    amount: int
    to_goals: dict[int, int]
    to_pool: int
    completed: tuple[int, ...]


@dataclass(frozen=True)
class PeriodSummary:
    income: int
    expense: int
    saved: int  # чистый поток в цели и накопления
    income_count: int
    expense_count: int
    by_category: tuple[tuple[str, int, int], ...]  # (категория, сумма, количество)
    goal_purchases: int
    spend_days: int  # дней, в которые были траты (без покупок целей)


@dataclass(frozen=True)
class Wish:
    id: int
    title: str
    amount: int
    status: str
    created_at: int
    remind_at: int | None
    decided_at: int | None
    tx_id: int | None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_WISH


@dataclass(frozen=True)
class WishStats:
    resisted: int
    resisted_sum: int
    bought: int
    bought_sum: int


class TxBlocked(Exception):
    """Запись нельзя отменить или изменить — в reason объяснение для пользователя."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TxLocked(TxBlocked):
    """Запись связана с уже закрытой целью."""

    def __init__(self, goal_title: str) -> None:
        super().__init__(
            f"Эта запись связана с закрытой целью «{goal_title}». Сначала верни цель (отмени её закрытие)."
        )
        self.goal_title = goal_title


class TxSpent(TxBlocked):
    """Отмена увела бы накопления в минус: деньги уже переведены или потрачены."""

    def __init__(self) -> None:
        super().__init__(
            "Нельзя: эти деньги уже ушли дальше (переведены или потрачены), и отмена увела бы баланс в минус."
        )


# ─────────────────────────────── база ───────────────────────────────

T = TypeVar("T")


def _now_ts() -> int:
    return int(clock.now().timestamp())


def _db(fn: Callable[..., T]) -> Callable[..., Any]:
    """Превращает синхронный метод в асинхронный, выполняемый в потоке базы."""

    @functools.wraps(fn)
    async def wrapper(self: Database, *args: Any, **kwargs: Any) -> T:
        return await self._call(fn, self, *args, **kwargs)

    return wrapper


_GOAL_SELECT = """
SELECT g.id, g.title, g.target, g.weight, g.status, g.deadline, g.created_at, g.closed_at,
       g.close_tx_id,
       COALESCE((SELECT SUM(m.amount) FROM moves m
                 WHERE m.account = 'goal' AND m.goal_id = g.id), 0) AS saved,
       (SELECT COUNT(*) FROM goal_photos p WHERE p.goal_id = g.id) AS photos
FROM goals g
"""
_GOAL_FIELDS = frozenset({"title", "target", "weight", "deadline"})


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finbot-db")
        self._conn: sqlite3.Connection | None = None
        # Настройки держим в памяти: читаются в каждом обработчике.
        self.settings = Settings()

    async def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    @property
    def c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        c = self.c
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    # ── открытие / закрытие ──

    @_db
    def open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        self._conn = conn
        self.settings = self._read_settings()

    async def close(self) -> None:
        if self._conn is not None:
            await self._call(self._close)
        self._executor.shutdown(wait=True)

    def _close(self) -> None:
        if self._conn is None:
            return
        with suppress(sqlite3.Error):
            self._conn.execute("PRAGMA optimize")
        self._conn.close()
        self._conn = None

    # ── настройки ──

    def _read_settings(self) -> Settings:
        raw = {r["key"]: r["value"] for r in self.c.execute("SELECT key, value FROM settings")}
        return Settings.from_raw(raw)

    @_db
    def update_settings(self, **changes: Any) -> Settings:
        new = replace(self.settings, **changes)
        with self._write() as c:
            c.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, Settings.encode(getattr(new, k))) for k in changes],
            )
        self.settings = new
        return new

    # ── внутренние помощники (только внутри потока базы) ──

    @staticmethod
    def _move(c: sqlite3.Connection, tx_id: int, account: str, goal_id: int | None, amount: int) -> None:
        if amount:
            c.execute(
                "INSERT INTO moves(tx_id, account, goal_id, amount) VALUES (?, ?, ?, ?)",
                (tx_id, account, goal_id, amount),
            )

    @staticmethod
    def _insert_tx(
        c: sqlite3.Connection,
        kind: str,
        amount: int,
        *,
        category: str | None = None,
        note: str | None = None,
        day: date | None = None,
        created_at: int | None = None,
        goal_pct: int | None = None,
    ) -> int:
        day = day or clock.today()
        ts = created_at if created_at is not None else _now_ts()
        cur = c.execute(
            "INSERT INTO tx(kind, amount, category, note, day, created_at, goal_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, amount, category, note, day.isoformat(), ts, goal_pct),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _goal_row(r: sqlite3.Row) -> Goal:
        return Goal(
            id=r["id"],
            title=r["title"],
            target=r["target"],
            weight=r["weight"],
            status=r["status"],
            deadline=date.fromisoformat(r["deadline"]) if r["deadline"] else None,
            created_at=r["created_at"],
            closed_at=r["closed_at"],
            close_tx_id=r["close_tx_id"],
            saved=r["saved"],
            photos=r["photos"],
        )

    def _get_goal(self, c: sqlite3.Connection, goal_id: int) -> Goal | None:
        r = c.execute(_GOAL_SELECT + " WHERE g.id = ?", (goal_id,)).fetchone()
        return self._goal_row(r) if r else None

    def _active_goals(self, c: sqlite3.Connection) -> list[Goal]:
        rows = c.execute(_GOAL_SELECT + " WHERE g.status = 'active' ORDER BY g.id")
        return [self._goal_row(r) for r in rows]

    def _plan(
        self, c: sqlite3.Connection, amount: int, exclude: set[int] | frozenset[int] = frozenset()
    ) -> tuple[dict[int, int], int, tuple[int, ...]]:
        """Как распределить сумму по активным целям: (по целям, остаток, достигнутые цели)."""
        if amount <= 0:
            return {}, 0, ()
        goals = [g for g in self._active_goals(c) if g.id not in exclude]
        alloc, left = allocate_capped(amount, [(g.id, g.weight, g.target - g.saved) for g in goals])
        alloc = {gid: a for gid, a in alloc.items() if a > 0}
        by_id = {g.id: g for g in goals}
        completed = tuple(
            gid for gid, a in alloc.items() if by_id[gid].saved < by_id[gid].target <= by_id[gid].saved + a
        )
        return alloc, left, completed

    def _apply_income(self, c: sqlite3.Connection, tx_id: int, amount: int, pct: int) -> IncomeResult:
        goals_part = pct_part(amount, pct)
        alloc, left, completed = self._plan(c, goals_part)
        for gid, a in alloc.items():
            self._move(c, tx_id, "goal", gid, a)
        self._move(c, tx_id, "pool", None, left)
        wallet_part = amount - goals_part
        self._move(c, tx_id, "wallet", None, wallet_part)
        return IncomeResult(tx_id, alloc, left, wallet_part, completed)

    @staticmethod
    def _tx_row(r: sqlite3.Row, moves: tuple[Move, ...] = ()) -> Tx:
        return Tx(
            id=r["id"],
            kind=r["kind"],
            amount=r["amount"],
            category=r["category"],
            note=r["note"],
            day=date.fromisoformat(r["day"]),
            created_at=r["created_at"],
            goal_pct=r["goal_pct"],
            moves=moves,
        )

    def _load_txs(self, c: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> list[Tx]:
        rows = c.execute(f"SELECT * FROM tx {where}", params).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        moves: dict[int, list[Move]] = {i: [] for i in ids}
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start : chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            for m in c.execute(
                f"SELECT tx_id, account, goal_id, amount FROM moves WHERE tx_id IN ({marks}) ORDER BY id",
                chunk,
            ):
                moves[m["tx_id"]].append(Move(m["account"], m["goal_id"], m["amount"]))
        return [self._tx_row(r, tuple(moves[r["id"]])) for r in rows]

    def _get_tx(self, c: sqlite3.Connection, tx_id: int) -> Tx | None:
        found = self._load_txs(c, "WHERE id = ?", (tx_id,))
        return found[0] if found else None

    def _check_unlocked(self, c: sqlite3.Connection, tx: Tx) -> None:
        """Запрещает менять запись, если она задевает закрытую цель (кроме записи-закрытия)."""
        for gid in {m.goal_id for m in tx.moves if m.goal_id is not None}:
            r = c.execute("SELECT title, status, close_tx_id FROM goals WHERE id = ?", (gid,)).fetchone()
            if r and r["status"] != "active" and r["close_tx_id"] != tx.id:
                raise TxLocked(r["title"])

    def _no_new_debt(self, c: sqlite3.Connection, before: Balances, *, wallet: bool) -> None:
        """Проверка после изменения: накопления (и кошелёк, если wallet) не ушли в новый минус."""
        after = self._balances(c)
        if after.pool < 0 and after.pool < before.pool:
            raise TxSpent
        for gid, value in after.goals.items():
            if value < 0 and value < before.goals.get(gid, 0):
                raise TxSpent
        if wallet and after.wallet < 0 and after.wallet < before.wallet:
            raise TxSpent

    def _balances(self, c: sqlite3.Connection) -> Balances:
        wallet = pool = 0
        goals: dict[int, int] = {}
        for r in c.execute("SELECT account, goal_id, SUM(amount) AS s FROM moves GROUP BY account, goal_id"):
            if r["account"] == "wallet":
                wallet += r["s"]
            elif r["account"] == "pool":
                pool += r["s"]
            elif r["goal_id"] is not None:
                goals[r["goal_id"]] = r["s"]
        active = {r[0] for r in c.execute("SELECT id FROM goals WHERE status = 'active'")}
        in_goals = sum(v for gid, v in goals.items() if gid in active)
        return Balances(wallet, pool, goals, in_goals)

    # ── доходы и расходы ──

    @_db
    def add_income(
        self,
        amount: int,
        *,
        category: str,
        note: str | None = None,
        day: date | None = None,
        created_at: int | None = None,
        pct: int | None = None,
    ) -> IncomeResult:
        pct = self.settings.goal_pct if pct is None else pct
        with self._write() as c:
            tx_id = self._insert_tx(
                c, "income", amount, category=category, note=note, day=day, created_at=created_at, goal_pct=pct
            )
            return self._apply_income(c, tx_id, amount, pct)

    @_db
    def add_expense(
        self,
        amount: int,
        *,
        category: str,
        note: str | None = None,
        day: date | None = None,
        created_at: int | None = None,
    ) -> int:
        with self._write() as c:
            tx_id = self._insert_tx(c, "expense", amount, category=category, note=note, day=day, created_at=created_at)
            self._move(c, tx_id, "wallet", None, -amount)
            return tx_id

    @_db
    def get_tx(self, tx_id: int) -> Tx | None:
        return self._get_tx(self.c, tx_id)

    @_db
    def delete_tx(self, tx_id: int) -> Tx | None:
        """Удаляет запись и все её движения. Закрытая этой записью цель снова становится активной."""
        with self._write() as c:
            tx = self._get_tx(c, tx_id)
            if tx is None:
                return None
            self._check_unlocked(c, tx)
            before = self._balances(c)
            c.execute("DELETE FROM tx WHERE id = ?", (tx_id,))
            # Отмена перевода не должна «воскрешать» деньги, которые уже ушли дальше.
            self._no_new_debt(c, before, wallet=tx.kind in ("transfer", "adjust"))
            c.execute(
                "UPDATE goals SET status = 'active', closed_at = NULL, close_tx_id = NULL WHERE close_tx_id = ?",
                (tx_id,),
            )
            # Отменили покупку из «Хочу купить» — желание снова открыто.
            c.execute(
                "UPDATE wishes SET "
                "status = CASE WHEN status = 'bought' THEN 'pending' ELSE status END, "
                "decided_at = CASE WHEN status = 'bought' THEN NULL ELSE decided_at END, "
                "tx_id = NULL WHERE tx_id = ?",
                (tx_id,),
            )
            return tx

    @_db
    def flip_tx(self, tx_id: int, category: str) -> tuple[int, tuple[int, ...]] | None:
        """Расход ↔ доход. Возвращает (id новой записи, достигнутые цели)."""
        with self._write() as c:
            tx = self._get_tx(c, tx_id)
            if tx is None or tx.kind not in ("income", "expense") or tx.category == "goal":
                return None
            self._check_unlocked(c, tx)
            before = self._balances(c)
            c.execute("DELETE FROM tx WHERE id = ?", (tx_id,))
            common = {"category": category, "note": tx.note, "day": tx.day, "created_at": tx.created_at}
            if tx.kind == "expense":
                pct = self.settings.goal_pct
                new_id = self._insert_tx(c, "income", tx.amount, goal_pct=pct, **common)
                return new_id, self._apply_income(c, new_id, tx.amount, pct).completed
            new_id = self._insert_tx(c, "expense", tx.amount, **common)
            self._move(c, new_id, "wallet", None, -tx.amount)
            self._no_new_debt(c, before, wallet=False)
            return new_id, ()

    @_db
    def set_category(self, tx_id: int, category: str) -> bool:
        with self._write() as c:
            cur = c.execute(
                "UPDATE tx SET category = ? WHERE id = ? AND kind IN ('income', 'expense') "
                "AND COALESCE(category, '') != 'goal'",
                (category, tx_id),
            )
            return cur.rowcount > 0

    @_db
    def reapply_income(self, tx_id: int, pct: int) -> IncomeResult | None:
        """Пересчитывает распределение дохода с другим процентом в цели."""
        with self._write() as c:
            tx = self._get_tx(c, tx_id)
            if tx is None or tx.kind != "income":
                return None
            self._check_unlocked(c, tx)
            before = self._balances(c)
            c.execute("DELETE FROM moves WHERE tx_id = ?", (tx_id,))
            c.execute("UPDATE tx SET goal_pct = ? WHERE id = ?", (pct, tx_id))
            result = self._apply_income(c, tx_id, tx.amount, pct)
            self._no_new_debt(c, before, wallet=False)
            return result

    # ── балансы и переводы ──

    @_db
    def balances(self) -> Balances:
        return self._balances(self.c)

    def _available(self, c: sqlite3.Connection, account: str, goal_id: int | None) -> int | None:
        """Сколько денег в кошельке (None — для внешнего источника)."""
        if account == "outside":
            return None
        b = self._balances(c)
        if account == "wallet":
            return b.wallet
        if account == "pool":
            return b.pool
        return b.goals.get(goal_id or 0, 0)

    def _transfer(
        self,
        c: sqlite3.Connection,
        src: str,
        dst: str,
        amount: int,
        *,
        src_goal: int | None = None,
        dst_goal: int | None = None,
        note: str | None = None,
        strict: bool = False,
    ) -> TransferResult | None:
        if amount <= 0:
            return None
        if strict:
            available = self._available(c, src, src_goal)
            if available is not None and amount > available:
                return None
        to_goals: dict[int, int] = {}
        to_pool = 0
        completed: tuple[int, ...] = ()
        moved = amount
        if dst == "goals":
            exclude = {src_goal} if src == "goal" and src_goal is not None else set()
            to_goals, left, completed = self._plan(c, amount, exclude)
            if src == "pool":  # из свободных забираем только то, что влезло в цели
                moved, left = amount - left, 0
            to_pool = left
        elif dst == "goal":
            g = self._get_goal(c, dst_goal) if dst_goal is not None else None
            if g is None or g.status != "active":
                return None
            to_goals = {g.id: amount}
            if g.saved < g.target <= g.saved + amount:
                completed = (g.id,)
        elif dst == "pool":
            to_pool = amount
        elif dst != "wallet":
            raise ValueError(f"unknown destination {dst}")
        if moved <= 0:
            return None
        kind = "adjust" if src == "outside" else "transfer"
        tx_id = self._insert_tx(c, kind, moved, note=note)
        if src != "outside":
            self._move(c, tx_id, src, src_goal if src == "goal" else None, -moved)
        for gid, a in to_goals.items():
            self._move(c, tx_id, "goal", gid, a)
        self._move(c, tx_id, "pool", None, to_pool)
        if dst == "wallet":
            self._move(c, tx_id, "wallet", None, moved)
        return TransferResult(tx_id, moved, to_goals, to_pool, completed)

    @_db
    def transfer(
        self,
        src: str,
        dst: str,
        amount: int,
        *,
        src_goal: int | None = None,
        dst_goal: int | None = None,
        note: str | None = None,
        strict: bool = True,
    ) -> TransferResult | None:
        """Перевод между кошельками.

        src: wallet | pool | goal | outside (деньги, отложенные ещё до бота);
        dst: wallet | pool | goal | goals (по долям всех активных целей).
        strict — не переводить больше, чем есть в источнике (проверка внутри транзакции,
        поэтому двойное нажатие кнопки не уведёт кошелёк в минус).
        """
        with self._write() as c:
            return self._transfer(c, src, dst, amount, src_goal=src_goal, dst_goal=dst_goal, note=note, strict=strict)

    @_db
    def set_wallet(self, value: int) -> int | None:
        """Выставляет точный остаток «на расходы» корректировкой."""
        with self._write() as c:
            delta = value - self._balances(c).wallet
            if delta == 0:
                return None
            tx_id = self._insert_tx(c, "adjust", abs(delta), note="Корректировка остатка на расходы")
            self._move(c, tx_id, "wallet", None, delta)
            return tx_id

    # ── цели ──

    @_db
    def create_goal(self, title: str, target: int, weight: int = 1) -> int:
        with self._write() as c:
            cur = c.execute(
                "INSERT INTO goals(title, target, weight, created_at) VALUES (?, ?, ?, ?)",
                (title, target, weight, _now_ts()),
            )
            return int(cur.lastrowid)

    @_db
    def get_goal(self, goal_id: int) -> Goal | None:
        return self._get_goal(self.c, goal_id)

    @_db
    def goals(self, status: str | None = "active") -> list[Goal]:
        """Цели с нужным статусом (None — все, включая закрытые)."""
        if status is None:
            rows = self.c.execute(_GOAL_SELECT + " ORDER BY g.id")
        else:
            order = "g.id" if status == "active" else "g.closed_at DESC, g.id DESC"
            rows = self.c.execute(_GOAL_SELECT + f" WHERE g.status = ? ORDER BY {order}", (status,))
        return [self._goal_row(r) for r in rows]

    @_db
    def update_goal(self, goal_id: int, **values: Any) -> Goal | None:
        bad = set(values) - _GOAL_FIELDS
        if bad:
            raise ValueError(f"unknown goal fields: {bad}")
        if isinstance(values.get("deadline"), date):
            values["deadline"] = values["deadline"].isoformat()
        with self._write() as c:
            sets = ", ".join(f"{k} = ?" for k in values)
            c.execute(f"UPDATE goals SET {sets} WHERE id = ? AND status = 'active'", (*values.values(), goal_id))
        return self._get_goal(self.c, goal_id)

    @_db
    def buy_goal(self, goal_id: int) -> tuple[int, int] | None:
        """Цель куплена: накопленное списывается расходом «Покупка цели». → (tx_id, сумма)."""
        with self._write() as c:
            g = self._get_goal(c, goal_id)
            if g is None or g.status != "active" or g.saved <= 0:
                return None
            spent = min(g.saved, g.target)
            tx_id = self._insert_tx(c, "expense", spent, category="goal", note=g.title)
            self._move(c, tx_id, "goal", goal_id, -g.saved)
            self._move(c, tx_id, "pool", None, g.saved - spent)  # лишнее — в свободные
            c.execute(
                "UPDATE goals SET status = 'bought', closed_at = ?, close_tx_id = ? WHERE id = ?",
                (_now_ts(), tx_id, goal_id),
            )
            return tx_id, spent

    @_db
    def delete_goal(self, goal_id: int, dest: str) -> TransferResult | None:
        """Удаляет цель, а её деньги переводит: goals | pool | wallet."""
        with self._write() as c:
            g = self._get_goal(c, goal_id)
            if g is None or g.status != "active":
                return None
            result = TransferResult(None, 0, {}, 0, ())
            if g.saved != 0:
                amount = g.saved
                if amount < 0:
                    dest = "wallet"
                to_goals: dict[int, int] = {}
                to_pool = 0
                completed: tuple[int, ...] = ()
                if dest == "goals":
                    to_goals, to_pool, completed = self._plan(c, amount, {goal_id})
                elif dest == "pool":
                    to_pool = amount
                tx_id = self._insert_tx(c, "transfer", abs(amount), note=f"Удаление цели «{g.title}»")
                self._move(c, tx_id, "goal", goal_id, -amount)
                for gid, a in to_goals.items():
                    self._move(c, tx_id, "goal", gid, a)
                self._move(c, tx_id, "pool", None, to_pool)
                if dest == "wallet":
                    self._move(c, tx_id, "wallet", None, amount)
                result = TransferResult(tx_id, amount, to_goals, to_pool, completed)
            c.execute(
                "UPDATE goals SET status = 'deleted', closed_at = ?, close_tx_id = ? WHERE id = ?",
                (_now_ts(), result.tx_id, goal_id),
            )
            return result

    # ── фото целей ──

    @_db
    def add_photo(self, goal_id: int, file_id: str, file_unique_id: str | None = None) -> int | None:
        with self._write() as c:
            if file_unique_id:
                dup = c.execute(
                    "SELECT id FROM goal_photos WHERE goal_id = ? AND file_unique_id = ?",
                    (goal_id, file_unique_id),
                ).fetchone()
                if dup:
                    return None
            cur = c.execute(
                "INSERT INTO goal_photos(goal_id, file_id, file_unique_id, created_at) VALUES (?, ?, ?, ?)",
                (goal_id, file_id, file_unique_id, _now_ts()),
            )
            return int(cur.lastrowid)

    @_db
    def photos(self, goal_id: int) -> list[Photo]:
        rows = self.c.execute(
            "SELECT id, goal_id, file_id, file_unique_id, local_path FROM goal_photos WHERE goal_id = ? ORDER BY id",
            (goal_id,),
        )
        return [Photo(r["id"], r["goal_id"], r["file_id"], r["file_unique_id"], r["local_path"]) for r in rows]

    @_db
    def get_photo(self, photo_id: int) -> Photo | None:
        r = self.c.execute(
            "SELECT id, goal_id, file_id, file_unique_id, local_path FROM goal_photos WHERE id = ?", (photo_id,)
        ).fetchone()
        return Photo(r["id"], r["goal_id"], r["file_id"], r["file_unique_id"], r["local_path"]) if r else None

    @_db
    def update_photo(self, photo_id: int, *, file_id: str | None = None, local_path: str | None = None) -> None:
        with self._write() as c:
            if file_id is not None:
                c.execute("UPDATE goal_photos SET file_id = ? WHERE id = ?", (file_id, photo_id))
            if local_path is not None:
                c.execute("UPDATE goal_photos SET local_path = ? WHERE id = ?", (local_path, photo_id))

    @_db
    def delete_photo(self, photo_id: int) -> Photo | None:
        with self._write() as c:
            r = c.execute(
                "SELECT id, goal_id, file_id, file_unique_id, local_path FROM goal_photos WHERE id = ?",
                (photo_id,),
            ).fetchone()
            if r is None:
                return None
            c.execute("DELETE FROM goal_photos WHERE id = ?", (photo_id,))
            return Photo(r["id"], r["goal_id"], r["file_id"], r["file_unique_id"], r["local_path"])

    # ── календарь и статистика ──

    @_db
    def day_totals(self, first: date, last: date) -> dict[date, tuple[int, int]]:
        """{день: (доход, расход)} за период."""
        rows = self.c.execute(
            "SELECT day, SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END) AS inc, "
            "SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END) AS exp "
            "FROM tx WHERE day BETWEEN ? AND ? AND kind IN ('income', 'expense') GROUP BY day",
            (first.isoformat(), last.isoformat()),
        )
        return {date.fromisoformat(r["day"]): (r["inc"], r["exp"]) for r in rows}

    @_db
    def summary(self, first: date, last: date) -> PeriodSummary:
        c = self.c
        a, b = first.isoformat(), last.isoformat()
        income = expense = n_inc = n_exp = 0
        for r in c.execute(
            "SELECT kind, COUNT(*) AS n, SUM(amount) AS s FROM tx "
            "WHERE day BETWEEN ? AND ? AND kind IN ('income', 'expense') GROUP BY kind",
            (a, b),
        ):
            if r["kind"] == "income":
                income, n_inc = r["s"], r["n"]
            else:
                expense, n_exp = r["s"], r["n"]
        cats = tuple(
            (r["category"] or "other", r["s"], r["n"])
            for r in c.execute(
                "SELECT category, SUM(amount) AS s, COUNT(*) AS n FROM tx "
                "WHERE kind = 'expense' AND day BETWEEN ? AND ? GROUP BY category ORDER BY s DESC",
                (a, b),
            )
        )
        saved = c.execute(
            "SELECT COALESCE(SUM(m.amount), 0) FROM moves m JOIN tx t ON t.id = m.tx_id "
            "WHERE t.kind IN ('income', 'transfer') AND m.account IN ('goal', 'pool') "
            "AND t.day BETWEEN ? AND ?",
            (a, b),
        ).fetchone()[0]
        spend_days = c.execute(
            "SELECT COUNT(DISTINCT day) FROM tx WHERE kind = 'expense' "
            "AND COALESCE(category, '') != 'goal' AND day BETWEEN ? AND ?",
            (a, b),
        ).fetchone()[0]
        goal_purchases = sum(s for cat, s, _ in cats if cat == "goal")
        return PeriodSummary(income, expense, saved, n_inc, n_exp, cats, goal_purchases, spend_days)

    @_db
    def day_txs(self, day: date) -> list[Tx]:
        return self._load_txs(
            self.c,
            "WHERE day = ? AND kind IN ('income', 'expense') ORDER BY created_at, id",
            (day.isoformat(),),
        )

    @_db
    def recent_txs(self, limit: int = 15) -> list[Tx]:
        return self._load_txs(self.c, "ORDER BY id DESC LIMIT ?", (limit,))

    @_db
    def all_txs(self) -> list[Tx]:
        return self._load_txs(self.c, "ORDER BY day, created_at, id", ())

    @_db
    def category_total(self, category: str, first: date, last: date) -> tuple[int, int]:
        r = self.c.execute(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM tx "
            "WHERE kind = 'expense' AND category = ? AND day BETWEEN ? AND ?",
            (category, first.isoformat(), last.isoformat()),
        ).fetchone()
        return r[0], r[1]

    @_db
    def first_day(self) -> date | None:
        r = self.c.execute("SELECT MIN(day) FROM tx WHERE kind IN ('income', 'expense')").fetchone()
        return date.fromisoformat(r[0]) if r and r[0] else None

    @_db
    def last_expense_day(self, categories: tuple[str, ...]) -> date | None:
        if not categories:
            return None
        marks = ",".join("?" * len(categories))
        r = self.c.execute(
            f"SELECT MAX(day) FROM tx WHERE kind = 'expense' AND category IN ({marks})", categories
        ).fetchone()
        return date.fromisoformat(r[0]) if r and r[0] else None

    @_db
    def daily_rates(self, days: int = 30) -> tuple[float, float]:
        """Средний доход в день и средние отложения в цели в день за последние N дней."""
        since = (clock.today() - timedelta(days=days - 1)).isoformat()
        income = self.c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM tx WHERE kind = 'income' AND day >= ?", (since,)
        ).fetchone()[0]
        saved = self.c.execute(
            "SELECT COALESCE(SUM(m.amount), 0) FROM moves m JOIN tx t ON t.id = m.tx_id "
            "WHERE t.kind IN ('income', 'transfer') AND m.account IN ('goal', 'pool') AND t.day >= ?",
            (since,),
        ).fetchone()[0]
        return income / days, max(0, saved) / days

    @_db
    def count_tx(self) -> int:
        return self.c.execute("SELECT COUNT(*) FROM tx").fetchone()[0]

    @_db
    def set_category_if(self, tx_id: int, expected: str, category: str) -> bool:
        """Меняет категорию, только если она всё ещё expected (ты не успел выбрать сам)."""
        with self._write() as c:
            cur = c.execute("UPDATE tx SET category = ? WHERE id = ? AND category = ?", (category, tx_id, expected))
            return cur.rowcount > 0

    # ── выученные категории ──

    @_db
    def learn(self, kind: str, keys: list[str], category: str, source: str = "user") -> None:
        """Запоминает категорию для слов. Выбор пользователя всегда главнее догадки нейросети."""
        guard = "" if source == "user" else " WHERE learned.source != 'user'"
        with self._write() as c:
            c.executemany(
                "INSERT INTO learned(kind, key, category, source, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET category = excluded.category, "
                "source = excluded.source, updated_at = excluded.updated_at" + guard,
                [(kind, key, category, source, _now_ts()) for key in keys],
            )

    @_db
    def recall(self, kind: str, keys: list[str]) -> str | None:
        """Категория по первому выученному ключу (фраза целиком важнее отдельного слова)."""
        for key in keys:
            r = self.c.execute("SELECT category FROM learned WHERE kind = ? AND key = ?", (kind, key)).fetchone()
            if r:
                return str(r[0])
        return None

    @_db
    def learned_count(self) -> int:
        return self.c.execute("SELECT COUNT(*) FROM learned").fetchone()[0]

    # ── «Хочу купить» ──

    @staticmethod
    def _wish_row(r: sqlite3.Row) -> Wish:
        return Wish(
            r["id"], r["title"], r["amount"], r["status"], r["created_at"], r["remind_at"], r["decided_at"], r["tx_id"]
        )

    def _get_wish(self, c: sqlite3.Connection, wish_id: int) -> Wish | None:
        r = c.execute("SELECT * FROM wishes WHERE id = ?", (wish_id,)).fetchone()
        return self._wish_row(r) if r else None

    @_db
    def add_wish(self, title: str, amount: int) -> int:
        with self._write() as c:
            cur = c.execute(
                "INSERT INTO wishes(title, amount, created_at) VALUES (?, ?, ?)", (title, amount, _now_ts())
            )
            return int(cur.lastrowid)

    @_db
    def get_wish(self, wish_id: int) -> Wish | None:
        return self._get_wish(self.c, wish_id)

    @_db
    def postpone_wish(self, wish_id: int, remind_at: int) -> Wish | None:
        with self._write() as c:
            cur = c.execute(
                "UPDATE wishes SET status = 'thinking', remind_at = ? "
                "WHERE id = ? AND status IN ('pending', 'thinking', 'reminded')",
                (remind_at, wish_id),
            )
            return self._get_wish(c, wish_id) if cur.rowcount else None

    @_db
    def resist_wish(self, wish_id: int) -> Wish | None:
        with self._write() as c:
            cur = c.execute(
                "UPDATE wishes SET status = 'resisted', decided_at = ? "
                "WHERE id = ? AND status IN ('pending', 'thinking', 'reminded')",
                (_now_ts(), wish_id),
            )
            return self._get_wish(c, wish_id) if cur.rowcount else None

    @_db
    def buy_wish(self, wish_id: int, category: str) -> Wish | None:
        """Сдался и купил: создаёт расход и закрывает желание."""
        with self._write() as c:
            w = self._get_wish(c, wish_id)
            if w is None or not w.is_open:
                return None
            tx_id = self._insert_tx(c, "expense", w.amount, category=category, note=w.title)
            self._move(c, tx_id, "wallet", None, -w.amount)
            c.execute(
                "UPDATE wishes SET status = 'bought', decided_at = ?, tx_id = ? WHERE id = ?",
                (_now_ts(), tx_id, wish_id),
            )
            return self._get_wish(c, wish_id)

    @_db
    def save_wish(self, wish_id: int) -> TransferResult | None:
        """Устоял — и отправил эти деньги в цели (один раз на желание)."""
        with self._write() as c:
            w = self._get_wish(c, wish_id)
            if w is None or w.status != "resisted" or w.tx_id is not None:
                return None
            wallet = self._balances(c).wallet
            res = self._transfer(c, "wallet", "goals", min(w.amount, wallet), note=f"Не купил «{w.title}»")
            if res is not None:
                c.execute("UPDATE wishes SET tx_id = ? WHERE id = ?", (res.tx_id, wish_id))
            return res

    @_db
    def due_wishes(self, now_ts: int) -> list[Wish]:
        rows = self.c.execute(
            "SELECT * FROM wishes WHERE status = 'thinking' AND remind_at <= ? ORDER BY remind_at", (now_ts,)
        )
        return [self._wish_row(r) for r in rows]

    @_db
    def mark_reminded(self, wish_id: int) -> None:
        with self._write() as c:
            c.execute("UPDATE wishes SET status = 'reminded' WHERE id = ? AND status = 'thinking'", (wish_id,))

    @_db
    def wish_stats(self, since_ts: int = 0, until_ts: int | None = None) -> WishStats:
        r = self.c.execute(
            "SELECT "
            "COALESCE(SUM(status = 'resisted'), 0), "
            "COALESCE(SUM(CASE WHEN status = 'resisted' THEN amount END), 0), "
            "COALESCE(SUM(status = 'bought'), 0), "
            "COALESCE(SUM(CASE WHEN status = 'bought' THEN amount END), 0) "
            "FROM wishes WHERE COALESCE(decided_at, 0) >= ? AND COALESCE(decided_at, 0) < ?",
            (since_ts, until_ts if until_ts is not None else 2**62),
        ).fetchone()
        return WishStats(r[0], r[1], r[2], r[3])

    # ── обслуживание ──

    @_db
    def backup_to(self, dest: str) -> None:
        target = sqlite3.connect(dest)
        try:
            self.c.backup(target)
        finally:
            target.close()

    @_db
    def wipe(self) -> None:
        """Удаляет все данные, кроме привязки владельца."""
        with self._write() as c:
            for table in ("moves", "tx", "goal_photos", "goals", "wishes", "learned"):
                c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM settings WHERE key != 'owner_id'")
        self.settings = Settings(owner_id=self.settings.owner_id)
