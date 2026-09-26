"""Версия без сервера (docs/ для GitHub Pages): совпадает с ботом и собрана из свежих файлов."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from finbot.db import Database, TxBlocked
from finbot.webapp import pages

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="нужен Node.js")
DAY = "2026-09-25"


def test_docs_are_up_to_date() -> None:
    """docs/ собирается из finbot/webapp/static и словарей бота: python -m finbot.webapp.pages."""
    for name, text in pages.site_files().items():
        path = ROOT / "docs" / name
        assert path.exists() and path.read_text(encoding="utf-8") == text, (
            f"docs/{name} устарел — выполни: python -m finbot.webapp.pages"
        )


def random_ops(seed: int, n: int = 60) -> list[dict[str, Any]]:
    rnd = random.Random(seed)
    ops: list[dict[str, Any]] = [{"op": "goal", "target": rnd.choice([50_000, 300_000, 1_000_000]), "weight": 1}]
    for _ in range(n):
        kind = rnd.choices(
            ["income", "expense", "goal", "deposit", "withdraw", "weight", "buy", "delete_goal", "delete_tx", "wallet"],
            weights=[20, 20, 4, 8, 5, 4, 2, 2, 8, 2],
        )[0]
        op: dict[str, Any] = {"op": kind, "k": rnd.randrange(1000)}
        if kind in ("income", "expense", "deposit", "withdraw"):
            op["amount"] = rnd.choice([100, 12_345, 50_000, 99_999, 250_000, 1_000_000])
        if kind == "income":
            op["pct"] = rnd.choice([0, 50, 80, 100])
        if kind == "goal":
            op["target"] = rnd.choice([10_000, 200_000, 3_000_000])
            op["weight"] = rnd.choice([0, 1, 2, 3])
        if kind == "deposit":
            op["src"] = rnd.choice(["wallet", "pool", "outside"])
        if kind == "weight":
            op["weight"] = rnd.choice([0, 1, 5])
        if kind == "delete_goal":
            op["dest"] = rnd.choice(["goals", "pool", "wallet"])
        if kind == "wallet":
            op["amount"] = rnd.choice([0, 7_700, 500_000])
        ops.append(op)
    return ops


async def run_python(db: Database, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Те же операции через базу бота. Цели и записи адресуются по порядку создания."""
    goals: list[int] = []
    txs: list[int] = []
    day = date.fromisoformat(DAY)
    for op in ops:
        kind, k = op["op"], op.get("k", 0)
        active = [g.id for g in await db.goals()]
        if kind == "goal":
            goals.append(await db.create_goal(f"g{len(goals)}", op["target"], op["weight"]))
        elif kind == "income":
            res = await db.add_income(op["amount"], category="salary", day=day, pct=op["pct"])
            txs.append(res.tx_id)
        elif kind == "expense":
            txs.append(await db.add_expense(op["amount"], category="food", day=day))
        elif kind in ("deposit", "withdraw", "weight", "buy", "delete_goal") and active:
            gid = active[k % len(active)]
            if kind == "deposit":
                res = await db.transfer(op["src"], "goal", op["amount"], dst_goal=gid)
                if res and res.tx_id:
                    txs.append(res.tx_id)
            elif kind == "withdraw":
                res = await db.transfer("goal", "wallet", op["amount"], src_goal=gid)
                if res and res.tx_id:
                    txs.append(res.tx_id)
            elif kind == "weight":
                await db.update_goal(gid, weight=op["weight"])
            elif kind == "buy":
                bought = await db.buy_goal(gid)
                if bought:
                    txs.append(bought[0])
            else:
                res = await db.delete_goal(gid, op["dest"])
                if res and res.tx_id:
                    txs.append(res.tx_id)
        elif kind == "delete_tx" and txs:
            i = k % len(txs)
            try:
                if await db.delete_tx(txs[i]) is not None:
                    txs.pop(i)
            except TxBlocked:
                pass
        elif kind == "wallet":
            tx_id = await db.set_wallet(op["amount"])
            if tx_id:
                txs.append(tx_id)
    b = await db.balances()
    all_goals = await db.goals(None)
    return {
        "wallet": b.wallet,
        "pool": b.pool,
        "goals": [[g.saved, g.status, g.weight] for g in all_goals],
        "txs": len(txs),
    }


JS_RUNNER = """
const C = require(process.argv[1]);
const ops = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const s = C.emptyState();
const goals = [], txs = [];
for (const op of ops) {
  const k = op.k || 0;
  const active = s.goals.filter((g) => g.status === 'active').map((g) => g.id);
  if (op.op === 'goal') goals.push(C.createGoal(s, 'g' + goals.length, op.target, op.weight).id);
  else if (op.op === 'income') txs.push(C.addIncome(s, {amount: op.amount, category: 'salary', day: '%DAY%', pct: op.pct}).tx.id);
  else if (op.op === 'expense') txs.push(C.addExpense(s, {amount: op.amount, category: 'food', day: '%DAY%'}).id);
  else if (['deposit', 'withdraw', 'weight', 'buy', 'delete_goal'].includes(op.op) && active.length) {
    const gid = active[k % active.length];
    let res = null;
    if (op.op === 'deposit') res = C.transfer(s, op.src, 'goal', op.amount, {dstGoal: gid});
    else if (op.op === 'withdraw') res = C.transfer(s, 'goal', 'wallet', op.amount, {srcGoal: gid});
    else if (op.op === 'weight') C.updateGoal(s, gid, {weight: op.weight});
    else if (op.op === 'buy') res = C.buyGoal(s, gid);
    else res = C.deleteGoal(s, gid, op.dest);
    if (res && res.tx) txs.push(res.tx.id);
  } else if (op.op === 'delete_tx' && txs.length) {
    const i = k % txs.length;
    try { if (C.deleteTx(s, txs[i])) txs.splice(i, 1); } catch (e) { if (!(e instanceof C.TxBlocked)) throw e; }
  } else if (op.op === 'wallet') {
    const tx = C.setWallet(s, op.amount);
    if (tx) txs.push(tx.id);
  }
}
const b = C.balances(s);
const all = C.goalViews(s, null, b).sort((a, c) => a.id - c.id);
console.log(JSON.stringify({wallet: b.wallet, pool: b.pool, goals: all.map((g) => [g.saved, g.status, g.weight]), txs: txs.length}));
"""


@needs_node
@pytest.mark.parametrize("seed", range(12))
async def test_browser_logic_matches_bot(db: Database, seed: int) -> None:
    ops = random_ops(seed)
    expected = await run_python(db, ops)
    result = subprocess.run(
        [NODE, "-e", JS_RUNNER.replace("%DAY%", DAY), str(ROOT / "docs" / "core.js")],
        input=json.dumps(ops),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert json.loads(result.stdout) == expected


@needs_node
def test_browser_unit_tests() -> None:
    """tests/js/*.test.js — логика и облачное хранилище версии без сервера."""
    files = sorted(str(f) for f in (ROOT / "tests" / "js").glob("*.test.js"))
    result = subprocess.run([NODE, "--test", *files], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
