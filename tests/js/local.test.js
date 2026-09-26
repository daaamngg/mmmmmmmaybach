// Версия без сервера: хранение в облаке Telegram и «сервер внутри приложения».
// Запуск: node --test tests/js   (перед этим: python -m finbot.webapp.pages)
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const L = require(path.join(__dirname, '..', '..', 'docs', 'local.js'));

function counted(store) {
  const calls = { set: [], remove: [] };
  const wrapped = Object.assign({}, store, {
    set: async (k, v) => {
      calls.set.push(k);
      return store.set(k, v);
    },
    remove: async (keys) => {
      calls.remove.push(...keys);
      return store.remove(keys);
    },
  });
  return { store: wrapped, calls };
}

async function ok(app, method, url, body) {
  const res = await app.request(method, url, body);
  assert.equal(res.status, 200, JSON.stringify(res.data));
  return res.data;
}

test('много записей раскладываются по страницам в пределах лимитов Telegram', async () => {
  const memory = L.memoryStorage();
  const app = L.create(memory);
  await ok(app, 'POST', '/api/goals', { title: 'Maybach', target: '20кк' });
  for (let i = 0; i < 300; i++) {
    const kind = i % 3 === 0 ? 'income' : 'expense';
    await ok(app, 'POST', '/api/tx', { kind, amount: String(100 + i), note: `запись номер ${i} шаурма` });
  }
  const pages = [...memory.data.keys()].filter((k) => /^t\d+$/.test(k));
  assert.ok(pages.length > 5, 'записи должны занять несколько страниц');
  for (const [key, value] of memory.data) assert.ok(value.length <= 4096 && /^[A-Za-z0-9_-]+$/.test(key), key);

  // Новая запись и удаление старой переписывают по одной странице, а не все.
  const { store, calls } = counted(memory);
  const again = L.create(store);
  const before = await ok(again, 'GET', '/api/state');
  assert.equal(before.month.expense > 0, true);
  await ok(again, 'POST', '/api/tx', { kind: 'expense', amount: '50', note: 'кофе' });
  assert.deepEqual(calls.set.filter((k) => k.startsWith('t')).length, 1);
  calls.set.length = 0;
  const history = await ok(again, 'GET', '/api/history?limit=200');
  const old = history.txs[history.txs.length - 1];
  await ok(again, 'DELETE', `/api/tx/${old.id}`);
  assert.equal(calls.set.filter((k) => k.startsWith('t')).length, 1);

  // После перезапуска всё на месте.
  const fresh = L.create(memory);
  const after = await ok(fresh, 'GET', '/api/state');
  assert.equal(after.balances.wallet, (await ok(again, 'GET', '/api/state')).balances.wallet);
  assert.equal((await ok(fresh, 'GET', '/api/history?limit=200')).txs.length, 200);
});

test('записи, цели, категории и защита от «денег из воздуха»', async () => {
  const memory = L.memoryStorage();
  const app = L.create(memory);
  const state0 = await ok(app, 'GET', '/api/state');
  assert.equal(state0.onboarded, false);
  assert.deepEqual(state0.features, ['manage']);

  const goal = (await ok(app, 'POST', '/api/goals', { title: 'Часы', target: '1000' })).goal;
  const income = await ok(app, 'POST', '/api/tx', { kind: 'income', amount: '2 000', note: 'зарплата' });
  assert.equal(income.tx.category, 'salary');
  assert.equal(income.tx.saved, 160000); // 80% в цели (1000 — в «Часы», 600 — в копилку)
  assert.deepEqual(income.completed, ['Часы']);
  assert.equal(income.wallet, 40000);

  const unknown = await ok(app, 'POST', '/api/tx', { kind: 'expense', amount: '500', note: 'бенз 95' });
  assert.equal(unknown.tx.category, 'other');
  await ok(app, 'PATCH', `/api/tx/${unknown.tx.id}`, { category: 'transport' });
  const guess = await ok(app, 'GET', '/api/classify?note=' + encodeURIComponent('бенз 92') + '&kind=expense');
  assert.equal(guess.category, 'transport'); // запомнил по главному слову

  const bad = await app.request('POST', '/api/tx', { kind: 'expense', amount: 'много' });
  assert.equal(bad.status, 400);
  assert.match(bad.data.error, /сумму/);
  const future = await app.request('POST', '/api/tx', { kind: 'expense', amount: '1', day: '2999-01-01' });
  assert.equal(future.status, 400);

  // Деньги из дохода ушли в цель и потрачены покупкой — удалить доход нельзя.
  const bought = await ok(app, 'POST', `/api/goals/${goal.id}/buy`);
  assert.equal(bought.spent, 100000);
  const blocked = await app.request('DELETE', `/api/tx/${income.tx.id}`);
  assert.equal(blocked.status, 409);
  assert.match(blocked.data.error, /закрытой целью/);

  const state = await ok(app, 'GET', '/api/state');
  assert.equal(state.goals.length, 0);
  assert.equal(state.balances.pool, 60000);
  assert.equal(state.onboarded, true);

  const missing = await app.request('GET', '/api/nope');
  assert.equal(missing.status, 404);
});

