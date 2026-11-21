/* Квордли: общее поле ввода, четыре независимых набора подсказок. */
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const sectionNames = ['game', 'stats', 'settings', 'rules', 'formula', 'leaderboard', 'summary', 'closed', 'exhausted'];
  const names = ['верхнее левое', 'верхнее правое', 'нижнее левое', 'нижнее правое'];
  const colors = {correct: 'на месте', present: 'есть', absent: 'нет', '': 'неизвестно'};
  const priority = {'': 0, absent: 1, present: 2, correct: 3};
  const keyElements = new Map();
  // Те же линейные пиктограммы, что на клавиатуре «5БУКВ».
  const keyIcons = {
    '↵': '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4.5 4.5L19 7"/></svg>',
    '⌫': '<svg viewBox="0 0 28 24" aria-hidden="true"><path d="M10 4h14v16H10L2 12Z"/><path d="m13 9 6 6m0-6-6 6"/></svg>',
  };
  let tg = window.Telegram?.WebApp;
  const launchParams = new URLSearchParams(location.hash.slice(1));
  const signedHash = launchParams.get('tgWebAppData') || '';
  const noMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const hoverCapable = window.matchMedia('(hover: hover) and (pointer: fine)');
  let state = null, screen = 'game', cursor = 0, cells = Array(5).fill(' ');
  let queue = Promise.resolve(), busy = false, draftTimer, toastTimer, ratingGeneration = 0;
  let invalid = false, definitionToken = 0, anchor = null, hoverTimer, midnightRequest = null;
  const api = async (route, body = {}) => {
    const headers = {'Content-Type': 'application/json'};
    const launchData = tg?.initData || signedHash;
    if (launchData) headers['X-Telegram-Init-Data'] = launchData;
    const response = await fetch('api/' + route, {
      method: 'POST', headers, body: JSON.stringify(body), credentials: 'same-origin',
      cache: 'no-store', referrerPolicy: 'no-referrer', mode: 'same-origin', redirect: 'error',
      keepalive: true, signal: AbortSignal.timeout?.(15000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = data.message || (response.status === 401 || response.status === 403
        ? 'Откройте «КВОРДЛИ» кнопкой в Telegram-боте.' : 'Не удалось связаться с игрой.');
      throw Object.assign(new Error(message), {code: data.error, status: response.status, state: data.state});
    }
    return data;
  };
  function serialize(task) {
    const call = queue.then(task);
    queue = call.catch(() => {});
    return call;
  }
  function notify(message, wordError = false) {
    clearTimeout(toastTimer);
    const box = $('toast');
    box.textContent = message;
    box.className = wordError ? 'toast toast--word-error is-visible' : 'toast is-visible';
    if (!wordError) toastTimer = setTimeout(hideToast, 3200);
  }
  function hideToast() { $('toast').className = 'toast'; }
  function active() { return state?.game?.status === 'in_progress'; }
  function finished() { return ['won', 'lost', 'expired'].includes(state?.game?.status); }
  function apply(next, preserve = false) {
    if (!next?.game || !Number.isInteger(next.revision)) throw new Error('Не удалось прочитать состояние игры.');
    state = next;
    if (!preserve) {
      cells = String(state.game.draft || '').padEnd(5, ' ').slice(0, 5).split('');
      const firstEmpty = cells.indexOf(' ');
      cursor = firstEmpty < 0 ? 4 : firstEmpty;
    }
    render();
  }
  function setScreen(name) {
    closeDefinition();
    hideToast();
    screen = name;
    document.body.dataset.screen = name;
    for (const section of sectionNames) $(section + '-screen').hidden = section !== name;
    document.querySelector('.app-shell').classList.toggle('app-shell--closed', name === 'closed');
    window.scrollTo(0, 0);
    if (name === 'stats') renderStats();
    if (name === 'settings') renderSettings();
    if (name === 'leaderboard') loadRating();
    if (name === 'summary') renderResult();
  }
  function home() {
    setScreen(finished() ? 'summary' : !state?.game || active() ? 'game' : 'exhausted');
  }
  function hints(board) {
    const result = Array(5).fill('');
    for (const row of board.rows)
      row.states.forEach((color, col) => { if (color === 'correct') result[col] = row.word[col]; });
    return result;
  }
  function drawBoards() {
    if (!state) return;
    const fragment = document.createDocumentFragment();
    state.game.boards.forEach((board, index) => {
      const grid = el('div', 'quad');
      grid.setAttribute('role', 'grid');
      grid.setAttribute('aria-label', 'Поле ' + (index + 1) + ': ' + names[index] + (board.solved ? ', угадано' : ''));
      const known = hints(board);
      for (let row = 0; row < 9; row++) {
        const entry = board.rows[row];
        const current = active() && !board.solved && row === state.game.attempts;
        const line = el('div', 'quad-row' + (current ? ' current' : '') + (current && invalid ? ' invalid' : ''));
        line.setAttribute('role', 'row');
        for (let col = 0; col < 5; col++) {
          const letter = current ? cells[col].trim() : '';
          const ghost = current && !letter ? known[col] : '';
          const shown = entry?.word[col] || letter || ghost || '';
          const tile = el('span', 'quad-cell', shown);
          tile.setAttribute('role', 'gridcell');
          tile.dataset.col = col;
          if (entry) tile.dataset.color = entry.states[col];
          if (current) {
            if (cursor === col) tile.setAttribute('aria-current', 'true');
            if (ghost) tile.dataset.hint = 'true';
            if (letter && letter === known[col]) tile.dataset.exact = 'true';
          }
          tile.setAttribute('aria-label', (col + 1) + ': ' + (shown || 'пусто') + (entry ? ', ' + colors[entry.states[col]] : ghost ? ', подсказка' : ''));
          line.append(tile);
        }
        grid.append(line);
      }
      fragment.append(grid);
    });
    $('boards').replaceChildren(fragment);
  }
  function makeKeys() {
    for (const row of ['ЙЦУКЕНГШЩЗХЪ', 'ФЫВАПРОЛДЖЭ', '↵ЯЧСМИТЬБЮ⌫']) {
      const line = el('div', 'keyboard-row');
      for (const key of row) {
        const action = key === '↵' || key === '⌫';
        const btn = el('button', 'key' + (key === '↵' ? ' key--enter' : key === '⌫' ? ' key--delete' : ''));
        btn.type = 'button'; btn.dataset.key = key;
        btn.setAttribute('aria-label', key === '↵' ? 'Подтвердить слово' : key === '⌫' ? 'Стереть букву' : key);
        if (!action) {
          const sectors = el('span', 'key-sectors');
          sectors.setAttribute('aria-hidden', 'true');
          sectors.append(...Array.from({length: 4}, () => el('i')));
          btn.append(sectors, el('span', 'key-letter', key)); keyElements.set(key, btn);
        } else btn.innerHTML = keyIcons[key];
        line.append(btn);
      }
      $('keyboard').append(line);
    }
  }
  function drawKeys(boards) {
    for (const [letter, btn] of keyElements) {
      // После закрытия поля сервер сохраняет подсказки последнего принятого им хода.
      const parts = boards.map(b => b.keyboard?.[letter] || '');
      const lightParts = parts.filter(p => p === 'present' || p === 'correct').length;
      btn.dataset.ink = lightParts === 4 ? 'dark' : lightParts ? 'mixed' : 'light';
      Array.from(btn.firstElementChild.children).forEach((part, i) => { part.dataset.color = parts[i]; });
      btn.setAttribute('aria-label', letter + ': ' + parts.map((p, i) => names[i] + ' — ' + colors[p]).join('; '));
    }
  }
  function render() {
    if (!state) return;
    drawBoards();
    drawKeys(state.game.boards);
    if (screen === 'stats') renderStats();
    if (screen === 'settings') renderSettings();
  }
  async function refresh() {
    apply(await api('state'));
    invalid = false; $('retry').hidden = true; $('loading-status').hidden = true;
    if (['game', 'summary', 'exhausted'].includes(screen)) home();
  }
  async function reload() {
    try { await serialize(refresh); }
    catch (err) {
      $('loading-status').textContent = err.message; $('loading-status').hidden = false; $('retry').hidden = false;
    }
  }
  async function recover(err) {
    if (err.status !== 409) return false;
    if (err.state?.game) apply(err.state);
    else await refresh();
    home(); notify(err.message || 'Партия обновлена.'); return true;
  }
  async function saveDraft() {
    if (!active()) return;
    const draft = cells.join('').trimEnd();
    if (draft === (state.game.draft || '')) return;
    try { apply(await api('draft', {revision: state.revision, draft}), true); }
    catch (err) {
      if (!await recover(err)) throw err;
    }
  }
  function scheduleDraft() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => serialize(saveDraft).catch(err => notify(err.message)), 230);
  }
  function clearInvalid() { invalid = false; hideToast(); }
  function input(key) {
    if (!active() || busy || screen !== 'game') return;
    if (key === '↵') { submit(); return; }
    clearInvalid();
    if (key === '⌫') {
      if (cells[cursor] === ' ' && cursor > 0) cursor--;
      cells[cursor] = ' ';
    } else {
      if (!/^[А-ЯЁ]$/u.test(key)) return;
      cells[cursor] = key.replace('Ё', 'Е'); cursor = Math.min(cursor + 1, 4);
    }
    drawBoards(); scheduleDraft();
  }
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function reveal(before, after) {
    const views = before.boards.map(b => ({solved: b.solved, keyboard: {...b.keyboard}}));
    const rows = $('boards').querySelectorAll('.quad');
    for (let col = 0; col < 5; col++) {
      after.boards.forEach((board, i) => {
        const move = board.rows[before.attempts];
        if (!move || before.boards[i].solved) return;
        const tile = rows[i]?.children[before.attempts]?.children[col];
        if (tile) {
          tile.textContent = move.word[col]; tile.dataset.color = move.states[col];
          delete tile.dataset.hint; delete tile.dataset.exact;
          if (!noMotion.matches) tile.animate?.([{transform:'scaleY(.3)'},{transform:'scaleY(1)'}], 160);
        }
        const letter = move.word[col], color = move.states[col], old = views[i].keyboard[letter] || '';
        if (priority[color] > priority[old]) views[i].keyboard[letter] = color;
      });
      drawKeys(views);
      if (!noMotion.matches) await sleep(95);
    }
  }
  function celebrate() {
    if (noMotion.matches) return;
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < 36; i++) {
      const piece = el('i', 'confetti-piece');
      piece.style.setProperty('--confetti-x', Math.random() * 100 + '%');
      piece.style.setProperty('--confetti-delay', Math.random() * 450 + 'ms');
      piece.style.setProperty('--confetti-color', ['#fedd2c','#fff','#e4bf19'][i % 3]);
      fragment.append(piece);
    }
    $('confetti').replaceChildren(fragment);
    setTimeout(() => $('confetti').replaceChildren(), 3200);
  }
  async function submit() {
    if (busy || !active()) return;
    const word = cells.join('');
    if (!/^[А-Я]{5}$/u.test(word)) {
      invalid = true; drawBoards(); notify('Введите пять букв.', true); return;
    }
    busy = true; clearTimeout(draftTimer); clearInvalid();
    const round = state.game.id;
    await serialize(async () => {
      if (!active() || round !== state.game.id || cells.join('') !== word) return;
      const before = state.game;
      const next = await api('guess', {word, revision: state.revision, request_id: crypto.randomUUID()});
      await reveal(before, next.game);
      apply(next);
      if (finished()) {
        if (next.game.status === 'won') celebrate();
        if (!noMotion.matches) await sleep(1000);
        if (screen === 'game') home();
      }
    }).catch(async err => {
      if (await recover(err)) return;
      invalid = err.status === 400 || err.status === 422;
      drawBoards(); notify(err.message, invalid);
      if (invalid) scheduleDraft();
    }).catch(err => notify(err.message)).finally(() => { busy = false; });
  }
  async function nextGame() {
    if (!finished() || busy) return;
    busy = true;
    try {
      await serialize(async () => { apply(await api('next', {revision: state.revision, request_id: crypto.randomUUID()})); home(); });
    } catch (err) { if (!await recover(err)) notify(err.message); }
    finally { busy = false; }
  }
  function renderStats() {
    if (!state) return;
    const stats = state.stats;
    const data = {
      played: stats.played, 'win-rate': Math.round(stats.win_rate) + '%',
      average: stats.average == null ? '—' : stats.average.toLocaleString('ru', {maximumFractionDigits: 2}),
      'win-streak': stats.win_streak, 'max-streak': stats.max_streak, score: stats.score ?? '—',
    };
    for (const node of document.querySelectorAll('[data-stat]')) node.textContent = data[node.dataset.stat];
    const delta = document.querySelector('[data-score-delta]');
    delta.hidden = !stats.score_delta;
    delta.textContent = (stats.score_delta > 0 ? '+' : '') + (stats.score_delta ?? '');
    delta.parentElement.classList.toggle('stat-score-number--compact',
      !delta.hidden && String(data.score).length >= 4 && delta.textContent.length >= 4);
    const max = Math.max(1, ...stats.distribution);
    const rows = stats.distribution.map((n, i) => {
      const row = el('div', 'distribution-row');
      row.setAttribute('role', 'listitem');
      const track = el('div', 'distribution-track');
      const bar = el('div', 'distribution-bar' + (n ? '' : ' distribution-bar--empty'));
      bar.style.width = Math.max(5, Math.round(n / max * 100)) + '%';
      bar.append(el('span', 'distribution-value', n)); track.append(bar);
      row.append(el('span', 'distribution-label', i + 4), track); return row;
    });
    $('distribution').replaceChildren(...rows);
  }
  function updateClock() {
    if (!state || !state.expires_at) return;
    const until = Date.parse(state.expires_at) - Date.now();
    const sec = Math.max(0, Math.ceil(until / 1000));
    $('next-word-countdown').textContent = [Math.floor(sec / 3600), Math.floor(sec / 60) % 60, sec % 60]
      .map(n => String(n).padStart(2, '0')).join(':');
    if (until <= 0 && state.game.status !== 'expired' && !document.hidden && !busy && midnightRequest !== state.day) {
      midnightRequest = state.day;
      serialize(refresh).catch(err => {
        notify(err.message);
        setTimeout(() => { midnightRequest = null; }, 15000);
      });
    }
  }
  function renderSettings() {
    $('reminder-toggle').disabled = !state?.settings;
    $('reminder-toggle').setAttribute('aria-checked', String(state?.settings?.enabled === true));
    updateClock();
  }
  async function toggleReminder() {
    if (!state?.settings || $('reminder-toggle').disabled) return;
    $('reminder-toggle').disabled = true;
    try {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      const result = await api('settings', {enabled: !state.settings.enabled, timezone});
      state.settings = result.settings;
    } catch (err) { notify(err.message); }
    finally { renderSettings(); }
  }
  async function loadRating() {
    const generation = ++ratingGeneration, table = $('leaderboard-table'), status = $('leaderboard-status');
    table.hidden = true; status.textContent = 'Загрузка…';
    try {
      const {rating} = await api('rating');
      if (generation !== ratingGeneration || screen !== 'leaderboard') return;
      const list = Array.isArray(rating) ? rating : [];
      const rows = list.map(item => {
        const row = el('tr', item.is_self ? 'leaderboard-self' : '');
        if (item.is_self) row.setAttribute('aria-current', 'true');
        const name = el('td'), wrap = el('div', 'leaderboard-name'), label = el('span', 'leaderboard-label');
        const full = String(item.name || 'Игрок');
        label.title = full;
        label.textContent = [...full].length > 14 ? [...full].slice(0, 14).join('') + '..' : full;
        wrap.append(el('span', 'leaderboard-rank', item.rank), label);
        name.append(wrap); row.append(name, el('td', '', item.played), el('td', '', item.score));
        return row;
      });
      $('leaderboard-rows').replaceChildren(...rows);
      table.hidden = !rows.length;
      status.textContent = !rows.length ? 'Пока нет участников с пятью завершёнными партиями.'
        : state.stats.played < 5 ? 'Для участия завершите 5 партий.' : '';
    } catch (err) { if (generation === ratingGeneration) status.textContent = err.message; }
  }
  function renderResult() {
    if (!finished()) return;
    const game = state.game;
    const expired = game.status === 'expired';
    $('result-title').textContent = expired ? 'Игровой день завершён'
      : game.status === 'won' ? 'Все слова отгаданы!' : 'Попытки закончились';
    $('result-text').textContent = 'Отгадано ' + game.solved + ' из 4 слов';
    $('result-attempt-count').textContent = game.attempts;
    const cards = game.boards.map((b, i) => {
      const node = el(b.answer ? 'button' : 'div', b.solved ? 'result-card result-card--solved' : 'result-card');
      node.setAttribute('aria-label', names[i] + ': ' + (b.answer || 'ответ скрыт')
        + (b.solved ? ', угадано' : ', не отгадано') + (b.answer ? ', показать определение' : ''));
      if (b.answer) {
        node.type = 'button'; node.dataset.word = b.answer; node.setAttribute('aria-expanded', 'false');
        node.setAttribute('aria-controls', 'definition-popover');
      }
      if (b.solved) {
        const check = el('span', 'result-check');
        check.innerHTML = keyIcons['↵']; node.append(check);
      }
      node.append(el('span', 'result-word', b.answer || '—'));
      return node;
    });
    $('result-cards').replaceChildren(...cards);
  }
  function closeDefinition() {
    definitionToken++; clearTimeout(hoverTimer);
    if (anchor) anchor.setAttribute('aria-expanded', 'false');
    anchor = null; $('definition-popover').hidden = true;
  }
  function placeDefinition() {
    if (!anchor) return;
    const pop = $('definition-popover');
    const target = anchor.getBoundingClientRect();
    const W = document.documentElement.clientWidth, H = window.innerHeight;
    pop.style.width = Math.min(344, W - 20) + 'px';
    pop.style.maxHeight = Math.max(100, H - 20) + 'px';
    const {width, height} = pop.getBoundingClientRect();
    const left = Math.min(W - width - 10, Math.max(10, target.left + (target.width - width) / 2));
    const top = target.bottom + height + 10 <= H ? target.bottom + 5 : Math.max(10, target.top - height - 5);
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
  }
  async function openDefinition(btn) {
    clearTimeout(hoverTimer);
    if (anchor === btn && !$('definition-popover').hidden) return;
    closeDefinition();
    anchor = btn; btn.setAttribute('aria-expanded', 'true');
    const generation = definitionToken, word = btn.dataset.word;
    $('definition-title').textContent = word;
    $('definition-status').textContent = 'Загрузка…';
    const source = $('definition-source'); source.hidden = true; source.removeAttribute('href');
    $('definition-meanings').hidden = true;
    $('definition-popover').hidden = false; placeDefinition();
    try {
      const info = await api('definition', {word});
      if (generation !== definitionToken) return;
      if (info.word !== word || !info.definition) throw new Error('Определение недоступно.');
      $('definition-title').textContent = info.headword || word;
      $('definition-status').textContent = '';
      $('definition-meanings').replaceChildren(el('li', '', info.definition));
      $('definition-meanings').hidden = false;
      if (info.source_url) {
        const link = new URL(info.source_url);
        if (link.origin === 'https://ru.wiktionary.org' && link.pathname.startsWith('/wiki/') && !link.username && !link.password) {
          source.href = link.href; source.hidden = false;
        }
      }
    } catch (err) { if (generation === definitionToken) $('definition-status').textContent = err.message; }
    if (generation === definitionToken) placeDefinition();
  }
  function closeApp() {
    if (tg?.close) tg.close();
    else if (window.TelegramWebviewProxy?.postEvent) window.TelegramWebviewProxy.postEvent('web_app_close', '{}');
    else if (window.parent !== window && document.referrer.startsWith('https://web.telegram.org/'))
      window.parent.postMessage(JSON.stringify({eventType: 'web_app_close'}), 'https://web.telegram.org');
    setScreen('closed');
  }
  document.addEventListener('click', event => {
    const target = event.target.closest?.('button, .quad-cell, #definition-popover');
    if (!target?.closest('#definition-popover, .result-card[data-word]')) closeDefinition();
    if (!target) return;
    if (target.dataset.word) { openDefinition(target); return; }
    if (target.dataset.key) { input(target.dataset.key); return; }
    if (target.closest('.quad-row.current') && !busy && active()) {
      clearInvalid(); cursor = Number(target.dataset.col); drawBoards(); return;
    }
    const action = target.dataset.action;
    if (action?.startsWith('open-')) setScreen(action.slice(5));
    else if (action === 'back-settings') setScreen('settings');
    else if (action === 'back-game' || action === 'reopen-app') home();
    else if (action === 'next-game') nextGame().catch(err => notify(err.message));
    else if (action === 'toggle-reminder') toggleReminder();
    else if (action === 'close-app') closeApp();
    else if (action === 'reload') reload();
  });
  $('keyboard').addEventListener('mousedown', event => event.preventDefault());
  document.addEventListener('pointerover', event => {
    if (!hoverCapable.matches || event.pointerType === 'touch') return;
    const btn = event.target.closest?.('[data-word]');
    if (btn) { clearTimeout(hoverTimer); hoverTimer = setTimeout(() => openDefinition(btn), 140); }
    if (event.target.closest?.('#definition-popover')) clearTimeout(hoverTimer);
  });
  document.addEventListener('pointerout', event => {
    if (!hoverCapable.matches || event.pointerType === 'touch') return;
    if (event.target.closest?.('[data-word],#definition-popover') && !event.relatedTarget?.closest?.('[data-word],#definition-popover')) {
      clearTimeout(hoverTimer); hoverTimer = setTimeout(closeDefinition, 260);
    }
  });
  document.addEventListener('keydown', event => {
    if (event.altKey || event.metaKey || event.ctrlKey || event.isComposing) return;
    if (event.key === 'Escape') {
      if (anchor) { const btn = anchor; closeDefinition(); btn.focus(); }
      else if (['rules','formula','leaderboard'].includes(screen)) setScreen('settings');
      else if (screen !== 'game') home();
      return;
    }
    if (screen !== 'game' || !active() || busy) return;
    if (event.target.closest?.('button, a') && (event.key === 'Enter' || event.key === ' ')) return;
    if (/^[А-ЯЁ]$/iu.test(event.key) || ['Enter','Backspace','Delete','ArrowLeft','ArrowRight'].includes(event.key)) {
      event.preventDefault();
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        cursor = Math.max(0, Math.min(4, cursor + (event.key === 'ArrowLeft' ? -1 : 1))); drawBoards();
      } else input(event.key === 'Enter' ? '↵' : ['Backspace', 'Delete'].includes(event.key) ? '⌫' : event.key.toUpperCase());
    }
  });
  const flush = () => { clearTimeout(draftTimer); if (!busy) serialize(saveDraft).catch(() => {}); };
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) flush();
    else if (!busy) serialize(async () => { await saveDraft(); await refresh(); }).catch(err => notify(err.message));
  });
  window.addEventListener('resize', placeDefinition);
  window.addEventListener('scroll', placeDefinition, {passive: true});
  makeKeys();
  setInterval(updateClock, 1000);
  function ready() {
    tg = window.Telegram?.WebApp;
    tg?.ready?.(); tg?.expand?.();
    reload();
  }
  // Обычный localhost-предпросмотр не запрашивает внешний SDK и не хранит учётных данных.
  let telegramLaunch = Boolean(signedHash || launchParams.get('tgWebAppVersion'));
  try {
    if (telegramLaunch) sessionStorage.setItem('quordle.telegramLaunch', '1');
    else telegramLaunch = sessionStorage.getItem('quordle.telegramLaunch') === '1';
  } catch (_) { /* При запрете sessionStorage исходный фрагмент остаётся доступен. */ }
  if (telegramLaunch && !tg) {
    const sdk = document.createElement('script');
    sdk.src = 'https://telegram.org/js/telegram-web-app.js?64';
    sdk.referrerPolicy = 'no-referrer';
    let started = false;
    const begin = () => {
      if (started) { tg = window.Telegram?.WebApp; tg?.ready?.(); tg?.expand?.(); return; }
      started = true; ready();
    };
    const fallback = setTimeout(begin, 2500);
    sdk.onload = sdk.onerror = () => { clearTimeout(fallback); begin(); };
    document.head.append(sdk);
  } else ready();
})();
