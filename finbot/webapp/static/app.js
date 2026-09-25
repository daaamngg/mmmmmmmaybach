/* Мини-приложение бота: баланс, цели с фото, календарь, история, быстрая запись.
 *
 * Без фреймворков и внешних скриптов — открывается мгновенно даже на слабом интернете.
 * Весь текст выводится через textContent: данные никогда не превращаются в HTML.
 */
'use strict';

(() => {
  // ═══════════════════════ Telegram: запуск, тема, события ═══════════════════════

  const LAUNCH_KEY = 'finbot.launch';

  function urlDecode(s) {
    try {
      return decodeURIComponent(s.replace(/\+/g, '%20'));
    } catch (e) {
      return s;
    }
  }

  function parseQuery(query) {
    const out = {};
    for (const part of (query || '').split('&')) {
      if (!part) continue;
      const i = part.indexOf('=');
      out[urlDecode(i < 0 ? part : part.slice(0, i))] = i < 0 ? '' : urlDecode(part.slice(i + 1));
    }
    return out;
  }

  /** Telegram передаёт подпись и тему в адресе после «#». Храним копию на случай перезагрузки. */
  function readLaunchParams() {
    let hash = '';
    try {
      hash = location.hash.slice(1);
    } catch (e) {
      /* нет доступа к адресу */
    }
    const q = hash.indexOf('?');
    if (q >= 0) hash = hash.slice(q + 1);
    const params = {};
    const fresh = Object.assign(parseQuery(location.search.slice(1)), parseQuery(hash));
    for (const [k, v] of Object.entries(fresh)) if (k.startsWith('tgWebApp')) params[k] = v;
    try {
      const saved = JSON.parse(sessionStorage.getItem(LAUNCH_KEY) || '{}') || {};
      for (const [k, v] of Object.entries(saved)) if (params[k] === undefined) params[k] = v;
      sessionStorage.setItem(LAUNCH_KEY, JSON.stringify(params));
    } catch (e) {
      /* приватный режим — обойдёмся */
    }
    return params;
  }

  const launch = readLaunchParams();
  const initData = launch.tgWebAppData || '';

  const inIframe = (() => {
    try {
      return window.parent != null && window !== window.parent;
    } catch (e) {
      return true;
    }
  })();

  /** Команда клиенту Telegram (тот же протокол, что у официального telegram-web-app.js). */
  function post(type, data) {
    const payload = data === undefined ? '' : data;
    try {
      if (window.TelegramWebviewProxy !== undefined) {
        window.TelegramWebviewProxy.postEvent(type, JSON.stringify(payload));
      } else if (window.external && 'notify' in window.external) {
        window.external.notify(JSON.stringify({ eventType: type, eventData: payload }));
      } else if (inIframe) {
        window.parent.postMessage(JSON.stringify({ eventType: type, eventData: payload }), '*');
      }
    } catch (e) {
      /* старый клиент — просто без этой функции */
    }
  }

  const handlers = {};
  function onEvent(type, fn) {
    (handlers[type] = handlers[type] || []).push(fn);
  }
  function receiveEvent(type, data) {
    for (const fn of handlers[type] || []) {
      try {
        fn(data);
      } catch (e) {
        console.error(e);
      }
    }
  }
  window.Telegram = window.Telegram || {};
  window.Telegram.WebView = Object.assign(window.Telegram.WebView || {}, { receiveEvent, postEvent: post, onEvent });
  window.TelegramGameProxy_receiveEvent = receiveEvent;
  window.TelegramGameProxy = { receiveEvent };
  if (inIframe) {
    window.addEventListener('message', (event) => {
      if (event.source !== window.parent) return;
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (!msg || !msg.eventType) return;
      if (msg.eventType === 'reload_iframe') {
        post('iframe_will_reload');
        location.reload();
        return;
      }
      receiveEvent(msg.eventType, msg.eventData);
    });
    post('iframe_ready', { reload_supported: true });
  }

  function parseColor(value) {
    if (typeof value !== 'string') return null;
    const c = value.trim().toLowerCase();
    if (/^#[0-9a-f]{6}$/.test(c)) return c;
    if (/^#[0-9a-f]{3}$/.test(c)) return '#' + c[1] + c[1] + c[2] + c[2] + c[3] + c[3];
    const m = c.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (m) return '#' + [m[1], m[2], m[3]].map((x) => Math.min(255, +x).toString(16).padStart(2, '0')).join('');
    return null;
  }

  function isDark(hex) {
    const n = parseInt(hex.slice(1), 16);
    const r = n >> 16;
    const g = (n >> 8) & 255;
    const b = n & 255;
    return Math.sqrt(0.299 * r * r + 0.587 * g * g + 0.114 * b * b) < 120;
  }

  function applyTheme(params) {
    if (!params || typeof params !== 'object') return;
    const theme = {};
    for (const [key, value] of Object.entries(params)) {
      const color = parseColor(value);
      if (color) theme[key] = color;
    }
    // Как в официальном SDK: на iOS фон и «второй» фон бывают одинаковыми.
    if (theme.bg_color === '#1c1c1d' && theme.secondary_bg_color === theme.bg_color) theme.secondary_bg_color = '#2c2c2e';
    const root = document.documentElement;
    for (const [key, color] of Object.entries(theme)) root.style.setProperty('--tg-theme-' + key.replace(/_/g, '-'), color);
    if (theme.bg_color) root.dataset.scheme = isDark(theme.bg_color) ? 'dark' : 'light';
    if (theme.secondary_bg_color) {
      post('web_app_set_header_color', { color_key: 'secondary_bg_color' });
      post('web_app_set_background_color', { color: theme.secondary_bg_color });
    }
  }

  if (launch.tgWebAppThemeParams) {
    try {
      applyTheme(JSON.parse(launch.tgWebAppThemeParams));
    } catch (e) {
      /* тема не критична */
    }
  }
  onEvent('theme_changed', (data) => applyTheme(data && data.theme_params));

  const HAPTICS = {
    tap: { type: 'impact', impact_style: 'light' },
    heavy: { type: 'impact', impact_style: 'heavy' },
    select: { type: 'selection_change' },
    success: { type: 'notification', notification_type: 'success' },
    warning: { type: 'notification', notification_type: 'warning' },
    error: { type: 'notification', notification_type: 'error' },
  };
  function haptic(kind) {
    post('web_app_trigger_haptic_feedback', HAPTICS[kind] || HAPTICS.tap);
  }

  // ═══════════════════════ форматирование ═══════════════════════

  const NBSP = ' ';
  const MINUS = '−';
  const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const MONTHS_SHORT = ['янв.', 'февр.', 'марта', 'апр.', 'мая', 'июня', 'июля', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];
  const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];

  function currency() {
    return (S.state && S.state.currency) || '₽';
  }

  /** Копейки → «12 345» или «10,50». Минус — настоящий «−». */
  function amount(value) {
    const abs = Math.abs(value);
    const minor = abs % 100;
    let s = String(Math.floor(abs / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
    if (minor) s += ',' + String(minor).padStart(2, '0');
    return (value < 0 ? MINUS : '') + s;
  }

  function money(value, sign) {
    const prefix = sign === '+' && value > 0 ? '+' : sign === '-' && value > 0 ? MINUS : '';
    return prefix + amount(value) + NBSP + currency();
  }

  function oneDecimal(x) {
    return (Math.round(x * 10) / 10).toString().replace('.', ',');
  }

  /** Коротко для клеток календаря: 350, 1,2к, 15к, 1,5м. */
  function short(value) {
    const r = Math.abs(value) / 100;
    if (r < 1000) return String(Math.round(r));
    if (r < 9950) return oneDecimal(r / 1000) + 'к';
    if (r < 999500) return Math.round(r / 1000) + 'к';
    if (r < 9950000) return oneDecimal(r / 1e6) + 'м';
    return Math.round(r / 1e6) + 'м';
  }

  function pct(fraction) {
    const p = Math.max(0, fraction) * 100;
    if (p === 0) return '0%';
    if (p < 0.1) return '<0,1%';
    if (p < 10) return oneDecimal(p) + '%';
    return Math.round(p) + '%';
  }

  function plural(n, one, few, many) {
    const n100 = Math.abs(n) % 100;
    const n10 = n100 % 10;
    if (n100 > 10 && n100 < 20) return many;
    if (n10 > 1 && n10 < 5) return few;
    if (n10 === 1) return one;
    return many;
  }

  function duration(days) {
    const d = Math.max(1, Math.round(days));
    if (d < 45) return `${d} ${plural(d, 'день', 'дня', 'дней')}`;
    let months = Math.round(d / 30.44);
    if (months < 12) return `${months} мес.`;
    const years = Math.floor(months / 12);
    months %= 12;
    return `${years} ${plural(years, 'год', 'года', 'лет')}` + (months ? ` ${months} мес.` : '');
  }

  function clip(text, limit) {
    return text.length <= limit ? text : text.slice(0, limit - 1).trimEnd() + '…';
  }

  function isoToDate(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  function addDays(iso, n) {
    const dt = isoToDate(iso);
    dt.setUTCDate(dt.getUTCDate() + n);
    return dt.toISOString().slice(0, 10);
  }

  function today() {
    if (S.state && S.state.today) return S.state.today;
    const now = new Date();
    return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  function dayTitle(iso) {
    const t = today();
    if (iso === t) return 'Сегодня';
    if (iso === addDays(t, -1)) return 'Вчера';
    const dt = isoToDate(iso);
    const year = dt.getUTCFullYear() !== isoToDate(t).getUTCFullYear() ? ` ${dt.getUTCFullYear()}` : '';
    return `${dt.getUTCDate()} ${MONTHS_GEN[dt.getUTCMonth()]}${year}, ${WEEKDAYS[(dt.getUTCDay() + 6) % 7].toLowerCase()}`;
  }

  function shortDate(iso) {
    const dt = isoToDate(iso);
    return `${dt.getUTCDate()} ${MONTHS_SHORT[dt.getUTCMonth()]}`;
  }

  /** Быстрый разбор суммы для подсказки (точно сумму считает сервер). */
  function looseAmount(text) {
    const s = String(text || '')
      .toLowerCase()
      .replace(/[\s  ']/g, '')
      .replace(/(₽|руб\.?|р\.?)$/, '');
    const m = s.match(/^(\d{1,12})(?:[.,](\d{1,2}))?(кк|kk|млн|тыс|к|k|т)?$/);
    if (!m) return null;
    const mult = !m[3] ? 1 : /^(кк|kk|млн)$/.test(m[3]) ? 1e6 : 1e3;
    const fraction = m[2] ? Number(m[2].padEnd(2, '0')) / 100 : 0;
    const value = Math.round((Number(m[1]) + fraction) * mult * 100);
    return value > 0 && value <= 1e13 ? value : null;
  }

  // ═══════════════════════ DOM ═══════════════════════

  /** h('div.card.hero', {text, onclick, css: {'--heat': 0.3}}, ...дети) — только безопасный текст. */
  function h(tag, props, ...children) {
    const parts = tag.split('.');
    const el = document.createElement(parts[0] || 'div');
    if (parts.length > 1) el.className = parts.slice(1).join(' ');
    if (el.tagName === 'BUTTON') el.type = 'button';
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'text') el.textContent = value;
      else if (key === 'css') for (const [name, v] of Object.entries(value)) el.style.setProperty(name, String(v));
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : String(value));
    }
    append(el, children);
    return el;
  }

  function append(el, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false || child === '') continue;
      if (Array.isArray(child)) append(el, child);
      else el.append(child instanceof Node ? child : String(child));
    }
  }

  function field(label, input) {
    return h('label.field', null, h('span', { text: label }), input);
  }

  function tile(label, value, cls) {
    return h('div.tile', null, h('div.label', { text: label }), h('div.value.num' + (cls ? '.' + cls : ''), { text: value }));
  }

  function progress(fraction, done) {
    const width = Math.max(0, Math.min(100, fraction * 100));
    return h('div.bar' + (done ? '.done' : ''), null, h('i', { css: { width: width.toFixed(1) + '%' } }));
  }

  function clickable(tag, props, ...children) {
    const onclick = props.onclick;
    const el = h(tag, Object.assign({}, props, { role: 'button', tabindex: 0 }), ...children);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onclick(e);
      }
    });
    return el;
  }

  function onEnter(input, action) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        action();
      }
    });
  }

  // ═══════════════════════ сервер ═══════════════════════

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, opts = {}) {
    const headers = { Authorization: 'tma ' + initData };
    const init = { method: opts.method || 'GET', headers, cache: 'no-store' };
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.json);
    } else if (opts.body !== undefined) {
      headers['Content-Type'] = opts.type || 'application/octet-stream';
      init.body = opts.body;
    }
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), opts.timeout || 20000) : null;
    if (controller) init.signal = controller.signal;
    let resp;
    try {
      resp = await fetch(path, init);
      if (opts.blob && resp.ok) return await resp.blob();
    } catch (e) {
      throw new ApiError(0, 'Нет связи с сервером. Проверь интернет и попробуй ещё раз.');
    } finally {
      if (timer) clearTimeout(timer);
    }
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      /* не JSON — ниже будет общая ошибка */
    }
    if (!resp.ok) {
      const message = (data && data.error) || `Сервер ответил ошибкой ${resp.status}. Попробуй ещё раз.`;
      const err = new ApiError(resp.status, message);
      if (resp.status === 401) fatal('⏳', 'Сессия устарела', message);
      else if (resp.status === 403) fatal('🔒', 'Вход закрыт', message);
      if (resp.status === 401 || resp.status === 403) err.fatal = true;
      throw err;
    }
    return data;
  }

  async function loadState() {
    S.state = await api('/api/state');
    return S.state;
  }

  async function loadCategories() {
    if (!S.cats) S.cats = await api('/api/categories');
    return S.cats;
  }

  const photoCache = new Map();

  /** Фото целей грузим с подписью Telegram, поэтому через blob, а не прямой ссылкой. */
  function photoUrl(id) {
    if (!photoCache.has(id)) {
      const promise = api('/api/photo/' + id, { blob: true, timeout: 60000 })
        .then((blob) => URL.createObjectURL(blob))
        .catch(() => {
          photoCache.delete(id);
          return null;
        });
      photoCache.set(id, promise);
    }
    return photoCache.get(id);
  }

  function forgetPhoto(id) {
    const cached = photoCache.get(id);
    photoCache.delete(id);
    if (cached) cached.then((url) => url && URL.revokeObjectURL(url));
  }

  function photoInto(box, id) {
    photoUrl(id).then((url) => {
      if (url) box.replaceChildren(h('img', { src: url, alt: '', decoding: 'async' }));
    });
  }

  /** Фото с телефона весят мегабайты — ужимаем до разумного размера прямо на телефоне. */
  async function shrinkImage(file, maxSide = 1600) {
    try {
      let source;
      let width;
      let height;
      if (typeof createImageBitmap === 'function') {
        source = await createImageBitmap(file, { imageOrientation: 'from-image' });
        width = source.width;
        height = source.height;
      } else {
        source = await new Promise((resolve, reject) => {
          const url = URL.createObjectURL(file);
          const img = new Image();
          img.onload = () => {
            URL.revokeObjectURL(url);
            resolve(img);
          };
          img.onerror = (e) => {
            URL.revokeObjectURL(url);
            reject(e);
          };
          img.src = url;
        });
        width = source.naturalWidth;
        height = source.naturalHeight;
      }
      const scale = Math.min(1, maxSide / Math.max(width, height));
      if (scale === 1 && file.size < 1500000 && /jpe?g/i.test(file.type)) return file;
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
      canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
      if (source.close) source.close();
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.86));
      return blob || file;
    } catch (e) {
      return file;
    }
  }

  // ═══════════════════════ каркас ═══════════════════════

  const S = {
    state: null,
    cats: null,
    tab: 'home',
    cal: null,
    histLimit: 60,
    seq: 0,
    sheetSeq: 0,
    sheetBack: null,
    started: false,
  };

  const $view = document.getElementById('view');
  const $tabs = document.getElementById('tabs');
  const $fab = document.getElementById('fab');
  const $sheet = document.getElementById('sheet');
  const $sheetBody = document.getElementById('sheetBody');
  const $sheetPanel = $sheet.querySelector('.sheet-panel');
  const $toast = document.getElementById('toast');

  let backVisible = false;
  function updateBackButton() {
    const visible = S.started && (!$sheet.hidden || S.tab !== 'home');
    if (visible === backVisible) return;
    backVisible = visible;
    post('web_app_setup_back_button', { is_visible: visible });
  }

  function goBack() {
    if (!$sheet.hidden) {
      if (S.sheetBack) S.sheetBack();
      else closeSheet();
    } else if (S.tab !== 'home') {
      setTab('home');
    }
  }
  onEvent('back_button_pressed', goBack);

  function openSheet(nodes, back) {
    S.sheetBack = back || null;
    $sheetBody.replaceChildren(...[].concat(nodes).filter(Boolean));
    if ($sheet.hidden) {
      $sheet.hidden = false;
      document.body.classList.add('locked');
    }
    $sheetPanel.scrollTop = 0;
    updateBackButton();
    return ++S.sheetSeq;
  }

  function closeSheet() {
    if ($sheet.hidden) return;
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    $sheet.hidden = true;
    $sheetBody.replaceChildren();
    document.body.classList.remove('locked');
    S.sheetBack = null;
    S.sheetSeq++;
    updateBackButton();
  }

  $sheet.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) closeSheet();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') goBack();
  });

  let toastTimer = null;
  function toast(title, lines, ms) {
    $toast.replaceChildren(h('b', { text: title }), ...(lines || []).filter(Boolean).map((line) => h('div.q', { text: line })));
    $toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      $toast.hidden = true;
    }, ms || 5000);
  }
  $toast.addEventListener('click', () => {
    $toast.hidden = true;
  });

  function fatal(emoji, title, text, retry) {
    closeSheet();
    $tabs.hidden = true;
    $fab.hidden = true;
    S.started = false;
    updateBackButton();
    $view.replaceChildren(
      h(
        'div.fatal',
        null,
        h('div.big-emoji', { text: emoji }),
        h('h2', { text: title }),
        h('p', { text }),
        retry && h('button.btn.primary', { text: 'Повторить', onclick: retry }),
      ),
    );
  }

  function errorBlock(err, retry) {
    return h(
      'div.empty',
      null,
      h('p', { text: err.message || 'Что-то пошло не так.' }),
      h('button.btn.ghost', { text: 'Повторить', onclick: retry || (() => refresh()) }),
    );
  }

  const LOADERS = {
    calendar: () => api(S.cal ? `/api/month?y=${S.cal.y}&m=${S.cal.m}` : '/api/month'),
    history: () => api('/api/history?limit=' + S.histLimit),
  };

  const RENDER = {
    home: (st) => renderHome(st),
    goals: (st) => renderGoals(st),
    calendar: (st, month) => renderCalendar(month),
    history: (st, data) => renderHistory(data),
  };

  /** Перерисовать текущую вкладку. quiet — не показывать «Загрузка…», пока ждём. */
  async function refresh(opts = {}) {
    const seq = ++S.seq;
    const tab = S.tab;
    const slow = opts.quiet
      ? null
      : setTimeout(() => {
          if (seq === S.seq) $view.replaceChildren(h('div.loading', { text: 'Загрузка…' }));
        }, 200);
    try {
      const loader = LOADERS[tab];
      const [st, extra] = await Promise.all([loadState(), loader ? loader() : null]);
      if (seq !== S.seq) return;
      $view.replaceChildren(RENDER[tab](st, extra));
    } catch (e) {
      if (seq === S.seq && !e.fatal) $view.replaceChildren(errorBlock(e));
    } finally {
      clearTimeout(slow);
    }
  }

  function setTab(tab) {
    if (!RENDER[tab]) return;
    if (tab !== S.tab) haptic('select');
    S.tab = tab;
    for (const b of $tabs.querySelectorAll('.tab')) b.classList.toggle('active', b.dataset.tab === tab);
    window.scrollTo(0, 0);
    updateBackButton();
    refresh();
  }

  $tabs.addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]');
    if (b) setTab(b.dataset.tab);
  });
  $fab.addEventListener('click', () => openAdd({}));

  // ═══════════════════════ главная ═══════════════════════

  function renderHome(st) {
    const b = st.balances;
    let sub;
    if (b.wallet > 0) {
      sub = `≈ ${money(st.safe_per_day)} в день · ещё ${st.days_left} ${plural(st.days_left, 'день', 'дня', 'дней')} до конца месяца`;
    } else if (b.wallet < 0) {
      sub = '🚨 Ты в минусе. Сейчас ты тратишь деньги своих целей.';
    } else {
      sub = `Пусто. С каждого дохода сюда приходит ${100 - st.goal_pct}%, остальное — в цели.`;
    }
    const m = st.month;
    const quoteText = h('div.quote-text', { text: st.quote });
    const moreQuote = h('button.link', {
      text: 'Ещё жёстче →',
      onclick: async () => {
        haptic('tap');
        try {
          quoteText.textContent = (await api('/api/quote?ctx=general')).quote;
        } catch (e) {
          /* цитата не критична */
        }
      },
    });
    const goal = st.goals.find((g) => g.id === st.main_goal);
    return h(
      'div',
      null,
      h(
        'section.card.hero' + (b.wallet < 0 ? '.negative' : ''),
        null,
        h('div.label', { text: 'На расходы' }),
        h('div.big.num', { text: money(b.wallet) }),
        h('div.sub', { text: sub }),
        h('div.row3', null, tile('В целях', money(b.goals), 'gold'), tile('Копилка', money(b.pool)), tile('Всего', money(b.total))),
      ),
      h(
        'div.actions',
        null,
        h('button.btn.expense', { text: '− Расход', onclick: () => openAdd({ kind: 'expense' }) }),
        h('button.btn.income', { text: '+ Доход', onclick: () => openAdd({ kind: 'income' }) }),
      ),
      h(
        'section.card',
        null,
        h('div.card-title', { text: m.title }),
        h('div.row3', null, tile('Заработал', money(m.income), 'pos'), tile('Потратил', money(m.expense), 'neg'), tile('Отложил', money(m.saved), 'gold')),
        m.impulse > 0 && h('div.note.warn', { text: `🍔 На хотелки ушло ${money(m.impulse)} — ${pct(m.impulse / Math.max(1, m.expense))} всех трат.` }),
        streakNote(st.streak),
      ),
      goal ? mainGoalCard(goal) : noGoalsCard(),
      h('section.card.quote', null, quoteText, moreQuote),
      st.ai && h('p.hint.foot', { text: '🤖 Нейросеть: ' + st.ai }),
    );
  }

  function streakNote(streak) {
    if (streak === null || streak === undefined) return null;
    if (streak === 0) return h('div.note.streak', { text: '🔥 Серия без хотелок обнулена сегодня. Завтра — заново.' });
    return h('div.note.streak', { text: `🔥 Без трат на хотелки: ${streak} ${plural(streak, 'день', 'дня', 'дней')} подряд` });
  }

  function mainGoalCard(g) {
    const cover = h('div.goal-cover', { text: g.reached ? '🏆' : '🎯' });
    if (g.photos.length) photoInto(cover, g.photos[0]);
    return clickable(
      'section.card',
      { onclick: () => openGoal(g.id) },
      h('div.card-title', { text: 'Главная цель' }),
      cover,
      h('div.goal-title', null, g.title, g.reached && h('span.badge', { text: 'Собрано' })),
      progress(g.progress, g.reached),
      h('div.goal-meta', { text: `${money(g.saved)} из ${money(g.target)} · ${pct(g.progress)}` }),
      h('div.goal-meta', { text: etaText(g) }),
    );
  }

  function noGoalsCard() {
    return h(
      'section.card',
      null,
      h('div.card-title', { text: 'Цель' }),
      h('p', { text: 'У тебя нет цели. Без цели деньги утекают на ерунду — поставь её прямо сейчас.' }),
      h('button.btn.primary', { text: '🎯 Поставить цель', onclick: openNewGoal }),
    );
  }

  function etaText(g) {
    if (g.reached) return 'Цель собрана. Забирай — ты это заработал. 🏆';
    const share = g.share > 0 && g.share < 1 ? ` · получает ${pct(g.share)} отчислений` : '';
    if (g.eta_days) return `≈ ещё ${duration(g.eta_days)} при нынешнем темпе${share}`;
    return `Осталось ${money(g.remaining)}${share}`;
  }

  // ═══════════════════════ цели ═══════════════════════

  function renderGoals(st) {
    const rows = st.goals.map((g) => {
      const thumb = h('div.thumb', { text: g.reached ? '🏆' : '🎯' });
      if (g.photos.length) photoInto(thumb, g.photos[0]);
      return clickable(
        'section.card',
        { onclick: () => openGoal(g.id) },
        h(
          'div.goal-row',
          null,
          thumb,
          h(
            'div.goal-body',
            null,
            h('div.goal-title', null, g.title, g.reached && h('span.badge', { text: 'Собрано' })),
            progress(g.progress, g.reached),
            h('div.goal-meta', { text: `${money(g.saved)} из ${money(g.target)} · ${pct(g.progress)}` }),
            h('div.goal-meta', { text: etaText(g) }),
          ),
        ),
      );
    });
    return h(
      'div',
      null,
      rows.length ? rows : h('div.empty', { text: 'Целей пока нет. Человек без цели просто тратит деньги.' }),
      h('button.btn.primary', { text: '＋ Новая цель', onclick: openNewGoal }),
      h('p.hint.foot', {
        text: `С каждого дохода ${st.goal_pct}% автоматически уходит в цели. Изменить, снять или купить цель — в чате с ботом.`,
      }),
    );
  }

  function openGoal(id, photoFailure) {
    const st = S.state;
    const g = st && st.goals.find((x) => x.id === id);
    if (!g) {
      closeSheet();
      return;
    }
    const b = st.balances;
    const photoError = h('p.error-text', { text: photoFailure || '' });

    const fileInput = h('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true });
    const addPhoto = h('button.btn.ghost', {
      text: g.photos.length ? '📷 Добавить ещё фото' : '📷 Добавить фото мечты',
      onclick: () => fileInput.click(),
    });
    fileInput.addEventListener('change', () => uploadPhotos(Array.from(fileInput.files || [])));

    async function uploadPhotos(files) {
      fileInput.value = '';
      const list = files.slice(0, 10);
      if (!list.length) return;
      const token = S.sheetSeq;
      addPhoto.disabled = true;
      photoError.textContent = '';
      let added = 0;
      let failure = null;
      try {
        for (const file of list) {
          addPhoto.textContent = `Загружаю ${added + 1} из ${list.length}…`;
          const blob = await shrinkImage(file);
          await api(`/api/goals/${g.id}/photos`, { method: 'POST', body: blob, type: blob.type, timeout: 120000 });
          added++;
        }
        haptic('success');
        toast(added > 1 ? `📸 Добавлено фото: ${added}` : '📸 Фото добавлено', ['Смотри на мечту каждый день. Каждая трата — шаг к ней или от неё.']);
      } catch (e) {
        if (e.fatal) return;
        haptic('error');
        failure = e.message;
      }
      if (added) {
        try {
          await loadState();
        } catch (e) {
          if (e.fatal) return;
        }
        refresh({ quiet: true });
      }
      if (token === S.sheetSeq) openGoal(g.id, failure);
    }

    let photos;
    if (g.photos.length) {
      photos = h(
        'div.carousel',
        null,
        g.photos.map((pid) => {
          const img = h('img', { alt: g.title, decoding: 'async', onclick: () => openPhoto(g, pid) });
          photoUrl(pid).then((url) => {
            if (url) img.src = url;
          });
          return img;
        }),
      );
    } else {
      photos = h('div.goal-cover', { text: g.reached ? '🏆' : '🎯' });
    }

    let depositBlock;
    if (g.reached) {
      depositBlock = h('div.note', { text: '🏆 Цель собрана! Отметить покупку — в чате с ботом, раздел «🎯 Цели».' });
    } else {
      let src = b.pool > 0 ? 'pool' : 'wallet';
      const sources = [
        ['wallet', `💸 С расходов · ${money(b.wallet)}`],
        ['pool', `🐷 Из копилки · ${money(b.pool)}`],
        ['outside', '🏦 Отложено раньше'],
      ].filter(([key]) => key !== 'pool' || b.pool > 0);
      const srcChips = h('div.chips.gap-top');
      const renderSources = () =>
        srcChips.replaceChildren(
          ...sources.map(([key, label]) =>
            h('button.chip' + (key === src ? '.on' : ''), {
              text: label,
              onclick: () => {
                haptic('select');
                src = key;
                renderSources();
              },
            }),
          ),
        );
      renderSources();
      const amountInput = h('input.input.num', { inputmode: 'decimal', placeholder: 'Сколько положить', autocomplete: 'off', enterkeyhint: 'done', maxlength: 20 });
      const depositError = h('p.error-text');
      const depositBtn = h('button.btn.primary', { text: 'Положить в цель', onclick: deposit });
      onEnter(amountInput, deposit);

      async function deposit() {
        if (depositBtn.disabled) return;
        const value = amountInput.value.trim();
        if (!value) {
          depositError.textContent = 'Сколько положить?';
          amountInput.focus();
          haptic('error');
          return;
        }
        depositBtn.disabled = true;
        depositError.textContent = '';
        try {
          const r = await api(`/api/goals/${g.id}/deposit`, { method: 'POST', json: { amount: value, src } });
          haptic('success');
          const done = r.completed && r.completed.length;
          toast(done ? `🏆 Цель собрана: ${r.completed.join(', ')}!` : `🎯 ${money(r.amount, '+')} в «${clip(g.title, 30)}»`, [r.quote], done ? 8000 : 5000);
          await loadState();
          openGoal(g.id);
          refresh({ quiet: true });
        } catch (e) {
          if (e.fatal) return;
          depositError.textContent = e.message;
          haptic('error');
          depositBtn.disabled = false;
        }
      }

      depositBlock = h(
        'div',
        null,
        h('div.card-title', { text: 'Пополнить цель' }),
        amountInput,
        srcChips,
        depositError,
        depositBtn,
      );
    }

    openSheet([
      photos,
      addPhoto,
      fileInput,
      photoError,
      h('h3.gap-top', null, g.title, g.reached && h('span.badge', { text: 'Собрано' })),
      progress(g.progress, g.reached),
      h('div.goal-meta', { text: `${money(g.saved)} из ${money(g.target)} · ${pct(g.progress)}` }),
      h('div.goal-meta', { text: etaText(g) }),
      h('div.gap-top', null, depositBlock),
    ]);
  }

  function openPhoto(g, pid) {
    haptic('tap');
    const img = h('img.photo-full', { alt: g.title });
    photoUrl(pid).then((url) => {
      if (url) img.src = url;
    });
    const error = h('p.error-text');
    const del = h('button.btn.danger', { text: '🗑 Удалить это фото', onclick: remove });
    let armed = false;
    async function remove() {
      if (!armed) {
        armed = true;
        haptic('warning');
        del.classList.add('confirm');
        del.textContent = 'Точно удалить? Нажми ещё раз';
        return;
      }
      del.disabled = true;
      try {
        await api('/api/photo/' + pid, { method: 'DELETE' });
        forgetPhoto(pid);
        haptic('success');
        await loadState();
        openGoal(g.id);
        refresh({ quiet: true });
      } catch (e) {
        error.textContent = e.message;
        del.disabled = false;
        haptic('error');
      }
    }
    openSheet([img, error, del, h('button.btn.ghost.gap-top', { text: '‹ Назад к цели', onclick: () => openGoal(g.id) })], () => openGoal(g.id));
  }

  function openNewGoal() {
    haptic('tap');
    const titleInput = h('input.input', { maxlength: 60, placeholder: 'Машина, квартира, подушка…', autocomplete: 'off', enterkeyhint: 'next' });
    const targetInput = h('input.input.num', { inputmode: 'decimal', placeholder: 'Сколько стоит', autocomplete: 'off', enterkeyhint: 'done', maxlength: 20 });
    const error = h('p.error-text');
    const save = h('button.btn.primary', { text: '🔥 Поставить цель', onclick: submit });
    onEnter(titleInput, () => targetInput.focus());
    onEnter(targetInput, submit);

    async function submit() {
      if (save.disabled) return;
      const title = titleInput.value.trim();
      const target = targetInput.value.trim();
      if (!title) {
        error.textContent = 'Назови цель.';
        titleInput.focus();
        haptic('error');
        return;
      }
      if (!target) {
        error.textContent = 'Сколько она стоит? Например: 150к';
        targetInput.focus();
        haptic('error');
        return;
      }
      save.disabled = true;
      error.textContent = '';
      try {
        const r = await api('/api/goals', { method: 'POST', json: { title, target } });
        haptic('success');
        toast('🎯 Цель поставлена', ['Теперь прикрепи фото — смотреть на мечту каждый день мощно мотивирует.']);
        await loadState();
        openGoal(r.goal.id);
        refresh({ quiet: true });
      } catch (e) {
        error.textContent = e.message;
        haptic('error');
        save.disabled = false;
      }
    }

    const pctText = S.state ? `С каждого дохода ${S.state.goal_pct}% автоматически уходит в цели.` : '';
    openSheet([
      h('h3', { text: 'Новая цель' }),
      field('Что хочешь', titleInput),
      field('Сколько стоит', targetInput),
      h('p.form-hint', { text: pctText }),
      error,
      save,
    ]);
    titleInput.focus({ preventScroll: true });
  }

  // ═══════════════════════ календарь ═══════════════════════

  function renderCalendar(d) {
    S.cal = { y: d.y, m: d.m };
    const nav = h(
      'div.month-nav',
      null,
      h('button', { text: '‹', 'aria-label': 'Предыдущий месяц', onclick: () => shiftMonth(d.prev) }),
      h('h2', { text: d.title }),
      h('button', { text: '›', 'aria-label': 'Следующий месяц', disabled: !d.next, onclick: () => d.next && shiftMonth(d.next) }),
    );
    let maxOut = 0;
    for (const [, out] of Object.values(d.days)) maxOut = Math.max(maxOut, out);
    const cells = [];
    for (let i = 0; i < d.first_weekday; i++) cells.push(h('div.cell.blank'));
    for (let day = 1; day <= d.days_in_month; day++) {
      const iso = `${d.y}-${String(d.m).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const [inc, out] = d.days[iso] || [0, 0];
      const future = iso > d.today;
      const cls = 'div.cell' + (iso === d.today ? '.today' : '') + (future ? '.future' : '');
      const heat = out > 0 && maxOut > 0 ? 0.1 + 0.45 * Math.sqrt(out / maxOut) : 0;
      const content = [
        h('span.d', { text: String(day) }),
        inc > 0 && h('span.in', { text: '+' + short(inc) }),
        out > 0 && h('span.out', { text: MINUS + short(out) }),
      ];
      cells.push(future ? h(cls, null, content) : clickable(cls, { css: { '--heat': heat.toFixed(3) }, onclick: () => openDay(iso) }, content));
    }
    const s = d.summary;
    const summary = h(
      'section.card',
      null,
      h('div.row3', null, tile('Заработал', money(s.income), 'pos'), tile('Потратил', money(s.expense), 'neg'), tile('Отложил', money(s.saved), 'gold')),
      d.no_spend && h('div.note', { text: `🧊 Дней без трат: ${d.no_spend.days} из ${d.no_spend.of}` }),
      s.impulse > 0 && h('div.note.warn', { text: `🍔 На хотелки: ${money(s.impulse)} (${pct(s.impulse / Math.max(1, s.expense))} трат)` }),
      s.goal_purchases > 0 && h('div.note', { text: `🏆 Из них покупки целей: ${money(s.goal_purchases)}` }),
    );
    const cats = d.categories.length
      ? h(
          'section.card',
          null,
          h('div.card-title', { text: 'Куда ушли деньги' }),
          d.categories.map((c) =>
            h(
              'div.cat-row' + (c.impulse ? '.impulse' : ''),
              null,
              h('div.cat-head', null, h('span', { text: `${c.emoji} ${c.name}` }), h('b.num', { text: `${money(c.total)} · ${pct(c.share)}` })),
              progress(c.share, false),
            ),
          ),
        )
      : null;
    return h(
      'div',
      null,
      nav,
      h('div.weekdays', null, WEEKDAYS.map((w) => h('div', { text: w }))),
      h('div.grid', null, cells),
      h('p.hint.foot', { text: 'Нажми на день — покажу, что там было.' }),
      summary,
      cats,
      h('section.card.quote', null, h('div.quote-text.verdict', { text: d.verdict })),
    );
  }

  function shiftMonth(target) {
    haptic('select');
    S.cal = { y: target[0], m: target[1] };
    refresh({ quiet: true });
  }

  function openDay(iso) {
    haptic('tap');
    const token = openSheet([h('h3', { text: dayTitle(iso) }), h('div.loading', { text: 'Загрузка…' })]);
    api('/api/day?d=' + iso)
      .then((d) => {
        if (token !== S.sheetSeq) return;
        let inc = 0;
        let out = 0;
        for (const t of d.txs) {
          if (t.kind === 'income') inc += t.amount;
          else if (t.kind === 'expense') out += t.amount;
        }
        const back = () => openDay(iso);
        const totals = [inc && `Заработал ${money(inc)}`, out && `потратил ${money(out)}`].filter(Boolean).join(' · ');
        openSheet([
          h('h3', { text: d.title }),
          totals && h('p.hint', { text: totals.charAt(0).toUpperCase() + totals.slice(1) }),
          d.txs.length
            ? h('div.list', null, d.txs.map((t) => txItem(t, () => openTx(t, back))))
            : h('div.empty', { text: d.future ? 'Этот день ещё не наступил.' : 'Ни одной записи. Либо ты не тратил, либо не записал. Второе — хуже.' }),
          !d.future && h('button.btn.ghost.gap-top', { text: '＋ Запись за этот день', onclick: () => openAdd({ day: iso, back }) }),
        ]);
      })
      .catch((e) => {
        if (token === S.sheetSeq && !e.fatal) openSheet([h('h3', { text: dayTitle(iso) }), h('p.error-text', { text: e.message })]);
      });
  }

  // ═══════════════════════ история ═══════════════════════

  function txSign(t) {
    return t.kind === 'income' ? '+' : t.kind === 'expense' ? '-' : '';
  }

  function txItem(t, onclick) {
    const sub = [t.note ? t.label : null, t.time, t.kind === 'income' && t.saved ? `в накопления ${money(t.saved)}` : null].filter(Boolean);
    const amountCls = t.kind === 'income' ? '.pos' : t.kind === 'expense' ? (t.impulse ? '.neg' : '') : '.gold';
    return clickable(
      'div.item',
      { onclick },
      h('div.emoji', { text: t.emoji }),
      h('div.main', null, h('div.title', { text: t.note || t.label }), sub.length && h('div.sub', { text: sub.join(' · ') })),
      h('div.amount.num' + amountCls, { text: money(t.amount, txSign(t)) }),
    );
  }

  function renderHistory(data) {
    const txs = data.txs;
    if (!txs.length) {
      return h(
        'div.empty',
        null,
        h('p', { text: 'Записей пока нет. Жми «+» и запиши первую трату или доход.' }),
      );
    }
    const groups = new Map();
    for (const t of txs) {
      if (!groups.has(t.day)) groups.set(t.day, []);
      groups.get(t.day).push(t);
    }
    const days = Array.from(groups.keys()).sort().reverse();
    return h(
      'div',
      null,
      days.map((day) => [
        h('div.day-head', { text: dayTitle(day) }),
        h('div.list', null, groups.get(day).map((t) => txItem(t, () => openTx(t)))),
      ]),
      txs.length >= S.histLimit &&
        S.histLimit < 200 &&
        h('button.btn.ghost.gap-top', {
          text: 'Показать ещё',
          onclick: () => {
            S.histLimit = Math.min(200, S.histLimit + 60);
            refresh({ quiet: true });
          },
        }),
    );
  }

  function openTx(tx, back) {
    haptic('tap');
    let t = tx;
    const regular = t.kind === 'income' || t.kind === 'expense';
    const title = h('h3');
    const details = h('div');
    const chips = h('div.chips');
    const error = h('p.error-text');
    const del = h('button.btn.danger', { text: '🗑 Удалить запись', onclick: remove });
    let armTimer = null;

    function renderHead() {
      title.textContent = `${t.emoji} ${money(t.amount, txSign(t))}`;
      const lines = [t.note && h('div.goal-title', { text: t.note }), h('div.goal-meta', { text: [t.label, dayTitle(t.day), t.time].filter(Boolean).join(' · ') })];
      if (t.kind === 'income') lines.push(h('div.goal-meta', { text: `🎯 В накопления: ${money(t.saved)} · 💸 На расходы: ${money(t.wallet)}` }));
      details.replaceChildren(...lines.filter(Boolean));
    }

    function renderChips() {
      if (!regular || !S.cats) return;
      chips.replaceChildren(
        ...S.cats[t.kind].map((c) =>
          h('button.chip' + (c.key === t.category ? '.on' : ''), { text: `${c.emoji} ${c.name}`, onclick: () => setCategory(c) }),
        ),
      );
    }

    async function setCategory(c) {
      if (c.key === t.category) return;
      haptic('select');
      error.textContent = '';
      try {
        t = (await api(`/api/tx/${t.id}`, { method: 'PATCH', json: { category: c.key } })).tx;
        renderHead();
        renderChips();
        toast('✅ Категория изменена', [t.note ? `Запомнил: «${clip(t.note, 40)}» → ${c.emoji} ${c.name}` : null]);
        refresh({ quiet: true });
      } catch (e) {
        error.textContent = e.message;
        haptic('error');
      }
    }

    async function remove() {
      if (!del.classList.contains('confirm')) {
        haptic('warning');
        del.classList.add('confirm');
        del.textContent = 'Точно удалить? Нажми ещё раз';
        armTimer = setTimeout(() => {
          del.classList.remove('confirm');
          del.textContent = '🗑 Удалить запись';
        }, 4000);
        return;
      }
      clearTimeout(armTimer);
      del.disabled = true;
      error.textContent = '';
      try {
        await api(`/api/tx/${t.id}`, { method: 'DELETE' });
        haptic('success');
        toast('🗑 Запись удалена', [`${money(t.amount, txSign(t))} · ${t.note || t.label}`]);
        if (back) back();
        else closeSheet();
        refresh({ quiet: true });
      } catch (e) {
        error.textContent = e.message;
        haptic('error');
        del.disabled = false;
        del.classList.remove('confirm');
        del.textContent = '🗑 Удалить запись';
      }
    }

    renderHead();
    renderChips();
    openSheet(
      [
        back && h('button.link', { text: '‹ Назад', onclick: back }),
        title,
        details,
        regular && h('div.card-title.gap-top', { text: 'Категория' }),
        regular && chips,
        regular && t.note && h('p.form-hint', { text: 'Поменяешь — запомню, и дальше такие записи сразу попадут куда надо.' }),
        error,
        del,
      ],
      back,
    );
    if (regular && !S.cats) loadCategories().then(renderChips, () => {});
  }

  // ═══════════════════════ новая запись ═══════════════════════

  function openAdd(opts) {
    if (!S.cats) {
      loadCategories().then(
        () => openAdd(opts),
        (e) => toast('Не получилось', [e.message]),
      );
      return;
    }
    haptic('tap');
    const f = {
      kind: opts.kind || 'expense',
      kindChosen: Boolean(opts.kind),
      category: null,
      suggested: null,
      day: opts.day || today(),
      classifySeq: 0,
    };
    let classifyTimer = null;

    const segExpense = h('button', { text: 'Расход', onclick: () => setKind('expense', true) });
    const segIncome = h('button', { text: 'Доход', onclick: () => setKind('income', true) });
    const amountInput = h('input.input.amount.num', {
      inputmode: 'decimal',
      placeholder: '0',
      autocomplete: 'off',
      enterkeyhint: 'next',
      maxlength: 20,
      'aria-label': 'Сумма',
    });
    const preview = h('div.form-hint');
    const noteInput = h('input.input', { maxlength: 100, autocomplete: 'off', enterkeyhint: 'done', 'aria-label': 'Комментарий' });
    const hint = h('p.form-hint');
    const chips = h('div.chips.cats');
    const dayChips = h('div.chips.days');
    const error = h('p.error-text');
    const save = h('button.btn', { onclick: submit });

    function setKind(kind, chosen) {
      if (chosen) {
        f.kindChosen = true;
        haptic('select');
      }
      if (kind === f.kind) return;
      f.kind = kind;
      f.category = null;
      f.suggested = null;
      renderKind();
      if (chosen) scheduleClassify(0);
    }

    function renderKind() {
      segExpense.className = f.kind === 'expense' ? 'on expense' : '';
      segIncome.className = f.kind === 'income' ? 'on income' : '';
      noteInput.placeholder = f.kind === 'income' ? 'Откуда: зарплата, фриланс, подарок…' : 'На что: шаурма, такси, пятёрочка…';
      save.className = 'btn ' + f.kind;
      save.textContent = f.kind === 'income' ? 'Записать доход' : 'Записать расход';
      renderChips();
      renderHint();
      renderPreview();
    }

    function renderChips() {
      const active = f.category || f.suggested;
      chips.replaceChildren(
        ...S.cats[f.kind].map((c) =>
          h('button.chip' + (c.key === active ? '.on' : ''), {
            text: `${c.emoji} ${c.name}`,
            onclick: () => {
              haptic('select');
              f.category = f.category === c.key ? null : c.key;
              renderChips();
              renderHint();
            },
          }),
        ),
      );
    }

    function renderHint() {
      const note = noteInput.value.trim();
      const key = f.category || f.suggested;
      const c = key && S.cats[f.kind].find((x) => x.key === key);
      let text;
      if (f.category && c) {
        text = note ? `Запомню: «${clip(note, 28)}» → ${c.emoji} ${c.name}` : `Категория: ${c.emoji} ${c.name}`;
      } else if (c) {
        text = `Угадал: ${c.emoji} ${c.name}. Не так — выбери другую, я запомню.`;
      } else if (note) {
        text = S.state && S.state.ai ? '🤖 Незнакомое слово — категорию подберёт нейросеть.' : 'Не узнал слово — выбери категорию, я запомню.';
      } else {
        text = f.kind === 'income' ? 'Напиши, откуда деньги, — угадаю категорию сам.' : 'Напиши, на что потратил, — угадаю категорию сам.';
      }
      hint.textContent = text;
    }

    function renderPreview() {
      const value = looseAmount(amountInput.value);
      const st = S.state;
      preview.classList.remove('neg');
      if (!value || !st) {
        preview.textContent = '';
        return;
      }
      if (f.kind === 'income') {
        const saved = Math.round((value * st.goal_pct) / 100);
        preview.textContent = `→ ${money(saved)} в накопления (${st.goal_pct}%) · ${money(value - saved)} на расходы`;
      } else {
        const left = st.balances.wallet - value;
        if (left >= 0) {
          preview.textContent = `Останется на расходы: ${money(left)}`;
        } else {
          preview.textContent = `🚨 Уйдёшь в минус на ${money(-left)} — это деньги твоих целей.`;
          preview.classList.add('neg');
        }
      }
    }

    function renderDays() {
      const t = today();
      const options = [
        [t, 'Сегодня'],
        [addDays(t, -1), 'Вчера'],
        [addDays(t, -2), 'Позавчера'],
      ];
      if (!options.some(([d]) => d === f.day)) options.push([f.day, shortDate(f.day)]);
      dayChips.replaceChildren(
        ...options.map(([d, label]) =>
          h('button.chip' + (d === f.day ? '.on' : ''), {
            text: label,
            onclick: () => {
              haptic('select');
              f.day = d;
              renderDays();
            },
          }),
        ),
      );
    }

    function scheduleClassify(delay) {
      clearTimeout(classifyTimer);
      const seq = ++f.classifySeq;
      const note = noteInput.value.trim();
      if (!note) {
        f.suggested = null;
        renderChips();
        renderHint();
        return;
      }
      classifyTimer = setTimeout(() => classify(note, seq), delay);
    }

    async function classify(note, seq) {
      const ask = () => api(`/api/classify?note=${encodeURIComponent(note)}&kind=${f.kind}`);
      try {
        let r = await ask();
        if (seq !== f.classifySeq) return;
        if (!f.kindChosen && r.kind !== f.kind) {
          setKind(r.kind, false); // «зарплата» — это доход, даже если открыл «+»
          r = await ask();
          if (seq !== f.classifySeq) return;
        }
        f.suggested = r.category;
        renderChips();
        renderHint();
      } catch (e) {
        /* подсказка не критична */
      }
    }

    async function submit() {
      if (save.disabled) return;
      const value = amountInput.value.trim();
      if (!value) {
        error.textContent = 'Введи сумму.';
        amountInput.focus();
        haptic('error');
        return;
      }
      save.disabled = true;
      error.textContent = '';
      const body = { kind: f.kind, amount: value, note: noteInput.value.trim(), day: f.day };
      if (f.category) body.category = f.category;
      try {
        const r = await api('/api/tx', { method: 'POST', json: body });
        const impulse = r.tx.impulse || r.wallet < 0;
        haptic(impulse ? 'warning' : 'success');
        announce(r);
        if (opts.back) opts.back();
        else closeSheet();
        refresh({ quiet: true });
        if (r.ai_pending) setTimeout(() => $sheet.hidden && refresh({ quiet: true }), 5000);
      } catch (e) {
        error.textContent = e.message;
        haptic('error');
        save.disabled = false;
      }
    }

    amountInput.addEventListener('input', () => {
      error.textContent = '';
      renderPreview();
    });
    noteInput.addEventListener('input', () => {
      renderHint();
      scheduleClassify(250);
    });
    onEnter(amountInput, () => noteInput.focus());
    onEnter(noteInput, submit);

    renderKind();
    renderDays();
    openSheet(
      [
        h('h3', { text: 'Новая запись' }),
        h('div.segment', null, segExpense, segIncome),
        field('Сумма', amountInput),
        preview,
        field('Комментарий', noteInput),
        hint,
        chips,
        h('div.field', null, h('span', { text: 'Когда' }), dayChips),
        error,
        save,
      ],
      opts.back,
    );
    amountInput.focus({ preventScroll: true });
  }

  function announce(r) {
    const t = r.tx;
    const lines = [];
    let title = `${money(t.amount, txSign(t))} · ${t.emoji} ${t.label}`;
    if (t.kind === 'income' && t.saved) lines.push(`🎯 ${money(t.saved)} в накопления · 💸 ${money(t.wallet)} на расходы`);
    if (t.kind === 'expense' && r.wallet < 0) lines.push(`🚨 На расходы: ${money(r.wallet)}. Ты в минусе.`);
    if (r.ai_pending) lines.push('🤖 Категорию уточнит нейросеть.');
    lines.push(r.quote);
    const done = r.completed && r.completed.length;
    if (done) {
      title = `🏆 Цель собрана: ${r.completed.join(', ')}!`;
      lines.unshift('Ты сделал это. Праздник — в чате с ботом.');
    }
    toast(title, lines, done ? 8000 : 5500);
  }

  // ═══════════════════════ старт ═══════════════════════

  async function start() {
    post('web_app_ready');
    post('web_app_expand');
    post('web_app_setup_swipe_behavior', { allow_vertical_swipe: false });
    post('web_app_request_theme');
    if (!initData) {
      fatal('🔒', 'Открой из Telegram', 'Это личное приложение бота. Открой его кнопкой «Приложение» в чате с ботом.');
      return;
    }
    $view.replaceChildren(h('div.loading', { text: 'Загрузка…' }));
    try {
      await Promise.all([loadState(), loadCategories()]);
    } catch (e) {
      if (!e.fatal) fatal('📡', 'Нет связи с ботом', e.message, start);
      return;
    }
    S.started = true;
    $tabs.hidden = false;
    $fab.hidden = false;
    $view.replaceChildren(renderHome(S.state));
    updateBackButton();
  }

  // Вернулся в приложение (например, после записи в чате) — показываем свежие цифры.
  function wake() {
    if (S.started && $sheet.hidden) refresh({ quiet: true });
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) wake();
  });
  onEvent('visibility_changed', (data) => {
    if (data && data.is_visible) wake();
  });

  start();
})();
