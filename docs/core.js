/* Логика денег для версии без сервера — то же, что делает бот на Python
 * (finbot/db.py, money.py, reports.py, classify.py, motivation.py).
 *
 * Суммы — целые копейки. Балансы не хранятся, а считаются из «движений» записей,
 * поэтому удаление любой записи точно восстанавливает все суммы.
 * Словари категорий и фразы берутся из data.js (его собирает finbot/webapp/pages.py).
 */
'use strict';

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./data.js'));
  else root.FinCore = factory(root.FinData);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (DATA) {
  const MAX_AMOUNT = 1e13; // 100 млрд — защита от опечаток
  const MAX_WEIGHT = 10;

  // ═══════════════════ даты (строки YYYY-MM-DD) ═══════════════════

  function pad(n) {
    return String(n).padStart(2, '0');
  }

  function isoOf(y, m, d) {
    return `${y}-${pad(m)}-${pad(d)}`;
  }

  function today(now) {
    const d = now || new Date();
    return isoOf(d.getFullYear(), d.getMonth() + 1, d.getDate());
  }

  function nowTs() {
    return Math.floor(Date.now() / 1000);
  }

  function parseIso(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    if (!m) return null;
    const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
    const dt = new Date(Date.UTC(y, mo - 1, d));
    return dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d ? dt : null;
  }

  function addDays(iso, n) {
    const dt = parseIso(iso);
    dt.setUTCDate(dt.getUTCDate() + n);
    return dt.toISOString().slice(0, 10);
  }

  function daysBetween(a, b) {
    return Math.round((parseIso(b) - parseIso(a)) / 86400000);
  }

  function daysInMonth(y, m) {
    return new Date(Date.UTC(y, m, 0)).getUTCDate();
  }

  function monthBounds(y, m) {
    return [isoOf(y, m, 1), isoOf(y, m, daysInMonth(y, m))];
  }

  function shiftMonth(y, m, delta) {
    const idx = y * 12 + (m - 1) + delta;
    return [Math.floor(idx / 12), (idx % 12) + 1];
  }

  // ═══════════════════ суммы ═══════════════════

  const SEP = "[ \\u00a0\\u202f.,']";
  const NUM = `(?:\\d{1,3}(?:${SEP}\\d{3})+(?!\\d)|\\d+)(?:[.,]\\d{1,2}(?!\\d))?`;
  const SUF = '(?:кк|kk|млн\\.?|миллион(?:а|ов)?|лям(?:а|ов)?|тыс(?:\\.|яч[аи]?)?|тыщ[аи]?|к|k|т)';
  const CUR =
    '(?:₽|руб(?:\\.|лей|ля|ль)?|р\\.?|\\$|usd|долл(?:\\.|ар(?:ов|а)?)?|€|eur|евро|₴|грн\\.?|₸|тг|тенге|сом|br|byn)';
  const AMOUNT_RE = new RegExp(`^\\s*\\+?\\s*(${NUM})\\s?(${SUF})?\\s?(?:${CUR})?\\s*$`, 'iu');
  const NUM_PARTS_RE = /^(\d+(?:[.,]\d{3})*)(?:[.,](\d{1,2}))?$/;

  function multiplier(suffix) {
    if (!suffix) return 1;
    const s = suffix.toLowerCase().replace(/\.+$/, '');
    if (s === 'кк' || s === 'kk' || s === 'млн' || s.startsWith('миллион') || s.startsWith('лям')) return 1e6;
    return 1000;
  }

  function toMinor(num, suffix) {
    const m = NUM_PARTS_RE.exec(num.replace(/[   ']/g, ''));
    if (!m) return null;
    const whole = m[1].replace(/[.,]/g, '');
    if (whole.length > 14) return null;
    const frac = m[2] || '0';
    const kopecks = Number(whole) * 100 + (frac.length === 1 ? Number(frac) * 10 : Number(frac));
    const minor = kopecks * multiplier(suffix);
    return minor > 0 && minor <= MAX_AMOUNT ? minor : null;
  }

  /** «2000», «2 000», «1,5к», «150к», «2кк», «500 руб» → копейки. null — если не сумма. */
  function parseAmount(text) {
    const m = AMOUNT_RE.exec(String(text || ''));
    return m ? toMinor(m[1], m[2]) : null;
  }

  /** Сколько из суммы уходит в цели при проценте (целые рубли остаются целыми). */
  function pctPart(total, pct) {
    if (pct <= 0 || total <= 0) return 0;
    if (pct >= 100) return total;
    const q = total % 100 === 0 ? 100 : 1;
    return Math.floor((total * pct + 50 * q) / (100 * q)) * q;
  }

  /** Деление пропорционально весам без потери копеек (метод наибольших остатков). */
  function splitWeighted(total, weights) {
    const n = weights.length;
    if (!n) return [];
    const wsum = weights.reduce((a, b) => a + b, 0);
    if (wsum <= 0 || weights.some((w) => w < 0)) throw new Error('weights must be positive');
    const q = total >= 100 ? 100 : 1;
    const units = Math.floor(total / q);
    const rest = total % q;
    const base = weights.map((w) => Math.floor((units * w) / wsum));
    const rems = weights.map((w) => (units * w) % wsum);
    const left = units - base.reduce((a, b) => a + b, 0);
    const order = weights.map((_, i) => i).sort((a, b) => rems[b] - rems[a] || weights[b] - weights[a] || a - b);
    for (const i of order.slice(0, left)) base[i] += 1;
    const result = base.map((b) => b * q);
    if (rest) {
      let top = 0;
      for (let i = 1; i < n; i++) if (weights[i] > weights[top]) top = i;
      result[top] += rest;
    }
    return result;
  }

  /** По целям пропорционально весам, не переполняя их; излишек перетекает в остальные. */
  function allocateCapped(total, items) {
    const alloc = new Map(items.map(([key]) => [key, 0]));
    let active = items.filter(([, w, room]) => w > 0 && room > 0);
    let remaining = total;
    while (remaining > 0 && active.length) {
      const shares = splitWeighted(
        remaining,
        active.map(([, w]) => w),
      );
      let overflow = 0;
      const stillOpen = [];
      active.forEach(([key, w, room], i) => {
        const give = Math.min(shares[i], room - alloc.get(key));
        alloc.set(key, alloc.get(key) + give);
        overflow += shares[i] - give;
        if (alloc.get(key) < room) stillOpen.push([key, w, room]);
      });
      if (overflow === remaining) break; // никто ничего не взял
      remaining = overflow;
      active = stillOpen;
    }
    return { alloc, left: remaining };
  }

  // ═══════════════════ категории ═══════════════════

  const CATS = new Map();
  for (const c of [...DATA.expense, ...DATA.income, DATA.goal_purchase]) CATS.set(c.key, c);
  const IMPULSE = new Set(DATA.impulse);
  const STOP = new Set(DATA.stopwords);
  const WORD_RE = /[a-zа-я0-9][a-zа-я0-9.-]*/g;
  const TOKEN_RE = /[a-zа-я0-9]+/g;

  function category(key) {
    return CATS.get(key) || CATS.get(DATA.default_expense);
  }

  function forKind(kind) {
    return kind === 'income' ? DATA.income : DATA.expense;
  }

  function defaultFor(kind) {
    return kind === 'income' ? DATA.default_income : DATA.default_expense;
  }

  function validCategory(key, kind) {
    return Boolean(key) && forKind(kind).some((c) => c.key === key);
  }

  function normalize(text) {
    return String(text).toLowerCase().replace(/ё/g, 'е');
  }

  /** Категория по словам комментария (словарь). null — если не узнали. */
  function detect(note, kind) {
    if (!note) return null;
    const text = normalize(note);
    const index = DATA.index[kind === 'income' ? 'income' : 'expense'];
    for (const [phrase, key] of index.phrases) if (text.includes(phrase)) return key;
    const words = text.match(WORD_RE) || [];
    for (const [kw, key] of index.tokens) {
      for (const word of words) if (kw.length < 4 ? word === kw : word.startsWith(kw)) return key;
    }
    return null;
  }

  // ═══════════════════ память категорий ═══════════════════

  function tokens(note) {
    return normalize(note).match(TOKEN_RE) || [];
  }

  function mainWord(note) {
    for (const t of tokens(note)) if (t.length >= 3 && !/^\d+$/.test(t) && !STOP.has(t)) return t;
    return null;
  }

  /** Ключи памяти: фраза целиком, затем главное слово. */
  function keysFor(note) {
    if (!note) return [];
    const words = tokens(note);
    const keys = words.length ? ['=' + words.join(' ')] : [];
    const word = mainWord(note);
    if (word) keys.push('~' + word);
    return keys;
  }

  function recall(state, kind, keys) {
    for (const key of keys) {
      const hit = state.learned.get(kind + '|' + key);
      if (hit) return hit.category;
    }
    return null;
  }

  function learn(state, kind, keys, cat, source) {
    for (const key of keys) {
      const id = kind + '|' + key;
      const current = state.learned.get(id);
      if (source !== 'user' && current && current.source === 'user') continue; // твой выбор главнее
      state.learned.set(id, { kind, key, category: cat, source });
    }
  }

  function resolve(state, note, kind) {
    if (!note) return null;
    const learned = recall(state, kind, keysFor(note));
    return validCategory(learned, kind) ? learned : detect(note, kind);
  }

  function remember(state, note, kind, cat, source) {
    let keys = keysFor(note);
    if ((source || 'user') !== 'user') keys = keys.slice(0, 1);
    if (keys.length && validCategory(cat, kind)) learn(state, kind, keys, cat, source || 'user');
  }

  /** Запись без знака: доход, если слова «доходные» или ты так уже учил. */
  function guessKind(state, note) {
    const keys = keysFor(note);
    if (keys.length) {
      const income = recall(state, 'income', keys);
      const expense = recall(state, 'expense', keys);
      if (income && !expense) return 'income';
      if (expense && !income) return 'expense';
    }
    return detect(note, 'income') && !detect(note, 'expense') ? 'income' : 'expense';
  }

  // ═══════════════════ записи и балансы ═══════════════════

  class TxBlocked extends Error {
    constructor(reason) {
      super(reason);
      this.reason = reason;
    }
  }

  const SPENT =
    'Нельзя: эти деньги уже ушли дальше (переведены или потрачены), и отмена увела бы баланс в минус.';

  function emptyState() {
    return { settings: { goal_pct: 80, harsh: 1, currency: '₽' }, goals: [], txs: [], learned: new Map(), lastId: 0 };
  }

  function newId(state) {
    state.lastId = Math.max(Date.now(), state.lastId + 1);
    return state.lastId;
  }

  function insertTx(state, kind, amount, opts) {
    const o = opts || {};
    const tx = {
      id: newId(state),
      kind,
      amount,
      category: o.category || null,
      note: o.note || null,
      day: o.day || today(),
      created_at: o.created_at != null ? o.created_at : nowTs(),
      goal_pct: o.goal_pct != null ? o.goal_pct : null,
      moves: [],
    };
    state.txs.push(tx);
    return tx;
  }

  function move(tx, account, goalId, amount) {
    if (amount) tx.moves.push({ account, goal_id: goalId == null ? null : goalId, amount });
  }

  function getTx(state, id) {
    return state.txs.find((t) => t.id === id) || null;
  }

  function balances(state) {
    let wallet = 0;
    let pool = 0;
    const goals = new Map();
    for (const tx of state.txs) {
      for (const m of tx.moves) {
        if (m.account === 'wallet') wallet += m.amount;
        else if (m.account === 'pool') pool += m.amount;
        else if (m.goal_id != null) goals.set(m.goal_id, (goals.get(m.goal_id) || 0) + m.amount);
      }
    }
    let inGoals = 0;
    for (const g of state.goals) if (g.status === 'active') inGoals += goals.get(g.id) || 0;
    return { wallet, pool, goals, in_goals: inGoals, savings: pool + inGoals, total: wallet + pool + inGoals };
  }

  function getGoal(state, id) {
    return state.goals.find((g) => g.id === id) || null;
  }

  /** Цель с накопленным и производными полями (как Goal в Python). */
  function goalView(goal, bal) {
    const saved = bal.goals.get(goal.id) || 0;
    const reached = saved >= goal.target;
    return Object.assign({}, goal, {
      saved,
      reached,
      remaining: Math.max(0, goal.target - saved),
      progress: goal.target > 0 ? Math.max(0, saved / goal.target) : 0,
      receives: goal.status === 'active' && goal.weight > 0 && !reached,
    });
  }

  function goalViews(state, status, bal) {
    const b = bal || balances(state);
    const list = state.goals.filter((g) => status === null || g.status === (status || 'active'));
    if ((status || 'active') === 'active') list.sort((a, c) => a.id - c.id);
    else list.sort((a, c) => (c.closed_at || 0) - (a.closed_at || 0) || c.id - a.id);
    return list.map((g) => goalView(g, b));
  }

  function plan(state, amount, exclude, bal) {
    const result = { alloc: new Map(), left: 0, completed: [] };
    if (amount <= 0) return result;
    const b = bal || balances(state);
    const skip = exclude || new Set();
    const goals = state.goals.filter((g) => g.status === 'active' && !skip.has(g.id)).sort((a, c) => a.id - c.id);
    const saved = (g) => b.goals.get(g.id) || 0;
    const { alloc, left } = allocateCapped(
      amount,
      goals.map((g) => [g.id, g.weight, g.target - saved(g)]),
    );
    for (const g of goals) {
      const a = alloc.get(g.id) || 0;
      if (a <= 0) continue;
      result.alloc.set(g.id, a);
      if (saved(g) < g.target && g.target <= saved(g) + a) result.completed.push(g.id);
    }
    result.left = left;
    return result;
  }

  function applyIncome(state, tx, pct) {
    const goalsPart = pctPart(tx.amount, pct);
    const p = plan(state, goalsPart);
    for (const [gid, a] of p.alloc) move(tx, 'goal', gid, a);
    move(tx, 'pool', null, p.left);
    move(tx, 'wallet', null, tx.amount - goalsPart);
    return { tx, completed: p.completed };
  }

  function addIncome(state, o) {
    const pct = o.pct != null ? o.pct : state.settings.goal_pct;
    const tx = insertTx(state, 'income', o.amount, Object.assign({}, o, { goal_pct: pct }));
    return applyIncome(state, tx, pct);
  }

  function addExpense(state, o) {
    const tx = insertTx(state, 'expense', o.amount, o);
    move(tx, 'wallet', null, -o.amount);
    return tx;
  }

  function checkUnlocked(state, tx) {
    for (const gid of new Set(tx.moves.filter((m) => m.goal_id != null).map((m) => m.goal_id))) {
      const g = getGoal(state, gid);
      if (g && g.status !== 'active' && g.close_tx_id !== tx.id) {
        throw new TxBlocked(
          `Эта запись связана с закрытой целью «${g.title}». Сначала верни цель (отмени её закрытие).`,
        );
      }
    }
  }

  /** После изменения накопления (и кошелёк, если wallet) не должны уйти в новый минус. */
  function noNewDebt(before, after, wallet) {
    if (after.pool < 0 && after.pool < before.pool) throw new TxBlocked(SPENT);
    for (const [gid, value] of after.goals) {
      if (value < 0 && value < (before.goals.get(gid) || 0)) throw new TxBlocked(SPENT);
    }
    if (wallet && after.wallet < 0 && after.wallet < before.wallet) throw new TxBlocked(SPENT);
  }

  /** Удаляет запись. Закрытая этой записью цель снова становится активной. */
  function deleteTx(state, id) {
    const i = state.txs.findIndex((t) => t.id === id);
    if (i < 0) return null;
    const tx = state.txs[i];
    checkUnlocked(state, tx);
    const before = balances(state);
    state.txs.splice(i, 1);
    try {
      noNewDebt(before, balances(state), tx.kind === 'transfer' || tx.kind === 'adjust');
    } catch (e) {
      state.txs.splice(i, 0, tx);
      throw e;
    }
    for (const g of state.goals) {
      if (g.close_tx_id === id) Object.assign(g, { status: 'active', closed_at: null, close_tx_id: null });
    }
    return tx;
  }

  function setCategory(state, id, cat) {
    const tx = getTx(state, id);
    if (!tx || (tx.kind !== 'income' && tx.kind !== 'expense') || tx.category === 'goal') return false;
    tx.category = cat;
    return true;
  }

  function available(bal, account, goalId) {
    if (account === 'outside') return null;
    if (account === 'wallet') return bal.wallet;
    if (account === 'pool') return bal.pool;
    return bal.goals.get(goalId) || 0;
  }

  /**
   * Перевод. src: wallet | pool | goal | outside (отложено ещё до бота);
   * dst: wallet | pool | goal | goals (по долям всех активных целей).
   * strict — не переводить больше, чем есть в источнике.
   */
  function transfer(state, src, dst, amount, opts) {
    const o = Object.assign({ strict: true }, opts || {});
    if (amount <= 0) return null;
    const bal = balances(state);
    if (o.strict) {
      const have = available(bal, src, o.srcGoal);
      if (have !== null && amount > have) return null;
    }
    let toGoals = new Map();
    let toPool = 0;
    let completed = [];
    let moved = amount;
    if (dst === 'goals') {
      const exclude = new Set(src === 'goal' && o.srcGoal != null ? [o.srcGoal] : []);
      const p = plan(state, amount, exclude, bal);
      let left = p.left;
      if (src === 'pool') {
        moved = amount - left; // из свободных забираем только то, что влезло в цели
        left = 0;
      }
      toGoals = p.alloc;
      toPool = left;
      completed = p.completed;
    } else if (dst === 'goal') {
      const g = o.dstGoal != null ? getGoal(state, o.dstGoal) : null;
      if (!g || g.status !== 'active') return null;
      const saved = bal.goals.get(g.id) || 0;
      toGoals = new Map([[g.id, amount]]);
      if (saved < g.target && g.target <= saved + amount) completed = [g.id];
    } else if (dst === 'pool') {
      toPool = amount;
    } else if (dst !== 'wallet') {
      throw new Error('unknown destination ' + dst);
    }
    if (moved <= 0) return null;
    const tx = insertTx(state, src === 'outside' ? 'adjust' : 'transfer', moved, { note: o.note });
    if (src !== 'outside') move(tx, src, src === 'goal' ? o.srcGoal : null, -moved);
    for (const [gid, a] of toGoals) move(tx, 'goal', gid, a);
    move(tx, 'pool', null, toPool);
    if (dst === 'wallet') move(tx, 'wallet', null, moved);
    return { tx, amount: moved, toGoals, toPool, completed };
  }

  /** Точный остаток «на расходы» — корректировкой. */
  function setWallet(state, value) {
    const delta = value - balances(state).wallet;
    if (delta === 0) return null;
    const tx = insertTx(state, 'adjust', Math.abs(delta), { note: 'Корректировка остатка на расходы' });
    move(tx, 'wallet', null, delta);
    return tx;
  }

  // ═══════════════════ цели ═══════════════════

  function createGoal(state, title, target, weight) {
    const goal = {
      id: newId(state),
      title,
      target,
      weight: weight == null ? 1 : weight,
      status: 'active',
      deadline: null,
      created_at: nowTs(),
      closed_at: null,
      close_tx_id: null,
      photos: [],
    };
    state.goals.push(goal);
    return goal;
  }

  function updateGoal(state, id, fields) {
    const g = getGoal(state, id);
    if (!g || g.status !== 'active') return null;
    for (const k of ['title', 'target', 'weight', 'deadline']) if (k in fields) g[k] = fields[k];
    return g;
  }

  /** Цель куплена: накопленное списывается расходом «Покупка цели». */
  function buyGoal(state, id) {
    const g = getGoal(state, id);
    const saved = g ? balances(state).goals.get(g.id) || 0 : 0;
    if (!g || g.status !== 'active' || saved <= 0) return null;
    const spent = Math.min(saved, g.target);
    const tx = insertTx(state, 'expense', spent, { category: 'goal', note: g.title });
    move(tx, 'goal', g.id, -saved);
    move(tx, 'pool', null, saved - spent); // лишнее — в свободные
    Object.assign(g, { status: 'bought', closed_at: nowTs(), close_tx_id: tx.id });
    return { tx, spent };
  }

  /** Удаляет цель, а её деньги переводит: goals | pool | wallet. */
  function deleteGoal(state, id, dest) {
    const g = getGoal(state, id);
    if (!g || g.status !== 'active') return null;
    const bal = balances(state);
    const amount = bal.goals.get(g.id) || 0;
    let result = { tx: null, amount: 0, toGoals: new Map(), toPool: 0, completed: [] };
    if (amount !== 0) {
      const where = amount < 0 ? 'wallet' : dest;
      let toGoals = new Map();
      let toPool = 0;
      let completed = [];
      if (where === 'goals') {
        const p = plan(state, amount, new Set([g.id]), bal);
        toGoals = p.alloc;
        toPool = p.left;
        completed = p.completed;
      } else if (where === 'pool') {
        toPool = amount;
      }
      const tx = insertTx(state, 'transfer', Math.abs(amount), { note: `Удаление цели «${g.title}»` });
      move(tx, 'goal', g.id, -amount);
      for (const [gid, a] of toGoals) move(tx, 'goal', gid, a);
      move(tx, 'pool', null, toPool);
      if (where === 'wallet') move(tx, 'wallet', null, amount);
      result = { tx, amount, toGoals, toPool, completed };
    }
    Object.assign(g, { status: 'deleted', closed_at: nowTs(), close_tx_id: result.tx ? result.tx.id : null });
    return result;
  }

  // ═══════════════════ сводки ═══════════════════

  function isRegular(tx) {
    return tx.kind === 'income' || tx.kind === 'expense';
  }

  function dayTotals(state, first, last) {
    const out = new Map();
    for (const tx of state.txs) {
      if (!isRegular(tx) || tx.day < first || tx.day > last) continue;
      const row = out.get(tx.day) || [0, 0];
      row[tx.kind === 'income' ? 0 : 1] += tx.amount;
      out.set(tx.day, row);
    }
    return out;
  }

  function summary(state, first, last) {
    const s = { income: 0, expense: 0, saved: 0, income_count: 0, expense_count: 0, by_category: [] };
    const cats = new Map();
    const spendDays = new Set();
    for (const tx of state.txs) {
      if (tx.day < first || tx.day > last) continue;
      if (tx.kind === 'income') {
        s.income += tx.amount;
        s.income_count += 1;
      } else if (tx.kind === 'expense') {
        s.expense += tx.amount;
        s.expense_count += 1;
        const key = tx.category || 'other';
        const row = cats.get(key) || [key, 0, 0];
        row[1] += tx.amount;
        row[2] += 1;
        cats.set(key, row);
        if (key !== 'goal') spendDays.add(tx.day);
      }
      if (tx.kind === 'income' || tx.kind === 'transfer') {
        for (const m of tx.moves) if (m.account === 'goal' || m.account === 'pool') s.saved += m.amount;
      }
    }
    s.by_category = [...cats.values()].sort((a, b) => b[1] - a[1]);
    s.goal_purchases = s.by_category.filter(([k]) => k === 'goal').reduce((a, [, v]) => a + v, 0);
    s.spend_days = spendDays.size;
    return s;
  }

  /** Средний доход в день и средние отложения в день за последние N дней. */
  function dailyRates(state, day, days) {
    const n = days || 30;
    const since = addDays(day || today(), -(n - 1));
    let income = 0;
    let saved = 0;
    for (const tx of state.txs) {
      if (tx.day < since) continue;
      if (tx.kind === 'income') income += tx.amount;
      if (tx.kind === 'income' || tx.kind === 'transfer') {
        for (const m of tx.moves) if (m.account === 'goal' || m.account === 'pool') saved += m.amount;
      }
    }
    return [income / n, Math.max(0, saved) / n];
  }

  function firstDay(state) {
    let first = null;
    for (const tx of state.txs) if (isRegular(tx) && (first === null || tx.day < first)) first = tx.day;
    return first;
  }

  /** Сколько дней подряд без трат на хотелки. null — если данных ещё нет. */
  function impulseStreak(state, day) {
    const first = firstDay(state);
    if (first === null) return null;
    const now = day || today();
    let last = null;
    for (const tx of state.txs) {
      if (tx.kind === 'expense' && IMPULSE.has(tx.category) && (last === null || tx.day > last)) last = tx.day;
    }
    if (last === null) return daysBetween(first, now) + 1;
    return Math.max(0, daysBetween(last, now));
  }

  function impulseTotal(summ) {
    return summ.by_category.filter(([k]) => IMPULSE.has(k)).reduce((a, [, v]) => a + v, 0);
  }

  function pctText(fraction) {
    const p = Math.max(0, fraction) * 100;
    if (p === 0) return '0%';
    if (p < 0.1) return '<0,1%';
    if (p < 10) return p.toFixed(1).replace('.', ',').replace(',0', '') + '%';
    return Math.round(p) + '%';
  }

  /** Короткий жёсткий вердикт по месяцу. */
  function verdict(summ, impulse) {
    const spent = summ.expense - summ.goal_purchases;
    if (summ.income === 0 && spent === 0) {
      return 'Пусто. Либо ты ничего не делал, либо ничего не записывал. Оба варианта — плохо.';
    }
    if (summ.income && spent > summ.income) {
      return 'Ты тратишь больше, чем зарабатываешь. Это прямая дорога в долги. Режь расходы. Сегодня.';
    }
    const share = spent ? impulse / spent : 0;
    if (share >= 0.4) {
      return `${pctText(share)} трат — на хотелки. Ты работаешь на кафе и маркетплейсы, а не на свою мечту.`;
    }
    if (share >= 0.2) {
      return 'Каждый пятый рубль уходит на ерунду. Неплохо, но ты способен на большее. Правило 40%.';
    }
    if (summ.income && summ.saved >= summ.income * 0.7) {
      return 'Мощно. Большая часть дохода работает на цели. Не расслабляйся — держи темп.';
    }
    return 'Нормально. Но «нормально» — это для обычных людей. Ты хочешь быть обычным?';
  }

  function daysLeftInMonth(day) {
    const [y, m, d] = day.split('-').map(Number);
    return daysInMonth(y, m) - d + 1;
  }

  /** Сколько можно тратить в день до конца месяца (целыми рублями). */
  function safePerDay(wallet, day) {
    const v = Math.max(0, wallet) / daysLeftInMonth(day);
    return Math.floor(Math.floor(v) / 100) * 100;
  }

  function goalShares(views) {
    const receiving = views.filter((g) => g.receives);
    const total = receiving.reduce((a, g) => a + g.weight, 0);
    const out = new Map();
    if (total) for (const g of receiving) out.set(g.id, g.weight / total);
    return out;
  }

  function mainGoal(views) {
    const active = views.filter((g) => g.status === 'active');
    if (!active.length) return null;
    const receiving = active.filter((g) => g.receives);
    const pool = receiving.length ? receiving : active;
    return pool.reduce((best, g) => (g.weight > best.weight || (g.weight === best.weight && g.id < best.id) ? g : best));
  }

  function etaDays(goal, share, savingsPerDay) {
    if (goal.reached) return 0;
    const rate = savingsPerDay * share;
    return rate <= 0 ? null : goal.remaining / rate;
  }

  // ═══════════════════ мотивация ═══════════════════

  const recent = new Map();

  function seeded(seed) {
    let s = Number(seed) % 4294967296 >>> 0 || 1;
    return () => {
      s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  /** Жёсткая фраза для ситуации. seed — чтобы фраза к одной записи не менялась. */
  function pick(ctx, level, seed) {
    const base = DATA.quotes['1'][ctx] || DATA.quotes['1'].general;
    const hard = (level || 1) >= 2 ? DATA.quotes['2'][ctx] || [] : [];
    const rnd = seed == null ? Math.random : seeded(seed);
    const pool = hard.length && rnd() < 0.6 ? hard : base;
    const choose = () => pool[Math.floor(rnd() * pool.length)];
    if (seed != null) return choose();
    const seen = recent.get(ctx) || [];
    const fresh = pool.filter((q) => !seen.includes(q)); // недавние фразы не повторяем
    const choice = fresh.length ? fresh[Math.floor(rnd() * fresh.length)] : choose();
    seen.push(choice);
    if (seen.length > 40) seen.shift();
    recent.set(ctx, seen);
    return choice;
  }

  return {
    MAX_WEIGHT,
    TxBlocked,
    today,
    nowTs,
    parseIso,
    addDays,
    daysBetween,
    daysInMonth,
    monthBounds,
    shiftMonth,
    parseAmount,
    pctPart,
    splitWeighted,
    allocateCapped,
    category,
    forKind,
    defaultFor,
    validCategory,
    detect,
    tokens,
    mainWord,
    keysFor,
    recall,
    resolve,
    remember,
    guessKind,
    emptyState,
    newId,
    getTx,
    balances,
    getGoal,
    goalView,
    goalViews,
    plan,
    addIncome,
    addExpense,
    deleteTx,
    setCategory,
    transfer,
    setWallet,
    createGoal,
    updateGoal,
    buyGoal,
    deleteGoal,
    dayTotals,
    summary,
    dailyRates,
    firstDay,
    impulseStreak,
    impulseTotal,
    pctText,
    verdict,
    daysLeftInMonth,
    safePerDay,
    goalShares,
    mainGoal,
    etaDays,
    pick,
  };
});
