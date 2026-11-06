/* Серверное сохранение: Telegram подтверждает личность, правила проверяет Python. */
(function () {
    'use strict';

    function createAuthenticatedFetch(win) {
        return (endpoint, options) => {
            const initData = win.Telegram?.WebApp?.initData;
            if (typeof initData !== 'string' || !initData) {
                const error = new Error('Откройте игру кнопкой «5БУКВ» в боте.');
                error.code = 'authentication_failed';
                return Promise.reject(error);
            }
            return win.fetch(endpoint, {...options, keepalive: true, headers: {...options.headers, 'X-Telegram-Init-Data': initData}});
        };
    }

    /** Последовательно сохраняет ходы и черновик, не используя localStorage. */
    function createServerGameClient({request, validateState, normalizeDraft, onState = () => {}, onBusy = () => {}, onMessage = () => {}, createId = () => crypto.randomUUID()}) {
        let state = null;
        let busy = false;
        let queue = Promise.resolve();
        let pendingDraft = null;
        let draftTimer;
        let pendingCommand = null;
        const identity = game => `${game.day}:${game.index}`;
        const enqueue = action => {
            const result = queue.then(action);
            queue = result.catch(() => {});
            return result;
        };
        function accept(next, source, preserveDraft = false) {
            validateState(next);
            if (next.storage !== 'server') throw new Error('Ожидалось серверное сохранение');
            if (preserveDraft && pendingDraft !== null && state && identity(state.game) === identity(next.game) && !next.game.outcome) {
                next = {...next, game: {...next.game, draft: pendingDraft}};
            }
            state = next;
            onState(next, source);
            return next;
        }
        async function conflict(error) {
            pendingDraft = null;
            pendingCommand = null;
            const next = error.state || await request('/api/state', {});
            accept(next, 'refresh');
            onMessage(error.code === 'day_changed' ? 'Начался новый игровой день' : 'Партия обновлена с другого устройства.');
            return false;
        }
        const isConflict = error => ['version_conflict', 'game_changed', 'day_changed'].includes(error.code);
        async function flushDraft() {
            clearTimeout(draftTimer);
            while (pendingDraft !== null && state && !state.game.outcome && state.game.status !== 'exhausted') {
                const draft = pendingDraft;
                pendingDraft = null;
                const payload = {day: state.day, index: state.game.index, revision: state.revision, draft};
                try {
                    accept(await request('/api/draft', payload), 'draft_saved', true);
                } catch (error) {
                    if (isConflict(error)) return conflict(error);
                    if (pendingDraft === null) pendingDraft = draft;
                    throw error;
                }
            }
            return true;
        }
        async function run(action) {
            if (busy) return null;
            busy = true;
            onBusy(true);
            try { return await enqueue(action); }
            finally { busy = false; onBusy(false); }
        }
        async function command(action) {
            if (!state || state.game.status === 'exhausted'
                || (action === 'next' ? !state.game.outcome : state.game.outcome)) return state;
            if (!await flushDraft()) return state;
            if (!pendingCommand) {
                pendingCommand = {
                    action, payload: {day: state.day, index: state.game.index, revision: state.revision, request_id: createId()},
                };
                if (action === 'guess') pendingCommand.payload.guess = state.game.draft;
            }
            const operation = pendingCommand;
            try {
                const next = await request(`/api/${operation.action}`, operation.payload);
                // Идемпотентный ответ старого запроса не должен откатить новое устройство.
                const latest = await request('/api/state', {});
                pendingCommand = null;
                return accept(latest.revision > next.revision ? latest : next, latest.revision > next.revision ? 'refresh' : action);
            } catch (error) {
                if (isConflict(error)) { await conflict(error); return state; }
                if (error.code && error.code !== 'unavailable') pendingCommand = null;
                throw error;
            }
        }
        return {
            get state() { return state; },
            get busy() { return busy; },
            refresh() {
                return run(async () => {
                    if (!await flushDraft()) return state;
                    const next = await request('/api/state', {});
                    if (pendingCommand && (next.revision !== pendingCommand.payload.revision || identity(next.game) !== `${pendingCommand.payload.day}:${pendingCommand.payload.index}`)) pendingCommand = null;
                    return accept(next, 'refresh');
                });
            },
            setDraft(value) {
                const draft = normalizeDraft(value);
                if (!state || busy || state.game.outcome || state.game.status === 'exhausted'
                    || draft === null || pendingCommand) return false;
                pendingDraft = draft;
                state = {...state, game: {...state.game, draft}};
                onState(state, 'draft');
                clearTimeout(draftTimer);
                draftTimer = setTimeout(() => enqueue(flushDraft).catch(error => onMessage(error.message)), 250);
                return true;
            },
            submit() { return run(() => command('guess')); },
            nextGame() { return run(() => command('next')); },
            flush() { return enqueue(flushDraft); },
        };
    }

    if (typeof module !== 'undefined' && module.exports) module.exports = {createAuthenticatedFetch, createServerGameClient};
    if (typeof window !== 'undefined') window.WordliTelegram = {createAuthenticatedFetch, createServerGameClient};
}());