test('управление целью, настройки, остаток и «Хочу купить»', async () => {
  const app = L.create(L.memoryStorage());
  await ok(app, 'POST', '/api/wallet', { amount: '10000' });
  const goal = (await ok(app, 'POST', '/api/goals', { title: 'Квартира', target: '5кк' })).goal;
  const dep = await ok(app, 'POST', `/api/goals/${goal.id}/deposit`, { amount: '3000', src: 'wallet' });
  assert.equal(dep.goal.saved, 300000);
  const tooMuch = await app.request('POST', `/api/goals/${goal.id}/withdraw`, { amount: '5000' });
  assert.equal(tooMuch.status, 409);
  const wd = await ok(app, 'POST', `/api/goals/${goal.id}/withdraw`, { amount: '1000' });
  assert.equal(wd.goal.saved, 200000);
  const edited = await ok(app, 'PATCH', `/api/goals/${goal.id}`, { title: 'Своя квартира', target: '6кк', weight: 3 });
  assert.equal(edited.goal.title, 'Своя квартира');
  assert.equal(edited.goal.target, 600000000);
  assert.equal(edited.goal.weight, 3);

  await ok(app, 'POST', '/api/settings', { goal_pct: 70, harsh: 2 });
  const inc = await ok(app, 'POST', '/api/tx', { kind: 'income', amount: '1000' });
  assert.equal(inc.tx.saved, 70000);
  assert.equal((await ok(app, 'GET', '/api/state')).harsh, 2);

  const w = await ok(app, 'POST', '/api/want', { title: 'кроссовки', amount: '4000' });
  assert.equal(w.amount, 400000);
  assert.equal(w.goal.title, 'Своя квартира');
  const saved = await ok(app, 'POST', '/api/want/decide', { title: 'кроссовки', amount: '4000', decision: 'save' });
  assert.equal(saved.amount, 400000);
  const bought = await ok(app, 'POST', '/api/want/decide', { title: 'кроссовки', amount: '500', decision: 'buy' });
  assert.equal(bought.tx.category, 'clothes');

  // В календаре видно любое изменение за день, а не только доходы и расходы.
  const now = (await ok(app, 'GET', '/api/state')).today;
  const [y, m] = now.split('-').map(Number);
  const month = await ok(app, 'GET', `/api/month?y=${y}&m=${m}`);
  assert.ok(month.days[now][2] >= 3, JSON.stringify(month.days[now])); // остаток, пополнение, снятие…
  const day = await ok(app, 'GET', `/api/day?d=${now}`);
  const kinds = new Set(day.txs.map((t) => t.kind));
  for (const kind of ['adjust', 'transfer', 'income', 'expense']) assert.ok(kinds.has(kind), kind);

  const del = await ok(app, 'DELETE', `/api/goals/${goal.id}?dest=wallet`);
  assert.ok(del.amount > 0);
  assert.equal((await ok(app, 'GET', '/api/state')).goals.length, 0);

  await ok(app, 'POST', '/api/wipe');
  const empty = await ok(app, 'GET', '/api/state');
  assert.equal(empty.balances.total, 0);
  assert.equal(empty.goal_pct, 80);
});
