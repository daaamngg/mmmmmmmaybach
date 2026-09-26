/* Версия без сервера: данные в облаке Telegram (CloudStorage), логика — в core.js.
 *
 * Отвечает на те же запросы /api/…, что и сайт внутри бота (finbot/webapp/api.py),
 * поэтому интерфейс (app.js) один на обе версии.
 *
 * Облако Telegram: до 1024 ключей на человека, значение — до 4096 символов.
 * Поэтому записи хранятся «страницами» (t0, t1…), цели — g0…, память слов — l0…,
 * настройки — s, фото — кусками p<id>_<n>. Пишутся только изменившиеся страницы.
 */
'use strict';

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./core.js'));
  else root.FinLocal = factory(root.FinCore);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (C) {
  const PAGE_BYTES = 3800; // запас до лимита Telegram в 4096 символов
  const CHUNK = 3800; // кусок фото (base64 — только латиница, символ = байт)
  const MAX_KEYS = 1024;
  const KEY_RESERVE = 60; // место под записи, чтобы фото не заняли всё
  const MAX_PHOTO_CHUNKS = 14;
  const RELOAD_AFTER_MS = 30000; // вернулся в приложение — подтягиваем изменения с другого устройства
  const MAX_NOTE = 100;
  const MAX_TITLE = 60;
  const MONTHS = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
  const MONTHS_GEN = ['', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const WEEKDAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье'];
  const QUOTE_CONTEXTS = new Set(['general', 'speech', 'morning', 'want', 'resisted', 'income', 'expense_impulse', 'overspent']);

  const encoder = typeof TextEncoder === 'function' ? new TextEncoder() : null;
  function bytes(s) {
    return encoder ? encoder.encode(s).length : unescape(encodeURIComponent(s)).length;
  }

  // ═══════════════════ хранилища ═══════════════════

  /** Облако Telegram через мост клиента (тот же протокол, что у telegram-web-app.js). */
  function cloudStorage(bridge) {
    const pending = new Map();
    bridge.onEvent('custom_method_invoked', (data) => {
      const req = data && pending.get(data.req_id);
      if (!req) return;
      pending.delete(data.req_id);
      clearTimeout(req.timer);
      if (data.error !== undefined && data.error !== null && data.error !== '') req.reject(new Error(String(data.error)));
      else req.resolve(data.result);
    });
    function call(method, params) {
      return new Promise((resolve, reject) => {
        const reqId = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
        const timer = setTimeout(() => {
          pending.delete(reqId);
          reject(new Error('timeout'));
        }, 15000);
        pending.set(reqId, { resolve, reject, timer });
        bridge.post('web_app_invoke_custom_method', { req_id: reqId, method, params });
      });
    }
    return {
      keys: async () => (await call('getStorageKeys', {})) || [],
      getMany: async (keys) => (keys.length ? (await call('getStorageValues', { keys })) || {} : {}),
      set: (key, value) => call('saveStorageValue', { key, value }),
      remove: (keys) => (keys.length ? call('deleteStorageValues', { keys }) : Promise.resolve(true)),
    };
  }

  /** Хранилище в памяти с теми же ограничениями, что у Telegram (для тестов). */
  function memoryStorage(initial) {
    const data = new Map(Object.entries(initial || {}));
    const check = (key, value) => {
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(key)) throw new Error('KEY_INVALID ' + key);
      if (typeof value !== 'string' || value.length > 4096) throw new Error('VALUE_TOO_LONG ' + key);
      if (!data.has(key) && data.size >= MAX_KEYS) throw new Error('TOO_MANY_KEYS');
    };
    return {
      data,
      keys: async () => [...data.keys()],
      getMany: async (keys) => Object.fromEntries(keys.filter((k) => data.has(k)).map((k) => [k, data.get(k)])),
      set: async (key, value) => {
        check(key, value);
        data.set(key, value);
        return true;
      },
      remove: async (keys) => {
        for (const k of keys) data.delete(k);
        return true;
      },
    };
  }

  // ═══════════════════ страницы ═══════════════════

  const KIND = { income: 'i', expense: 'e', transfer: 't', adjust: 'a' };
  const KIND_BACK = { i: 'income', e: 'expense', t: 'transfer', a: 'adjust' };
  const ACCOUNT = { wallet: 'w', pool: 'p', goal: 'g' };
  const ACCOUNT_BACK = { w: 'wallet', p: 'pool', g: 'goal' };
  const STATUS = { active: 'a', bought: 'b', deleted: 'd' };
  const STATUS_BACK = { a: 'active', b: 'bought', d: 'deleted' };

  const TX_CODEC = {
    prefix: 't',
    id: (t) => t.id,
    encode: (t) => [
      t.id,
      KIND[t.kind],
      t.amount,
      t.category,
      t.note,
      t.day,
      t.created_at,
      t.goal_pct,
      t.moves.map((m) => [ACCOUNT[m.account], m.goal_id || 0, m.amount]),
    ],
    decode: (r) => ({
      id: r[0],
      kind: KIND_BACK[r[1]],
      amount: r[2],
      category: r[3],
      note: r[4],
      day: r[5],
      created_at: r[6],
      goal_pct: r[7],
      moves: (r[8] || []).map((m) => ({ account: ACCOUNT_BACK[m[0]], goal_id: m[1] || null, amount: m[2] })),
    }),
  };

  const GOAL_CODEC = {
    prefix: 'g',
    id: (g) => g.id,
    encode: (g) => [
      g.id,
      g.title,
      g.target,
      g.weight,
      STATUS[g.status],
      g.deadline,
      g.created_at,
      g.closed_at,
      g.close_tx_id,
      g.photos.map((p) => [p.id, p.n]),
    ],
    decode: (r) => ({
      id: r[0],
      title: r[1],
      target: r[2],
      weight: r[3],
      status: STATUS_BACK[r[4]],
      deadline: r[5],
      created_at: r[6],
      closed_at: r[7],
      close_tx_id: r[8],
      photos: (r[9] || []).map((p) => ({ id: p[0], n: p[1] })),
    }),
  };

  const LEARN_CODEC = {
    prefix: 'l',
    id: (l) => l.kind + '|' + l.key,
    encode: (l) => [l.kind === 'income' ? 'i' : 'e', l.key, l.category, l.source === 'user' ? 'u' : 'a'],
    decode: (r) => ({ kind: r[0] === 'i' ? 'income' : 'expense', key: r[1], category: r[2], source: r[3] === 'u' ? 'user' : 'ai' }),
  };

  /**
   * Список, разложенный по страницам. Элемент остаётся на своей странице, пока она ему
   * по размеру, — поэтому новая запись или правка меняют одну страницу, а не все.
   */
  class Paged {
    constructor(codec) {
      this.codec = codec;
      this.re = new RegExp('^' + codec.prefix + '(\\d+)$');
      this.where = new Map(); // id элемента → номер страницы
      this.written = new Map(); // ключ → что лежит в облаке
    }

    load(values) {
      const items = [];
      this.where.clear();
      this.written.clear();
      for (const [key, value] of Object.entries(values)) {
        const m = this.re.exec(key);
        if (!m) continue;
        let rows;
        try {
          rows = JSON.parse(value);
        } catch (e) {
          continue; // битую страницу пропускаем, остальное читаем
        }
        this.written.set(key, value);
        for (const row of rows) {
          const item = this.codec.decode(row);
          this.where.set(this.codec.id(item), Number(m[1]));
          items.push(item);
        }
      }
      return items;
    }

    /** Что записать и что удалить, чтобы в облаке лежал этот список. */
    plan(items) {
      const pages = new Map();
      const fresh = [];
      for (const item of items) {
        const n = this.where.get(this.codec.id(item));
        if (n === undefined) fresh.push(item);
        else (pages.get(n) || pages.set(n, []).get(n)).push(item);
      }
      const used = [...pages.keys(), ...[...this.written.keys()].map((k) => Number(this.re.exec(k)[1]))];
      let last = used.length ? Math.max(...used) : -1;
      const size = (list) => bytes(JSON.stringify(list.map(this.codec.encode)));
      for (const item of fresh) {
        const tail = pages.get(last);
        if (tail && size([...tail, item]) <= PAGE_BYTES) tail.push(item);
        else pages.set(++last, [item]);
      }
      for (const n of [...pages.keys()]) {
        const list = pages.get(n);
        while (list.length > 1 && size(list) > PAGE_BYTES) {
          const moved = [list.pop()];
          while (size(list) > PAGE_BYTES && list.length > 1) moved.unshift(list.pop());
          pages.set(++last, moved);
        }
      }
      const where = new Map();
      const values = new Map();
      for (const [n, list] of pages) {
        const value = JSON.stringify(list.map(this.codec.encode));
        if (bytes(value) > 4096) throw new Error('Запись слишком длинная');
        values.set(this.codec.prefix + n, value);
        for (const item of list) where.set(this.codec.id(item), n);
      }
      const writes = [...values].filter(([k, v]) => this.written.get(k) !== v);
      const removes = [...this.written.keys()].filter((k) => !values.has(k));
      return { writes, removes, where };
    }
  }

  // ═══════════════════ приложение ═══════════════════

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  function create(storage) {
    let state = null;
    let loadedAt = 0;
    let photoKeys = new Set();
    const photoCache = new Map();
    const stores = { txs: new Paged(TX_CODEC), goals: new Paged(GOAL_CODEC), learned: new Paged(LEARN_CODEC) };
    let settingsWritten = null;
    let queue = Promise.resolve();

    function serial(fn) {
      const run = queue.then(fn, fn);
      queue = run.catch(() => {});
      return run;
    }

    async function load() {
      const keys = await storage.keys();
      photoKeys = new Set(keys.filter((k) => /^p\d+_\d+$/.test(k)));
      const dataKeys = keys.filter((k) => !photoKeys.has(k));
      const values = {};
      for (let i = 0; i < dataKeys.length; i += 50) Object.assign(values, await storage.getMany(dataKeys.slice(i, i + 50)));
      const s = C.emptyState();
      if (values.s) {
        try {
          Object.assign(s.settings, JSON.parse(values.s));
        } catch (e) {
          /* настройки по умолчанию */
        }
      }
      settingsWritten = values.s || null;
      s.txs = stores.txs.load(values).sort((a, b) => a.id - b.id);
      s.goals = stores.goals.load(values).sort((a, b) => a.id - b.id);
      for (const l of stores.learned.load(values)) s.learned.set(l.kind + '|' + l.key, l);
      const ids = [0, ...s.txs.map((t) => t.id), ...s.goals.map((g) => g.id)];
      for (const g of s.goals) for (const p of g.photos) ids.push(p.id);
      s.lastId = Math.max(...ids);
      state = s;
      loadedAt = Date.now();
    }

    async function persist() {
      const plans = [
        [stores.txs, stores.txs.plan(state.txs)],
        [stores.goals, stores.goals.plan(state.goals)],
        [stores.learned, stores.learned.plan([...state.learned.values()])],
      ];
      for (const [store, p] of plans) {
        for (const [key, value] of p.writes) {
          await storage.set(key, value);
          store.written.set(key, value);
        }
        if (p.removes.length) {
          await storage.remove(p.removes);
          for (const key of p.removes) store.written.delete(key);
        }
        store.where = p.where;
      }
      const settings = JSON.stringify(state.settings);
      if (settings !== settingsWritten) {
        await storage.set('s', settings);
        settingsWritten = settings;
      }
    }

    // Изменение: если запись в облако не удалась, перечитываем облако — память не расходится с ним.
    async function change(fn) {
      try {
        const result = fn();
        await persist();
        return result;
      } catch (e) {
        if (!(e instanceof ApiError) && !(e instanceof C.TxBlocked)) {
          await load().catch(() => {});
          throw new ApiError(503, 'Не удалось сохранить в облако Telegram. Проверь интернет и попробуй ещё раз.');
        }
        throw e;
      }
    }

    // ── сериализация (как в api.py) ──

    const settings = () => state.settings;
    const harsh = () => settings().harsh || 1;

    function categoryJson(c) {
      return { key: c.key, emoji: c.emoji, name: c.name, impulse: c.tier === 'impulse' };
    }

    function localTime(ts, day) {
      const d = new Date(ts * 1000);
      if (C.today(d) !== day) return null;
      return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }

    function txJson(tx) {
      const regular = tx.kind === 'income' || tx.kind === 'expense';
      const cat = C.category(tx.category);
      let emoji = cat.emoji;
      let label = cat.name;
      if (tx.kind === 'transfer') [emoji, label] = ['🔁', 'Перевод'];
      if (tx.kind === 'adjust') [emoji, label] = ['⚙️', 'Корректировка'];
      const sum = (accounts) => tx.moves.filter((m) => accounts.includes(m.account)).reduce((a, m) => a + m.amount, 0);
      return {
        id: tx.id,
        kind: tx.kind,
        amount: tx.amount,
        category: regular ? tx.category : null,
        emoji,
        label,
        note: tx.note || '',
        day: tx.day,
        time: localTime(tx.created_at, tx.day),
        saved: tx.kind === 'income' ? sum(['goal', 'pool']) : 0,
        wallet: sum(['wallet']),
        impulse: tx.kind === 'expense' && cat.tier === 'impulse',
      };
    }

    function goalsPayload(bal) {
      const views = C.goalViews(state, 'active', bal);
      const shares = C.goalShares(views);
      const spd = C.dailyRates(state, C.today())[1];
      const goals = views.map((g) => {
        const share = shares.get(g.id) || 0;
        const eta = C.etaDays(g, share, spd);
        return {
          id: g.id,
          title: g.title,
          target: g.target,
          saved: g.saved,
          remaining: g.remaining,
          progress: Math.round(g.progress * 1e5) / 1e5,
          weight: g.weight,
          share: Math.round(share * 1e4) / 1e4,
          reached: g.reached,
          eta_days: eta ? Math.round(eta * 10) / 10 : null,
          deadline: g.deadline,
          photos: g.photos.map((p) => p.id),
        };
      });
      const main = C.mainGoal(views);
      return { goals, main: main ? main.id : null };
    }

    function goalJson(id) {
      return goalsPayload(C.balances(state)).goals.find((g) => g.id === id) || null;
    }

    function titles(ids) {
      return ids.map((id) => C.getGoal(state, id)).filter(Boolean).map((g) => g.title);
    }

    function quoteFor(tx, wallet) {
      if (tx.kind === 'income') return C.pick('income', harsh(), tx.id);
      if (wallet < 0) return C.pick('overspent', harsh(), tx.id);
      const tier = C.category(tx.category).tier;
      const ctx = tier === 'impulse' ? 'expense_impulse' : tier === 'growth' ? 'expense_growth' : 'expense_normal';
      return C.pick(ctx, harsh(), tx.id);
    }

    function int(value, what) {
      const n = Number(value);
      if (!Number.isInteger(n)) throw new ApiError(400, 'Некорректный параметр: ' + what);
      return n;
    }

    function monthTitle(y, m) {
      return `${MONTHS[m]} ${y}`;
    }

    function fmtDay(iso, now) {
      const [y, m, d] = iso.split('-').map(Number);
      return `${d} ${MONTHS_GEN[m]}` + (y !== Number(now.slice(0, 4)) ? ` ${y}` : '');
    }

    function weekday(iso) {
      return (C.parseIso(iso).getUTCDay() + 6) % 7;
    }

    // ── обработчики ──

    function getState() {
      const now = C.today();
      const b = C.balances(state);
      const [y, m] = now.split('-').map(Number);
      const summ = C.summary(state, ...C.monthBounds(y, m));
      const { goals, main } = goalsPayload(b);
      return {
        currency: settings().currency,
        goal_pct: settings().goal_pct,
        harsh: harsh(),
        today: now,
        month: {
          title: monthTitle(y, m),
          income: summ.income,
          expense: summ.expense,
          saved: summ.saved,
          impulse: C.impulseTotal(summ),
        },
        balances: { wallet: b.wallet, pool: b.pool, goals: b.in_goals, total: b.total },
        safe_per_day: C.safePerDay(b.wallet, now),
        days_left: C.daysLeftInMonth(now),
        streak: C.impulseStreak(state, now),
        goals,
        main_goal: main,
        quote: C.pick(b.wallet < 0 ? 'overspent' : 'general', harsh()),
        ai: null,
        features: ['manage'],
        onboarded: Boolean(settings().onboarded) || state.txs.length > 0 || state.goals.length > 0,
      };
    }

    function getMonth(query) {
      const now = C.today();
      const y = query.y ? int(query.y, 'y') : Number(now.slice(0, 4));
      const m = query.m ? int(query.m, 'm') : Number(now.slice(5, 7));
      if (!(m >= 1 && m <= 12 && y >= 2000 && y <= 2200)) throw new ApiError(400, 'Некорректный месяц');
      const [first, last] = C.monthBounds(y, m);
      const summ = C.summary(state, first, last);
      const impulse = C.impulseTotal(summ);
      let noSpend = null;
      const firstUse = C.firstDay(state);
      if (firstUse !== null && first <= now) {
        const start = first > firstUse ? first : firstUse;
        const end = last < now ? last : now;
        const total = C.daysBetween(start, end) + 1;
        if (total > 0) noSpend = { days: Math.max(0, total - summ.spend_days), of: total };
      }
      // [доход, расход, сколько других изменений: пополнения целей, снятия, остаток]
      const days = {};
      for (const [d, v] of C.dayTotals(state, first, last)) days[d] = [v[0], v[1], 0];
      for (const t of state.txs) {
        if ((t.kind === 'transfer' || t.kind === 'adjust') && t.day >= first && t.day <= last) {
          (days[t.day] = days[t.day] || [0, 0, 0])[2] += 1;
        }
      }
      const [py, pm] = C.shiftMonth(y, m, -1);
      const [ny, nm] = C.shiftMonth(y, m, 1);
      const isPast = y * 12 + m < Number(now.slice(0, 4)) * 12 + Number(now.slice(5, 7));
      return {
        y,
        m,
        title: monthTitle(y, m),
        first_weekday: weekday(first),
        days_in_month: C.daysInMonth(y, m),
        today: now,
        days,
        summary: {
          income: summ.income,
          expense: summ.expense,
          saved: summ.saved,
          impulse,
          goal_purchases: summ.goal_purchases,
        },
        no_spend: noSpend,
        categories: summ.by_category.map(([key, total, count]) =>
          Object.assign(categoryJson(C.category(key)), {
            total,
            count,
            share: summ.expense ? Math.round((total / summ.expense) * 1e4) / 1e4 : 0,
          }),
        ),
        verdict: C.verdict(summ, impulse),
        prev: [py, pm],
        next: isPast ? [ny, nm] : null,
      };
    }

    function getDay(query) {
      const d = query.d || '';
      if (!C.parseIso(d)) throw new ApiError(400, 'Некорректная дата');
      const now = C.today();
      const txs = state.txs.filter((t) => t.day === d).sort((a, b) => a.created_at - b.created_at || a.id - b.id);
      return { date: d, title: `${WEEKDAYS[weekday(d)]}, ${fmtDay(d, now)}`, future: d > now, txs: txs.map(txJson) };
    }

    function getHistory(query) {
      const limit = Math.max(1, Math.min(200, query.limit ? int(query.limit, 'limit') : 60));
      return { txs: [...state.txs].sort((a, b) => b.id - a.id).slice(0, limit).map(txJson) };
    }

    function addTx(body) {
      const kind = body.kind;
      if (kind !== 'income' && kind !== 'expense') throw new ApiError(400, 'Выбери: расход или доход');
      const amount = C.parseAmount(String(body.amount == null ? '' : body.amount));
      if (amount === null) throw new ApiError(400, 'Не понял сумму. Например: 350, 1.5к, 2 000');
      const note = String(body.note || '').split(/\s+/).filter(Boolean).join(' ').slice(0, MAX_NOTE);
      const now = C.today();
      const day = body.day || now;
      if (!C.parseIso(day)) throw new ApiError(400, 'Некорректная дата');
      if (day > now) throw new ApiError(400, 'Будущее ещё не наступило. Записывай то, что уже случилось.');
      const chosen = body.category == null ? null : body.category;
      if (chosen !== null && !C.validCategory(chosen, kind)) throw new ApiError(400, 'Неизвестная категория');
      return change(() => {
        const cat = chosen || C.resolve(state, note, kind);
        if (chosen && note) C.remember(state, note, kind, chosen);
        let tx;
        let completed = [];
        if (kind === 'income') {
          const res = C.addIncome(state, { amount, category: cat || C.defaultFor(kind), note, day });
          tx = res.tx;
          completed = res.completed;
        } else {
          tx = C.addExpense(state, { amount, category: cat || C.defaultFor(kind), note, day });
        }
        const wallet = C.balances(state).wallet;
        return { tx: txJson(tx), quote: quoteFor(tx, wallet), wallet, completed: titles(completed), ai_pending: false };
      });
    }

    function deleteTx(id) {
      return change(() => {
        const tx = C.deleteTx(state, id);
        if (!tx) throw new ApiError(404, 'Запись уже удалена');
        return { ok: true };
      });
    }

    function patchTx(id, body) {
      const tx = C.getTx(state, id);
      if (!tx) throw new ApiError(404, 'Запись не найдена');
      const cat = body.category;
      if ((tx.kind !== 'income' && tx.kind !== 'expense') || !C.validCategory(cat, tx.kind)) {
        throw new ApiError(400, 'Эту категорию здесь выбрать нельзя');
      }
      return change(() => {
        if (!C.setCategory(state, id, cat)) throw new ApiError(409, 'Категорию этой записи менять нельзя');
        C.remember(state, tx.note, tx.kind, cat);
        return { tx: txJson(tx) };
      });
    }

    function goalTitle(value) {
      const title = String(value || '').split(/\s+/).filter(Boolean).join(' ');
      if (!title || title.length > MAX_TITLE) throw new ApiError(400, `Название цели — от 1 до ${MAX_TITLE} символов`);
      return title;
    }

    function goalTarget(value) {
      const target = C.parseAmount(String(value == null ? '' : value));
      if (target === null) throw new ApiError(400, 'Не понял сумму цели. Например: 150к или 2кк');
      return target;
    }

    function activeGoal(id) {
      const goal = C.getGoal(state, id);
      if (!goal || goal.status !== 'active') throw new ApiError(404, 'Этой цели уже нет');
      return goal;
    }

    function createGoal(body) {
      const title = goalTitle(body.title);
      const target = goalTarget(body.target);
      return change(() => ({ goal: goalJson(C.createGoal(state, title, target).id) }));
    }

    function patchGoal(id, body) {
      const goal = activeGoal(id);
      const fields = {};
      if (body.title !== undefined) fields.title = goalTitle(body.title);
      if (body.target !== undefined) fields.target = goalTarget(body.target);
      if (body.weight !== undefined) {
        fields.weight = Math.max(0, Math.min(C.MAX_WEIGHT, int(body.weight, 'weight')));
      }
      return change(() => {
        C.updateGoal(state, goal.id, fields);
        return { goal: goalJson(goal.id) };
      });
    }

    function deposit(id, body) {
      const goal = activeGoal(id);
      const src = body.src;
      if (!['wallet', 'pool', 'outside'].includes(src)) throw new ApiError(400, 'Выбери, откуда взять деньги');
      const amount = C.parseAmount(String(body.amount == null ? '' : body.amount));
      if (amount === null) throw new ApiError(400, 'Не понял сумму');
      const notes = {
        wallet: 'С расходов в цель',
        pool: 'Из свободных накоплений в цель',
        outside: 'Накоплено до бота',
      };
      return change(() => {
        const res = C.transfer(state, src, 'goal', amount, { dstGoal: goal.id, note: `${notes[src]} «${goal.title}»` });
        if (!res) throw new ApiError(409, 'Столько денег там нет');
        return {
          goal: goalJson(goal.id),
          tx_id: res.tx.id,
          amount: res.amount,
          quote: C.pick('deposit', harsh()),
          completed: titles(res.completed),
        };
      });
    }

    function withdraw(id, body) {
      const goal = activeGoal(id);
      const amount = C.parseAmount(String(body.amount == null ? '' : body.amount));
      if (amount === null) throw new ApiError(400, 'Не понял сумму');
      return change(() => {
        const res = C.transfer(state, 'goal', 'wallet', amount, { srcGoal: goal.id, note: `Снятие из «${goal.title}»` });
        if (!res) throw new ApiError(409, 'В цели столько нет');
        return { goal: goalJson(goal.id), amount: res.amount, quote: C.pick('withdraw', harsh()) };
      });
    }

    function buyGoal(id) {
      const goal = activeGoal(id);
      return change(() => {
        const res = C.buyGoal(state, goal.id);
        if (!res) throw new ApiError(409, 'В цели пока нет денег');
        return { title: goal.title, spent: res.spent, quote: C.pick('goal_bought', harsh()) };
      });
    }

    function deleteGoal(id, query) {
      const goal = activeGoal(id);
      const dest = query.dest || 'goals';
      if (!['goals', 'pool', 'wallet'].includes(dest)) throw new ApiError(400, 'Куда деть деньги цели?');
      return change(() => {
        const res = C.deleteGoal(state, goal.id, dest);
        for (const p of goal.photos) for (let i = 0; i < p.n; i++) photoKeys.delete(`p${p.id}_${i}`);
        return { ok: true, amount: res.amount, quote: C.pick('goal_deleted', harsh()), photos: goal.photos };
      });
    }

    function setSettings(body) {
      const next = {};
      if (body.goal_pct !== undefined) {
        const pct = int(body.goal_pct, 'goal_pct');
        if (pct < 0 || pct > 100) throw new ApiError(400, 'Процент — от 0 до 100');
        next.goal_pct = pct;
      }
      if (body.harsh !== undefined) next.harsh = int(body.harsh, 'harsh') >= 2 ? 2 : 1;
      if (body.onboarded !== undefined) next.onboarded = Boolean(body.onboarded);
      return change(() => {
        Object.assign(state.settings, next);
        return { ok: true };
      });
    }

    function setWallet(body) {
      const raw = String(body.amount == null ? '' : body.amount).trim();
      const value = raw === '0' ? 0 : C.parseAmount(raw);
      if (value === null) throw new ApiError(400, 'Не понял сумму. Например: 15000');
      return change(() => {
        C.setWallet(state, value);
        state.settings.onboarded = true;
        return { wallet: C.balances(state).wallet };
      });
    }

    /** «Хочу купить»: чего это стоит на самом деле. */
    function want(body) {
      const title = String(body.title || '').trim().slice(0, MAX_TITLE) || 'Покупка';
      const amount = C.parseAmount(String(body.amount == null ? '' : body.amount));
      if (amount === null) throw new ApiError(400, 'Не понял цену. Например: 12000');
      const now = C.today();
      const b = C.balances(state);
      const [incomePerDay, savingsPerDay] = C.dailyRates(state, now);
      const views = C.goalViews(state, 'active', b);
      const goal = C.mainGoal(views);
      const result = {
        title,
        amount,
        wallet: b.wallet,
        share: b.wallet > 0 ? amount / b.wallet : null,
        after: b.wallet - amount,
        per_day_after: C.safePerDay(b.wallet - amount, now),
        work_days: incomePerDay >= 1 ? amount / incomePerDay : null,
        goal: null,
        quote: C.pick('want', harsh()),
      };
      if (goal && !goal.reached) {
        const rate = savingsPerDay * (C.goalShares(views).get(goal.id) || 0);
        result.goal = {
          title: goal.title,
          closer_days: rate >= 1 ? amount / rate : null,
          progress: goal.progress,
          new_progress: (goal.saved + amount) / goal.target,
        };
      }
      return result;
    }

    function wantDecide(body) {
      const amount = C.parseAmount(String(body.amount == null ? '' : body.amount));
      if (amount === null) throw new ApiError(400, 'Не понял цену');
      const title = String(body.title || '').trim().slice(0, MAX_TITLE) || 'Покупка';
      if (body.decision === 'buy') {
        return change(() => {
          const cat = C.resolve(state, title, 'expense') || 'shopping';
          const tx = C.addExpense(state, { amount, category: cat, note: title });
          const wallet = C.balances(state).wallet;
          return { tx: txJson(tx), wallet, quote: C.pick('caved', harsh()) };
        });
      }
      if (body.decision === 'save') {
        return change(() => {
          const res = C.transfer(state, 'wallet', 'goals', amount, { note: `Не купил «${title}» — в цели` });
          if (!res) throw new ApiError(409, 'На расходах столько нет — просто не покупай, это уже победа.');
          return { amount: res.amount, completed: titles(res.completed), quote: C.pick('resisted', harsh()) };
        });
      }
      return { quote: C.pick('resisted', harsh()) };
    }

    // ── фото целей: сжатая картинка кусками в облаке ──

    async function encodePhoto(blob) {
      const source = await createImageBitmap(blob);
      const attempts = [
        [560, 0.62],
        [480, 0.55],
        [400, 0.5],
        [320, 0.45],
      ];
      let data = null;
      for (const [side, quality] of attempts) {
        const scale = Math.min(1, side / Math.max(source.width, source.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(source.width * scale));
        canvas.height = Math.max(1, Math.round(source.height * scale));
        canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
        data = canvas.toDataURL('image/jpeg', quality).split(',')[1];
        if (Math.ceil(data.length / CHUNK) <= MAX_PHOTO_CHUNKS) break;
      }
      if (source.close) source.close();
      return data;
    }

    async function uploadPhoto(id, blob) {
      const goal = activeGoal(id);
      if (!blob || !blob.size) throw new ApiError(415, 'Это не похоже на фото.');
      let data;
      try {
        data = await encodePhoto(blob);
      } catch (e) {
        throw new ApiError(415, 'Не получилось прочитать фото. Подойдут JPG, PNG или WEBP.');
      }
      const chunks = [];
      for (let i = 0; i < data.length; i += CHUNK) chunks.push(data.slice(i, i + CHUNK));
      if (chunks.length > MAX_PHOTO_CHUNKS) throw new ApiError(413, 'Фото слишком большое даже после сжатия.');
      const total = (await storage.keys()).length;
      if (total + chunks.length > MAX_KEYS - KEY_RESERVE) {
        throw new ApiError(507, 'Место для фото в облаке Telegram закончилось — удали старые фото целей.');
      }
      const photoId = C.newId(state);
      try {
        for (let i = 0; i < chunks.length; i++) {
          await storage.set(`p${photoId}_${i}`, chunks[i]);
          photoKeys.add(`p${photoId}_${i}`);
        }
      } catch (e) {
        await storage.remove(chunks.map((_, i) => `p${photoId}_${i}`)).catch(() => {});
        throw new ApiError(503, 'Не удалось сохранить фото в облако Telegram. Попробуй ещё раз.');
      }
      return change(() => {
        goal.photos.push({ id: photoId, n: chunks.length });
        return { goal: goalJson(goal.id), added: true };
      });
    }

    function findPhoto(photoId) {
      for (const g of state.goals) {
        const p = g.photos.find((x) => x.id === photoId);
        if (p) return { goal: g, photo: p };
      }
      return null;
    }

    async function getPhoto(photoId) {
      const found = findPhoto(photoId);
      if (!found) throw new ApiError(404, 'Фото не найдено');
      if (!photoCache.has(photoId)) {
        const keys = Array.from({ length: found.photo.n }, (_, i) => `p${photoId}_${i}`);
        const values = await storage.getMany(keys);
        const data = keys.map((k) => values[k] || '').join('');
        if (!data) throw new ApiError(404, 'Фото недоступно');
        const raw = atob(data);
        const buf = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
        photoCache.set(photoId, new Blob([buf], { type: 'image/jpeg' }));
      }
      return photoCache.get(photoId);
    }

    async function deletePhoto(photoId) {
      const found = findPhoto(photoId);
      if (!found) throw new ApiError(404, 'Фото уже удалено');
      const result = await change(() => {
        found.goal.photos = found.goal.photos.filter((p) => p.id !== photoId);
        return { goal: goalJson(found.goal.id) };
      });
      const keys = Array.from({ length: found.photo.n }, (_, i) => `p${photoId}_${i}`);
      await storage.remove(keys).catch(() => {});
      for (const k of keys) photoKeys.delete(k);
      photoCache.delete(photoId);
      return result;
    }

    async function wipe() {
      const keys = await storage.keys();
      for (let i = 0; i < keys.length; i += 100) await storage.remove(keys.slice(i, i + 100));
      photoCache.clear();
      for (const store of Object.values(stores)) {
        store.where.clear();
        store.written.clear();
      }
      settingsWritten = null;
      await load();
      return { ok: true };
    }

    // ── маршруты ──

    async function route(method, path, body, query) {
      if (!state || (method === 'GET' && path === '/api/state' && Date.now() - loadedAt > RELOAD_AFTER_MS)) {
        await load();
      }
      let m;
      if (method === 'GET') {
        if (path === '/api/state') return getState();
        if (path === '/api/categories') {
          return { expense: C.forKind('expense').map(categoryJson), income: C.forKind('income').map(categoryJson) };
        }
        if (path === '/api/classify') {
          const note = String(query.note || '').slice(0, MAX_NOTE);
          const kind = query.kind === 'income' ? 'income' : 'expense';
          return { category: C.resolve(state, note, kind), kind: C.guessKind(state, note) };
        }
        if (path === '/api/month') return getMonth(query);
        if (path === '/api/day') return getDay(query);
        if (path === '/api/history') return getHistory(query);
        if (path === '/api/quote') {
          const ctx = QUOTE_CONTEXTS.has(query.ctx) ? query.ctx : 'general';
          return { quote: C.pick(ctx, harsh()) };
        }
        if ((m = /^\/api\/photo\/(\d+)$/.exec(path))) return getPhoto(Number(m[1]));
      }
      if (method === 'POST') {
        if (path === '/api/tx') return addTx(body);
        if (path === '/api/goals') return createGoal(body);
        if (path === '/api/settings') return setSettings(body);
        if (path === '/api/wallet') return setWallet(body);
        if (path === '/api/want') return want(body);
        if (path === '/api/want/decide') return wantDecide(body);
        if (path === '/api/wipe') return wipe();
        if ((m = /^\/api\/goals\/(\d+)\/deposit$/.exec(path))) return deposit(Number(m[1]), body);
        if ((m = /^\/api\/goals\/(\d+)\/withdraw$/.exec(path))) return withdraw(Number(m[1]), body);
        if ((m = /^\/api\/goals\/(\d+)\/buy$/.exec(path))) return buyGoal(Number(m[1]));
        if ((m = /^\/api\/goals\/(\d+)\/photos$/.exec(path))) return uploadPhoto(Number(m[1]), body);
      }
      if (method === 'PATCH') {
        if ((m = /^\/api\/tx\/(\d+)$/.exec(path))) return patchTx(Number(m[1]), body);
        if ((m = /^\/api\/goals\/(\d+)$/.exec(path))) return patchGoal(Number(m[1]), body);
      }
      if (method === 'DELETE') {
        if ((m = /^\/api\/tx\/(\d+)$/.exec(path))) return deleteTx(Number(m[1]));
        if ((m = /^\/api\/photo\/(\d+)$/.exec(path))) return deletePhoto(Number(m[1]));
        if ((m = /^\/api\/goals\/(\d+)$/.exec(path))) {
          const res = await deleteGoal(Number(m[1]), query);
          const keys = [];
          for (const p of res.photos) for (let i = 0; i < p.n; i++) keys.push(`p${p.id}_${i}`);
          if (keys.length) await storage.remove(keys).catch(() => {});
          delete res.photos;
          return res;
        }
      }
      throw new ApiError(404, 'Нет такого действия');
    }

    /** Запрос как к серверу: {status, data}. Все изменения — строго по очереди. */
    function request(method, url, body) {
      const [path, qs] = url.split('?');
      const query = {};
      for (const [k, v] of new URLSearchParams(qs || '')) query[k] = v;
      return serial(async () => {
        try {
          return { status: 200, data: await route(method, path, body || {}, query) };
        } catch (e) {
          if (e instanceof ApiError) return { status: e.status, data: { error: e.message } };
          if (e instanceof C.TxBlocked) return { status: 409, data: { error: e.reason } };
          console.error(e);
          return { status: 500, data: { error: 'Что-то пошло не так. Попробуй ещё раз.' } };
        }
      });
    }

    return { request, load, state: () => state };
  }

  /** Подключение к Telegram: вызывает app.js, передавая мост к клиенту. */
  function connect(bridge) {
    return create(cloudStorage(bridge));
  }

  return { connect, create, memoryStorage, cloudStorage, PAGE_BYTES };
});
