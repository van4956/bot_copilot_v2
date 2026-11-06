/* Локальная игра: Python проверяет слова, браузер хранит только свою историю. */
(function () {
    'use strict';

    const STORAGE_KEY = 'wordli.local.v01';
    const WORD_LENGTH = 5;
    const ROW_COUNT = 6;
    const KEYBOARD_ROWS = ['ЙЦУКЕНГШЩЗХЪ', 'ФЫВАПРОЛДЖЭ', 'ЯЧСМИТЬБЮ'];
    const STATES = new Set(['correct', 'present', 'absent']);
    const STATE_LABELS = {
        correct: 'на своём месте', present: 'есть в другом месте',
        absent: 'нет в слове', filled: 'ввод', empty: 'пусто',
    };
    const ENTER_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4.5 4.5L19 7"/></svg>';
    const DELETE_ICON = '<svg viewBox="0 0 28 24" aria-hidden="true"><path d="M10 4h14v16H10L2 12Z"/><path d="m13 9 6 6m0-6-6 6"/></svg>';
    const STORAGE_WARNING = 'Локальное сохранение недоступно. Прогресс останется только до закрытия страницы.';

    class LocalApiError extends Error {
        constructor(code, message, status = 0, state = null) {
            super(message);
            this.name = 'LocalApiError';
            this.code = code;
            this.status = status;
            this.state = state;
        }
    }

    /** Разрешает адресом выбирать только экран, но не исход или дату игры. */
    function initialScreen(search = '') {
        const scene = new URLSearchParams(search).get('scene');
        return ['stats', 'settings'].includes(scene) ? scene : 'game';
    }

    /** Даёт прочитать предупреждение о несохранении, даже если игрок продолжает набор. */
    function preserveStorageWarning(message, now, protectedUntil) {
        return message === STORAGE_WARNING && now < protectedUntil;
    }

    /** Нормализует набранные буквы, не подменяя латинские символы русскими. */
    function normalizeDraft(value) {
        if (typeof value !== 'string') return null;
        const normalized = value.normalize('NFC').trim().toLocaleUpperCase('ru-RU').replace(/Ё/g, 'Е');
        return /^[А-Я]{0,5}$/u.test(normalized) ? normalized : null;
    }

    function copyHistory(history) {
        return history.map(game => ({
            day: game.day, index: game.index ?? 0, guesses: [...game.guesses], draft: game.draft,
            ...(game.answer !== undefined ? {answer: game.answer, dictionary_id: game.dictionary_id} : {}),
        }));
    }

    /** Проверяет формат сохранения; сами правила игры проверит сервер. */
    function validateHistory(history) {
        if (!Array.isArray(history) || history.length > 5000) throw new Error('Invalid saved history');
        const identities = new Set();
        for (const game of history) {
            const index = game?.index === undefined ? 0 : game.index;
            if (!game || typeof game !== 'object' || !/^\d{4}-\d{2}-\d{2}$/.test(game.day)
                || !Number.isInteger(index) || index < 0 || index >= 1000
                || !Array.isArray(game.guesses) || game.guesses.length > ROW_COUNT
                || game.guesses.some(word => typeof word !== 'string' || !/^[А-Я]{5}$/u.test(word))
                || normalizeDraft(game.draft) !== game.draft
                || ((game.answer !== undefined || game.dictionary_id !== undefined)
                    && (typeof game.answer !== 'string' || !/^[А-Я]{5}$/u.test(game.answer)
                        || typeof game.dictionary_id !== 'string' || !game.dictionary_id.length
                        || game.dictionary_id.length > 128))) throw new Error('Invalid saved game');
            const identity = gameIdentity(game);
            if (identities.has(identity)) throw new Error('Duplicate saved game');
            identities.add(identity);
        }
        return history;
    }

    /** Читает только свою версию формата, не исправляя повреждённые данные молча. */
    function parseSavedHistory(raw) {
        if (raw === null) return [];
        const saved = JSON.parse(raw);
        if (!saved || ![1, 2, 3].includes(saved.schema_version)) throw new Error('Unknown saved version');
        return copyHistory(validateHistory(saved.history));
    }

    /** При отказе localStorage честно переключается на память текущей страницы. */
    function createHistoryStore(storage, onWarning = () => {}) {
        let memory = [];
        let persistent = Boolean(storage);
        let warned = false;
        function warn() {
            if (!warned) { warned = true; onWarning(STORAGE_WARNING); }
        }
        return {
            read() {
                if (persistent) {
                    try { memory = parseSavedHistory(storage.getItem(STORAGE_KEY)); }
                    catch (_) { persistent = false; warn(); }
                } else warn();
                return copyHistory(memory);
            },
            write(history) {
                memory = copyHistory(validateHistory(history));
                if (persistent) {
                    try { storage.setItem(STORAGE_KEY, JSON.stringify({schema_version: 3, history: memory})); }
                    catch (_) { persistent = false; warn(); }
                } else warn();
            },
            get persistent() { return persistent; },
        };
    }

    /** Проверяет необходимую для отрисовки часть ответа локального Python API. */
    function validateState(state) {
        const game = state?.game;
        const stats = state?.stats;
        const index = game?.index === undefined ? 0 : game.index;
        const invalid = () => { throw new LocalApiError('invalid_response', 'Не удалось получить состояние игры'); };
        if (!state || !/^\d{4}-\d{2}-\d{2}$/.test(state.day)
            || !Number.isFinite(Date.parse(state.expires_at)) || !game || game.day !== state.day
            || !Number.isInteger(index) || index < 0 || index >= 1000
            || !['in_progress', 'won', 'lost', 'exhausted'].includes(game.status)
            || ![null, 'win', 'loss'].includes(game.outcome)
            || normalizeDraft(game.draft) !== game.draft || !Array.isArray(game.rows)
            || game.rows.length > ROW_COUNT || !Array.isArray(game.guesses)
            || game.guesses.length !== game.rows.length || !game.keyboard || typeof game.keyboard !== 'object') invalid();
        if (game.status === 'exhausted'
            && (game.outcome !== null || game.answer !== null || game.rows.length || game.draft)) invalid();
        if (state.availability !== undefined && (!state.availability
            || !Number.isInteger(state.availability.remaining) || state.availability.remaining < 0
            || typeof state.availability.exhausted !== 'boolean'
            || state.availability.exhausted !== (state.availability.remaining === 0))) invalid();
        for (const row of game.rows) {
            if (!row || typeof row.word !== 'string' || !/^[А-Я]{5}$/u.test(row.word)
                || !Array.isArray(row.states) || row.states.length !== WORD_LENGTH
                || row.states.some(value => !STATES.has(value))) invalid();
        }
        if (state.storage === 'server') {
            if (!Number.isInteger(state.revision) || state.revision < 0 || state.history !== undefined) invalid();
        } else {
            try { validateHistory(state.history); } catch (_) { invalid(); }
        }
        if (!stats || !['played', 'wins', 'strict_streak', 'win_streak', 'max_streak', 'win_rate']
            .every(key => Number.isFinite(stats[key]) && stats[key] >= 0)
            || !(stats.average === null || (Number.isFinite(stats.average) && stats.average >= 0))
            || !(stats.score === undefined || stats.score === null
                || (Number.isInteger(stats.score) && stats.score >= 0 && stats.score <= 10000))
            || !Array.isArray(stats.distribution) || stats.distribution.length !== ROW_COUNT
            || stats.distribution.some(count => !Number.isInteger(count) || count < 0)) invalid();
        if (state.progression !== undefined) {
            const progress = state.progression;
            if (!progress || !Number.isInteger(progress.total_wins) || progress.total_wins < 0
                || !Array.isArray(progress.nodes) || progress.nodes.length !== 5
                || progress.nodes.some(node => !node || !Number.isInteger(node.number) || node.number < 1
                    || typeof node.complete !== 'boolean'
                    || (node.complete ? typeof node.word !== 'string' || !/^[А-Я]{5}$/u.test(node.word) : node.word !== null))) invalid();
        }
        return state;
    }

    /** Передаёт данные только на игровые адреса этого же приложения. */
    async function requestApi(fetchFunction, endpoint, payload) {
        if (!['/api/state', '/api/guess', '/api/next', '/api/draft'].includes(endpoint)) throw new Error('Unsupported API endpoint');
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 12000);
        try {
            const response = await fetchFunction(endpoint, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                credentials: 'same-origin', mode: 'same-origin', redirect: 'error', cache: 'no-store',
                body: JSON.stringify(payload), signal: controller.signal,
            });
            let result;
            try { result = await response.json(); } catch (_) {
                throw new LocalApiError('unavailable', 'Локальный игровой сервер недоступен', response.status);
            }
            if (!response.ok) {
                const error = result?.error;
                throw new LocalApiError(
                    error?.code || 'request_failed',
                    typeof error?.message === 'string' ? error.message : 'Не удалось выполнить запрос',
                    response.status, result?.state || null,
                );
            }
            return validateState(result);
        } catch (error) {
            if (error instanceof LocalApiError) throw error;
            if (error?.code === 'authentication_failed') throw new LocalApiError(error.code, error.message, 401);
            throw new LocalApiError('unavailable', 'Локальный игровой сервер недоступен');
        } finally { clearTimeout(timer); }
    }

    function signature(history) { return JSON.stringify(history); }
    function gameIdentity(game) { return `${game.day}:${game.index ?? 0}`; }
    function formatProgressWord(word) { return word ? word.toLocaleUpperCase('ru-RU') : '*****'; }
    function activeRecord(history, game) {
        const today = history.filter(record => record.day === game.day);
        const last = today.reduce((latest, record) => !latest || (record.index ?? 0) > (latest.index ?? 0) ? record : latest, null);
        return last && gameIdentity(last) === gameIdentity(game) ? last : null;
    }

    /** Празднуем каждую свежую победу, но не повторное открытие результата. */
    function shouldCelebrate(game, source) {
        return source === 'guess' && game.outcome === 'win';
    }

    /** Показывает оставшееся время без отрицательных секунд. */
    function formatCountdown(expiresAt, nowMillis) {
        const seconds = Math.max(0, Math.ceil((Date.parse(expiresAt) - nowMillis) / 1000));
        if (!Number.isFinite(seconds)) return '00:00:00';
        return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
            .map(value => String(value).padStart(2, '0')).join(':');
    }

    /** Координирует API и историю, не вычисляя ответ или раскраску букв. */
    function createGameClient({request, store, locks, onState = () => {}, onBusy = () => {}, onMessage = () => {}}) {
        let state = null;
        let busy = false;
        const locked = action => locks?.request
            ? locks.request(STORAGE_KEY, {mode: 'exclusive'}, action) : action();
        function accept(next, source) {
            validateState(next);
            store.write(next.history);
            state = next;
            onState(state, source);
            return state;
        }
        async function refreshLocked() {
            for (let attempt = 0; attempt < 2; attempt += 1) {
                const history = store.read();
                const next = await request('/api/state', {history});
                if (signature(store.read()) === signature(history)) return accept(next, 'refresh');
            }
            throw new LocalApiError('conflict', 'Партия изменилась в другой вкладке. Повторите действие.');
        }
        async function run(action) {
            if (busy) return null;
            busy = true;
            onBusy(true);
            try { return await locked(action); }
            finally { busy = false; onBusy(false); }
        }
        return {
            get state() { return state; },
            get busy() { return busy; },
            refresh() { return run(refreshLocked); },
            setDraft(value) {
                const draft = normalizeDraft(value);
                if (!state || busy || state.game.outcome || state.game.status === 'exhausted' || draft === null) return false;
                const history = store.read();
                const current = activeRecord(history, state.game);
                if (!current || signature(current.guesses) !== signature(state.game.guesses)) return false;
                current.draft = draft;
                store.write(history);
                state = {...state, history, game: {...state.game, draft}};
                onState(state, 'draft');
                return true;
            },
            submit() { return changeGame(false); },
            nextGame() { return changeGame(true); },
        };

        function changeGame(advance) {
            return run(async () => {
                if (!state || state.game.status === 'exhausted'
                    || (advance ? !state.game.outcome : state.game.outcome)) return state;
                const history = store.read();
                const current = activeRecord(history, state.game);
                if (!current || signature(current.guesses) !== signature(state.game.guesses)) {
                    const next = await refreshLocked();
                    onMessage('Партия обновлена в другой вкладке.');
                    return next;
                }
                const before = signature(history);
                try {
                    const payload = {history, day: state.day, index: state.game.index ?? 0};
                    if (!advance) payload.guess = state.game.draft;
                    const next = await request(advance ? '/api/next' : '/api/guess', payload);
                    if (signature(store.read()) !== before) {
                        const latest = await refreshLocked();
                        onMessage('Партия обновлена в другой вкладке.');
                        return latest;
                    }
                    return accept(next, advance ? 'next' : 'guess');
                } catch (error) {
                    if (['day_changed', 'game_changed'].includes(error.code) && error.state) {
                        const next = signature(store.read()) === before
                            ? accept(error.state, 'day_changed') : await refreshLocked();
                        onMessage(error.code === 'day_changed' ? 'Начался новый игровой день' : 'Партия обновлена в другой вкладке.');
                        return next;
                    }
                    throw error;
                }
            });
        }
    }

    /** Форматирует только показатели, вычисленные Python-сервером. */
    function formatStatistics(stats) {
        const decimal = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        return {
            played: String(stats.played), 'win-rate': stats.played ? `${decimal.format(stats.win_rate)}%` : '0%',
            'win-streak': String(stats.win_streak), score: stats.score == null ? '—' : String(stats.score),
            average: stats.average === null ? '—' : decimal.format(stats.average),
            'max-streak': String(stats.max_streak),
        };
    }

    /** Подключает локальную игру, анимации и экраны без сервисов бота. */
    function mount(doc, search = '') {
        const win = doc.defaultView;
        const board = doc.getElementById('board');
        board.tabIndex = -1;
        const keyboard = doc.getElementById('keyboard');
        const toast = doc.getElementById('toast');
        const shell = doc.querySelector('.app-shell');
        const screens = Object.fromEntries(['game', 'stats', 'settings', 'rules', 'summary', 'exhausted', 'closed']
            .map(name => [name, doc.getElementById(`${name}-screen`)]));
        const confetti = doc.getElementById('confetti');
        const reducedMotion = win.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        let screen = initialScreen(search);
        let state = null;
        let toastTimer;
        let invalidTimer;
        let invalidRow = -1;
        let stickyWordError = false;
        let storageWarningUntil = 0;
        let revealActive = false;
        let revealTimers = [];
        let endTimer;
        let summaryReady = false;
        let confettiTimer;
        let dayTimer;
        let storageTimer;
        let countdownTimer;
        let serverClock = {at: 0, received: 0};
        let lastRefreshAt = -Infinity;
        let closing = false;
        let inputQueue = [];
        let drainingInput = false;
        let client;

        function clearMessage(keepStorageWarning = false) {
            if (keepStorageWarning && preserveStorageWarning(toast.textContent, win.performance.now(), storageWarningUntil)) return;
            win.clearTimeout(toastTimer);
            win.clearTimeout(invalidTimer);
            toast.classList.remove('is-visible', 'toast--word-error');
            toast.textContent = '';
            stickyWordError = false;
            invalidRow = -1;
            board.querySelectorAll('.tile--invalid').forEach(tile => tile.classList.remove('tile--invalid'));
            storageWarningUntil = 0;
        }
        function showMessage(message) {
            clearMessage();
            toast.textContent = message;
            toast.classList.add('is-visible');
            storageWarningUntil = message === STORAGE_WARNING ? win.performance.now() + 4200 : 0;
            toastTimer = win.setTimeout(clearMessage, 4200);
        }
        function positionWordError() {
            const row = board.children[invalidRow];
            if (!row?.firstElementChild) return;
            const first = row.firstElementChild.getBoundingClientRect();
            const last = row.lastElementChild.getBoundingClientRect();
            const x = Math.max(130, Math.min(win.innerWidth - 130, (first.left + last.right) / 2));
            const y = Math.max(75, Math.min(win.innerHeight - 125, first.bottom + 14));
            toast.style.setProperty('--toast-x', `${x}px`);
            toast.style.setProperty('--toast-y', `${y}px`);
        }
        function showWordError() {
            clearMessage();
            invalidRow = state.game.rows.length;
            stickyWordError = true;
            const row = board.children[invalidRow];
            row?.querySelectorAll('.tile').forEach(tile => tile.classList.add('tile--invalid'));
            invalidTimer = win.setTimeout(() => {
                row?.querySelectorAll('.tile').forEach(tile => tile.classList.remove('tile--invalid'));
                if (!stickyWordError) return;
                toast.textContent = 'Введите существительное из пяти букв';
                toast.classList.add('toast--word-error');
                positionWordError();
                toast.classList.add('is-visible');
            }, reducedMotion ? 0 : 520);
        }
        function cancelReveal() {
            revealTimers.forEach(timer => win.clearTimeout(timer));
            revealTimers = [];
            revealActive = false;
        }
        function stopConfetti() {
            win.clearTimeout(confettiTimer);
            confetti.replaceChildren();
        }
        function celebrate() {
            if (reducedMotion) return;
            stopConfetti();
            const fragment = doc.createDocumentFragment();
            for (let index = 0; index < 65; index += 1) {
                const piece = doc.createElement('span');
                piece.className = 'confetti-piece';
                const values = {
                    x: `${Math.random() * 100}%`, color: ['#fedd2c', '#ffffff', '#d6c15a'][index % 3],
                    delay: `${Math.random() * 280}ms`, duration: `${1600 + Math.random() * 700}ms`,
                    drift: `${(Math.random() - 0.5) * 170}px`, rotation: `${Math.random() * 720}deg`,
                };
                for (const [name, value] of Object.entries(values)) piece.style.setProperty(`--confetti-${name}`, value);
                fragment.appendChild(piece);
            }
            confetti.appendChild(fragment);
            confettiTimer = win.setTimeout(stopConfetti, 2900);
        }
        function renderBoard(animatedRow = -1) {
            cancelReveal();
            const game = state?.game;
            const fragment = doc.createDocumentFragment();
            const animatedTiles = [];
            for (let rowIndex = 0; rowIndex < ROW_COUNT; rowIndex += 1) {
                const row = doc.createElement('div');
                row.className = 'board-row';
                row.setAttribute('role', 'row');
                for (let column = 0; column < WORD_LENGTH; column += 1) {
                    const accepted = game?.rows[rowIndex];
                    const isDraft = game && !game.outcome && rowIndex === game.rows.length;
                    const letter = accepted ? accepted.word[column] : isDraft ? game.draft[column] : '';
                    const finalState = accepted ? accepted.states[column] : letter ? 'filled' : 'empty';
                    const animated = accepted && rowIndex === animatedRow && !reducedMotion;
                    const tileState = animated ? 'filled' : finalState;
                    const tile = doc.createElement('div');
                    tile.className = `tile tile--${tileState}`;
                    tile.dataset.state = tileState;
                    tile.setAttribute('role', 'gridcell');
                    tile.setAttribute('aria-label', `Ряд ${rowIndex + 1}, клетка ${column + 1}: ${letter || 'пусто'}${letter ? `, ${STATE_LABELS[finalState]}` : ''}`);
                    tile.textContent = letter || '';
                    if (isDraft && column === Math.min(game.draft.length, WORD_LENGTH - 1)) tile.classList.add('tile--active');
                    if (animated) animatedTiles.push({tile, finalState, column});
                    row.appendChild(tile);
                }
                fragment.appendChild(row);
            }
            board.replaceChildren(fragment);
            if (animatedTiles.length) {
                revealActive = true;
                for (const {tile, finalState, column} of animatedTiles) {
                    const delay = column * 160;
                    revealTimers.push(win.setTimeout(() => tile.classList.add('tile--reveal'), delay));
                    revealTimers.push(win.setTimeout(() => {
                        tile.classList.remove('tile--filled');
                        tile.classList.add(`tile--${finalState}`);
                        tile.dataset.state = finalState;
                    }, delay + 250));
                    revealTimers.push(win.setTimeout(() => tile.classList.remove('tile--reveal'), delay + 510));
                }
                revealTimers.push(win.setTimeout(() => {
                    revealActive = false;
                    revealTimers = [];
                    renderKeyboard();
                    drainInputQueue();
                }, 1160));
            }
        }
        function renderDraftRow() {
            const game = state?.game;
            if (!game || game.outcome) return;
            const rowIndex = game.rows.length;
            const row = board.children[rowIndex];
            if (!row) return;
            [...row.children].forEach((tile, column) => {
                const letter = game.draft[column] || '';
                const tileState = letter ? 'filled' : 'empty';
                tile.className = `tile tile--${tileState}`;
                tile.dataset.state = tileState;
                tile.textContent = letter;
                tile.setAttribute('aria-label', `Ряд ${rowIndex + 1}, клетка ${column + 1}: ${letter || 'пусто'}${letter ? ', ввод' : ''}`);
                if (column === Math.min(game.draft.length, WORD_LENGTH - 1)) tile.classList.add('tile--active');
            });
        }
        function makeKey(letter, action) {
            const key = doc.createElement('button');
            key.type = 'button';
            key.className = 'key';
            key.disabled = !state || client.busy || Boolean(state.game.outcome)
                || state.game.status === 'exhausted' || (action === 'enter' && revealActive);
            if (action) {
                key.classList.add(`key--${action}`);
                key.dataset.keyAction = action;
                key.setAttribute('aria-label', action === 'enter' ? 'Подтвердить слово' : 'Удалить букву');
                key.innerHTML = action === 'enter' ? ENTER_ICON : DELETE_ICON;
            } else {
                key.textContent = letter;
                key.dataset.letter = letter;
                const keyState = state?.game.keyboard[letter];
                key.dataset.state = keyState || 'empty';
                key.setAttribute('aria-label', `${letter}${keyState ? `, ${STATE_LABELS[keyState]}` : ''}`);
                if (keyState) key.classList.add(`key--${keyState}`);
            }
            return key;
        }
        function renderKeyboard() {
            const restoreFocus = keyboard.contains(doc.activeElement);
            const fragment = doc.createDocumentFragment();
            KEYBOARD_ROWS.forEach((letters, index) => {
                const row = doc.createElement('div');
                row.className = `keyboard-row keyboard-row--${index + 1}`;
                if (index === 2) row.appendChild(makeKey('', 'enter'));
                for (const letter of letters) row.appendChild(makeKey(letter));
                if (index === 2) row.appendChild(makeKey('', 'delete'));
                fragment.appendChild(row);
            });
            keyboard.replaceChildren(fragment);
            if (restoreFocus && screen === 'game') board.focus({preventScroll: true});
            board.setAttribute('aria-busy', String(client.busy || revealActive));
            const replay = doc.querySelector('[data-action="next-game"]');
            replay.disabled = !state?.game.outcome || client.busy || state?.availability?.exhausted === true;
            replay.textContent = state?.availability?.exhausted ? 'Все слова сыграны' : 'Играть ещё раз';
        }
        function renderStatistics() {
            if (!state) return;
            const values = formatStatistics(state.stats);
            for (const [name, value] of Object.entries(values)) doc.querySelector(`[data-stat="${name}"]`).textContent = value;
            const fragment = doc.createDocumentFragment();
            const maximum = Math.max(...state.stats.distribution, 1);
            state.stats.distribution.forEach((count, index) => {
                const row = doc.createElement('div');
                row.className = 'distribution-row';
                row.setAttribute('role', 'listitem');
                row.setAttribute('aria-label', `Попытка ${index + 1}: ${count} побед`);
                const label = doc.createElement('span');
                label.className = 'distribution-label';
                label.textContent = String(index + 1);
                const track = doc.createElement('div');
                track.className = 'distribution-track';
                const bar = doc.createElement('div');
                bar.className = 'distribution-bar';
                bar.dataset.empty = String(count === 0);
                if (!count) bar.classList.add('distribution-bar--empty');
                bar.style.width = count ? `${count / maximum * 100}%` : '0%';
                const value = doc.createElement('span');
                value.className = 'distribution-value distribution-count';
                value.textContent = String(count);
                bar.appendChild(value);
                track.appendChild(bar);
                row.append(label, track);
                fragment.appendChild(row);
            });
            doc.getElementById('distribution').replaceChildren(fragment);
        }
        function renderSummary() {
            if (!state?.game.outcome) return;
            const won = state.game.outcome === 'win';
            doc.getElementById('result-title').textContent = won ? 'Слово отгадано!' : 'Попытки закончились';
            doc.getElementById('result-text').textContent = `Слово: ${state.game.answer}`;
            doc.getElementById('result-attempt-count').textContent = String(state.game.guesses.length);
            const fragment = doc.createDocumentFragment();
            for (const item of state.progression?.nodes || []) {
                const node = doc.createElement('div');
                node.className = `progress-node${item.complete ? ' is-complete' : ''}`;
                node.setAttribute('role', 'listitem');
                const point = doc.createElement('span');
                point.className = 'progress-point';
                point.textContent = item.complete ? '✓' : String(item.number);
                const word = doc.createElement('span');
                word.className = 'progress-word';
                word.textContent = formatProgressWord(item.word);
                node.setAttribute('aria-label', item.complete ? `${item.number}: ${item.word}, отгадано` : `${item.number}: ещё не отгадано`);
                node.append(point, word);
                fragment.appendChild(node);
            }
            doc.getElementById('progress-chain').replaceChildren(fragment);
        }
        function showScreen(next) {
            clearMessage(true);
            if (next !== 'game') stopConfetti();
            if (revealActive) { cancelReveal(); renderBoard(); renderKeyboard(); }
            screen = next;
            if (screen !== 'game') inputQueue = [];
            shell.classList.toggle('app-shell--closed', next === 'closed');
            for (const [name, element] of Object.entries(screens)) element.hidden = name !== screen;
            doc.body.dataset.screen = screen;
            if (screen === 'game') board.focus({preventScroll: true});
            doc.querySelectorAll('[data-action="open-stats"], [data-action="open-settings"]').forEach(button => {
                button.setAttribute('aria-pressed', String(button.dataset.action === `open-${screen}`));
            });
        }
        function returnToGame() {
            showScreen(state?.game.status === 'exhausted' ? 'exhausted'
                : state?.game.outcome && summaryReady ? 'summary' : 'game');
        }
        function refresh() {
            if (doc.visibilityState === 'hidden' || client.busy) return;
            lastRefreshAt = win.performance.now();
            client.refresh().catch(error => {
                showMessage(error.message);
                win.clearTimeout(dayTimer);
                dayTimer = win.setTimeout(refresh, 30000);
            });
        }
        function serverNow() {
            return serverClock.at + win.performance.now() - serverClock.received;
        }
        function updateCountdown() {
            if (state) doc.getElementById('next-word-countdown').textContent = formatCountdown(state.expires_at, serverNow());
        }
        function scheduleDayRefresh(next) {
            win.clearTimeout(dayTimer);
            const received = Date.parse(next.server_time);
            serverClock = {at: Number.isFinite(received) ? received : Date.now(), received: win.performance.now()};
            const remaining = Date.parse(next.expires_at) - serverNow();
            dayTimer = win.setTimeout(refresh, Math.min(Math.max(remaining + 250, 1000), 86400500));
            updateCountdown();
            if (!countdownTimer) countdownTimer = win.setInterval(updateCountdown, 1000);
        }

        let storage;
        const serverMode = win.WORDLI_MODE === 'telegram';
        if (!serverMode) { try { storage = win.localStorage; } catch (_) { storage = null; } }
        const store = serverMode ? null : createHistoryStore(storage, message => showMessage(message));
        const options = {
            store, locks: win.navigator.locks,
            request: (endpoint, payload) => {
                return requestApi(serverMode ? win.WordliTelegram.createAuthenticatedFetch(win) : win.fetch.bind(win), endpoint, payload);
            },
            validateState, normalizeDraft,
            onBusy: busy => { renderKeyboard(); if (!busy) drainInputQueue(); }, onMessage: message => showMessage(message),
            onState(next, source) {
                const previous = state;
                const changedGame = previous && gameIdentity(previous.game) !== gameIdentity(next.game);
                const changedDay = previous && previous.day !== next.day;
                const changedRows = !previous || changedGame || signature(previous.game.guesses) !== signature(next.game.guesses);
                state = next;
                if (source !== 'draft') scheduleDayRefresh(next);
                if (changedGame) {
                    inputQueue = [];
                    win.clearTimeout(endTimer);
                    stopConfetti();
                    summaryReady = false;
                    if (['summary', 'game'].includes(screen)) showScreen('game');
                }
                if (changedDay) showMessage('Начался новый игровой день');
                // Фоновое обновление той же партии не обрывает раскрытие букв.
                if (revealActive && !changedRows) {
                    renderDraftRow();
                } else {
                    renderBoard(source === 'guess' ? next.game.rows.length - 1 : -1);
                }
                renderKeyboard();
                renderStatistics();
                renderSummary();
                if (next.game.status === 'exhausted' && ['game', 'summary'].includes(screen)) showScreen('exhausted');
                else if (next.game.status !== 'exhausted' && screen === 'exhausted') returnToGame();
                const justFinished = next.game.outcome && (!previous || changedGame || previous.game.outcome !== next.game.outcome);
                if (justFinished) {
                    if (source === 'guess') {
                        if (shouldCelebrate(next.game, source)) {
                            const identity = gameIdentity(next.game);
                            revealTimers.push(win.setTimeout(() => {
                                if (screen === 'game' && state && gameIdentity(state.game) === identity) celebrate();
                            }, reducedMotion ? 0 : 1165));
                        }
                        endTimer = win.setTimeout(() => {
                            summaryReady = true;
                            if (screen === 'game') showScreen('summary');
                        }, 3500);
                    } else {
                        summaryReady = true;
                        if (screen === 'game') showScreen('summary');
                    }
                }
            },
        };
        client = serverMode ? win.WordliTelegram.createServerGameClient(options) : createGameClient(options);
        if (serverMode) win.Telegram?.WebApp?.ready?.();
        function drainInputQueue() {
            if (drainingInput || !state || client.busy || screen !== 'game') return;
            if (state.game.outcome) { inputQueue = []; return; }
            drainingInput = true;
            try {
                while (inputQueue.length && !client.busy && !state.game.outcome) {
                    const next = inputQueue[0];
                    if (next.action === 'submit' && revealActive) break;
                    inputQueue.shift();
                    if (next.action === 'submit') submitNow();
                    else applyDraftEdit(next.action, next.letter);
                }
            } finally { drainingInput = false; }
        }
        function queueInput(action, letter = '') {
            if (!state || state.game.outcome || screen !== 'game' || closing) return;
            const lastInput = inputQueue[inputQueue.length - 1];
            if (action === 'submit' && (lastInput?.action === 'submit' || (client.busy && !inputQueue.length))) return;
            if (inputQueue.length < 64) inputQueue.push({action, letter});
            drainInputQueue();
        }
        function editDraft(action, letter = '') { queueInput(action, letter); }
        function submit() { queueInput('submit'); }
        function applyDraftEdit(action, letter = '') {
            if (!state || client.busy || state.game.outcome || screen !== 'game') return;
            clearMessage(true);
            const normalized = normalizeDraft(letter);
            const draft = action === 'delete' ? state.game.draft.slice(0, -1)
                : normalized === null ? state.game.draft : state.game.draft + normalized;
            if (draft.length <= WORD_LENGTH && !client.setDraft(draft)) refresh();
        }
        function submitNow() {
            if (!state || client.busy || state.game.outcome || screen !== 'game') return;
            clearMessage(true);
            client.submit().catch(error => {
                if (['unknown_word', 'invalid_word'].includes(error.code)) showWordError();
                else showMessage(error.message);
            });
        }
        async function closeApplication() {
            if (closing) return;
            closing = true;
            if (serverMode) {
                try { await client.flush(); }
                catch (error) { closing = false; showMessage(error.message); return; }
            }
            clearMessage(true);
            stopConfetti();
            if (typeof win.Telegram?.WebApp?.close === 'function') {
                try { win.Telegram.WebApp.close(); } catch (_) { /* Локальный просмотр остаётся доступен. */ }
            } else if (win.opener) {
                try { win.close(); } catch (_) { /* Обычные вкладки браузер может не закрыть. */ }
            }
            showScreen('closed');
            closing = false;
        }
        // Ошибка слова живёт до следующего действия, а не до таймера.
        doc.addEventListener('pointerdown', () => { if (stickyWordError) clearMessage(true); }, true);
        doc.addEventListener('keydown', () => { if (stickyWordError) clearMessage(true); }, true);
        doc.addEventListener('paste', () => { if (stickyWordError) clearMessage(true); }, true);
        doc.addEventListener('wheel', () => { if (stickyWordError) clearMessage(true); }, {passive: true});
        doc.addEventListener('click', event => {
            if (stickyWordError) clearMessage(true);
            const button = event.target?.closest?.('button');
            if (!button || button.disabled) return;
            const action = button.dataset.action;
            if (action === 'open-stats') showScreen('stats');
            else if (action === 'open-settings') showScreen('settings');
            else if (action === 'open-rules') showScreen('rules');
            else if (action === 'back-settings') showScreen('settings');
            else if (action === 'back-game' || action === 'reopen-app') returnToGame();
            else if (action === 'close-app') closeApplication();
            else if (action === 'next-game') client.nextGame().catch(error => showMessage(error.message));
            else if (button.dataset.letter) editDraft('letter', button.dataset.letter);
            else if (button.dataset.keyAction === 'enter') submit();
            else if (button.dataset.keyAction === 'delete') editDraft('delete');
        });
        doc.addEventListener('keydown', event => {
            if (event.ctrlKey || event.altKey || event.metaKey || event.isComposing) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                if (screen === 'rules') showScreen('settings');
                else if (screen !== 'game') returnToGame();
                return;
            }
            if (screen !== 'game') return;
            if (event.key === 'Enter' && event.target?.closest?.('button[data-action]')) return;
            if (event.key === 'Enter' || event.key === 'Backspace') {
                event.preventDefault();
                if (event.key === 'Enter') submit(); else editDraft('delete');
            } else if (/^[а-яё]$/iu.test(event.key)) {
                event.preventDefault();
                editDraft('letter', event.key);
            }
        });
        doc.addEventListener('paste', event => {
            if (screen !== 'game' || !state || client.busy || revealActive || state.game.outcome) return;
            event.preventDefault();
            const draft = normalizeDraft(event.clipboardData?.getData('text/plain') || '');
            if (draft === null) showMessage('Введите до пяти русских букв');
            else if (!client.setDraft(draft)) refresh();
        });
        win.addEventListener('resize', () => { if (stickyWordError) positionWordError(); });
        win.addEventListener('focus', () => { if (win.performance.now() - lastRefreshAt > 1000) refresh(); });
        doc.addEventListener('visibilitychange', () => {
            if (doc.visibilityState === 'visible') refresh();
            else client.flush?.().catch(() => {});
        });
        win.addEventListener('pagehide', () => client.flush?.().catch(() => {}));
        win.addEventListener('storage', event => {
            if (event.key !== STORAGE_KEY && event.key !== null) return;
            win.clearTimeout(storageTimer);
            storageTimer = win.setTimeout(refresh, 150);
        });
        renderBoard();
        renderKeyboard();
        showScreen(screen);
        if (!['http:', 'https:'].includes(win.location.protocol)) showMessage('Локальный игровой сервер недоступен');
        else refresh();
        return client;
    }


    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            STORAGE_KEY, LocalApiError, initialScreen, normalizeDraft, parseSavedHistory,
            createHistoryStore, preserveStorageWarning, validateState, requestApi, createGameClient, formatStatistics,
            gameIdentity, formatProgressWord, shouldCelebrate, formatCountdown, mount,
        };
    }
    if (typeof window !== 'undefined' && typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => mount(document, window.location.search), {once: true});
        } else mount(document, window.location.search);
    }
}());
