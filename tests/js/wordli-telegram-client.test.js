// Серверный клиент проверяется без Telegram, сети, браузерного хранилища и БД.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../../prototypes/wordli/prototype.js');
const {createAuthenticatedFetch, createServerGameClient} = require('../../prototypes/wordli/telegram_client.js');

const DAY = '2026-11-05';
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function stateFor({revision = 0, index = 0, guesses = [], draft = '', outcome = null, day = DAY} = {}) {
    return {
        storage: 'server', revision, day,
        server_time: `${day}T12:00:00Z`, expires_at: '2026-11-06T00:00:00Z',
        dictionary: {id: 'synthetic-test', size: 2},
        game: {
            day, index, guesses: [...guesses], draft,
            status: outcome === 'win' ? 'won' : outcome === 'loss' ? 'lost' : 'in_progress',
            rows: guesses.map(word => ({word, states: Array(5).fill(outcome === 'win' ? 'correct' : 'absent')})),
            outcome, answer: outcome ? 'ТАЧКА' : null, keyboard: {},
        },
        stats: {played: 0, wins: 0, strict_streak: 0, win_streak: 0, max_streak: 0, average: null, win_rate: 0, distribution: [0, 0, 0, 0, 0, 0]},
    };
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
    return {promise, resolve, reject};
}

function makeClient(handler, options = {}) {
    const calls = [];
    const states = [];
    const messages = [];
    const busy = [];
    let counter = 0;
    const client = createServerGameClient({
        request: async (endpoint, payload) => {
            calls.push({endpoint, payload: structuredClone(payload)});
            return handler(endpoint, payload);
        },
        validateState: ui.validateState, normalizeDraft: ui.normalizeDraft,
        createId: () => `00000000-0000-4000-8000-${String(++counter).padStart(12, '0')}`,
        onState: (state, source) => states.push({state, source}),
        onBusy: value => busy.push(value), onMessage: message => messages.push(message),
        ...options,
    });
    return {client, calls, states, messages, busy};
}

test('серверный клиент сохраняет и восстанавливает пустые позиции черновика', async () => {
    let stored = stateFor({draft: ' А К'});
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') stored = stateFor({draft: payload.draft, revision: stored.revision + 1});
        return structuredClone(stored);
    });
    await client.refresh();
    assert.equal(client.state.game.draft, ' А К');
    assert.equal(client.setDraft('   К'), true);
    await client.flush();
    assert.equal(calls.find(call => call.endpoint === '/api/draft').payload.draft, '   К');
    assert.equal(client.state.game.draft, '   К');
    await client.refresh();
    assert.equal(client.state.game.draft, '   К');
});

test('исчерпанный серверный профиль не отправляет черновик или новую партию', async () => {
    const state = stateFor();
    state.game.status = 'exhausted';
    state.availability = {remaining: 0, exhausted: true};
    const {client, calls} = makeClient(async () => state);
    await client.refresh();
    assert.equal(client.setDraft('КОШКА'), false);
    await client.submit();
    await client.nextGame();
    await client.flush();
    assert.deepEqual(calls.map(item => item.endpoint), ['/api/state']);
});

test('без initData выдаётся понятная ошибка до обращения к сети', async () => {
    for (const Telegram of [undefined, {WebApp: {}}, {WebApp: {initData: ''}}]) {
        const fetch = createAuthenticatedFetch({Telegram, fetch: () => assert.fail('Сеть недопустима')});
        await assert.rejects(fetch('/api/state', {headers: {}, body: '{}'}), error =>
            error.code === 'authentication_failed' && /«5БУКВ» в боте/.test(error.message));
    }
});

test('подпись Telegram идёт только в заголовок, а не в тело или URL', async () => {
    const initData = 'query_id=synthetic&user=%7B%22id%22%3A111%7D&hash=fake';
    const calls = [];
    const win = {
        Telegram: {WebApp: {initData, initDataUnsafe: {user: {id: 999}}}},
        fetch: async (endpoint, options) => { calls.push({endpoint, options}); return 'response'; },
    };
    const fetch = createAuthenticatedFetch(win);
    const body = JSON.stringify({day: DAY, index: 0});
    assert.equal(await fetch('/api/guess', {headers: {'Content-Type': 'application/json', 'X-Telegram-Init-Data': 'spoofed-header'}, body}), 'response');
    assert.equal(calls[0].endpoint, '/api/guess');
    assert.equal(calls[0].options.headers['X-Telegram-Init-Data'], initData);
    assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
    assert.equal(calls[0].options.body, body);
    assert.doesNotMatch(calls[0].options.body, /initData|user_id|999/);
});

test('серверное состояние принимается без истории и с обязательной ревизией', () => {
    const valid = stateFor();
    assert.equal(ui.validateState(valid), valid);
    assert.equal(valid.history, undefined);
    for (const revision of [undefined, null, -1, 0.5, false]) {
        assert.throws(() => ui.validateState({...valid, revision}), /состояние игры/);
    }
    assert.throws(() => ui.validateState({...valid, history: []}), /состояние игры/);
});

test('серверный клиент не читает localStorage и не отправляет историю или user_id', async () => {
    const source = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/telegram_client.js'), 'utf8');
    assert.doesNotMatch(source.replace(/\/\*[\s\S]*?\*\//g, ''), /(?:localStorage|sessionStorage)\s*(?:\.|\[)/);
    const {client, calls} = makeClient(async () => stateFor());
    await client.refresh();
    assert.deepEqual(calls, [{endpoint: '/api/state', payload: {}}]);
    assert.equal(client.state.storage, 'server');
    assert.equal(client.state.history, undefined);
});

test('черновик отправляется после паузы 250 мс одним последним значением', async () => {
    let canonical = stateFor();
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') canonical = stateFor({revision: canonical.revision + 1, draft: payload.draft});
        return canonical;
    });
    await client.refresh();
    client.setDraft('КО');
    client.setDraft('КОШ');
    await wait(80);
    assert.equal(calls.length, 1);
    await wait(240);
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state', '/api/draft']);
    assert.deepEqual(calls[1].payload, {day: DAY, index: 0, revision: 0, draft: 'КОШ'});
    assert.equal(client.state.game.draft, 'КОШ');
});

test('Enter сначала сохраняет черновик, затем отправляет ход с новой ревизией', async () => {
    let canonical = stateFor();
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') canonical = stateFor({revision: 1, draft: payload.draft});
        if (endpoint === '/api/guess') canonical = stateFor({revision: 2, guesses: [payload.guess]});
        return canonical;
    });
    await client.refresh();
    client.setDraft('КОШКА');
    await client.submit();
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state', '/api/draft', '/api/guess', '/api/state']);
    assert.equal(calls[2].payload.revision, 1);
    assert.equal(calls[2].payload.guess, 'КОШКА');
    assert.match(calls[2].payload.request_id, /^[0-9a-f-]{36}$/);
    assert.equal('history' in calls[2].payload, false);
    assert.equal('user_id' in calls[2].payload, false);
    assert.equal(client.state.revision, 2);
});

test('ввод во время сохранения черновика не затирается старым ответом', async () => {
    const save = deferred();
    let drafts = 0;
    let canonical = stateFor();
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') {
            drafts += 1;
            if (drafts === 1) return save.promise;
            canonical = stateFor({revision: 2, draft: payload.draft});
        }
        return canonical;
    });
    await client.refresh();
    client.setDraft('КО');
    await wait(280);
    assert.equal(drafts, 1);
    assert.equal(client.setDraft('КОШ'), true);
    save.resolve(stateFor({revision: 1, draft: 'КО'}));
    await wait(30);
    assert.equal(client.state.game.draft, 'КОШ');
    assert.equal(client.state.revision, 2);
    assert.deepEqual(calls.filter(call => call.endpoint === '/api/draft').map(call => call.payload.draft), ['КО', 'КОШ']);
    await client.refresh();
});

test('конфликт черновика принимает серверную версию и не отправляет устаревший ход', async () => {
    const canonical = stateFor({revision: 3, guesses: ['ПАРОМ'], draft: 'БИ'});
    const {client, calls, messages} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return stateFor();
        throw new ui.LocalApiError('version_conflict', 'Версия изменилась', 409, canonical);
    });
    await client.refresh();
    client.setDraft('КОШКА');
    await client.submit();
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state', '/api/draft']);
    assert.equal(client.state.revision, 3);
    assert.equal(client.state.game.draft, 'БИ');
    assert.equal(messages.length, 1);
});

test('после неизвестного слова новый ход получает новый request_id', async () => {
    let canonical = stateFor();
    let attempts = 0;
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') canonical = stateFor({revision: canonical.revision + 1, draft: payload.draft});
        if (endpoint === '/api/guess') {
            attempts += 1;
            if (attempts === 1) throw new ui.LocalApiError('unknown_word', 'Нет в словаре', 400);
            canonical = stateFor({revision: canonical.revision + 1, guesses: [payload.guess]});
        }
        return canonical;
    });
    await client.refresh();
    client.setDraft('АБВГД');
    await assert.rejects(client.submit(), /Нет в словаре/);
    assert.equal(client.state.game.draft, 'АБВГД');
    assert.equal(client.setDraft('КОШКА'), true);
    await client.submit();
    const guesses = calls.filter(call => call.endpoint === '/api/guess');
    assert.equal(guesses.length, 2);
    assert.notEqual(guesses[0].payload.request_id, guesses[1].payload.request_id);
    assert.equal(guesses[1].payload.guess, 'КОШКА');
});

test('повтор после сетевого сбоя использует тот же UUID и тот же payload', async () => {
    let canonical = stateFor();
    let attempts = 0;
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') canonical = stateFor({revision: 1, draft: payload.draft});
        if (endpoint === '/api/guess') {
            attempts += 1;
            if (attempts === 1) throw new ui.LocalApiError('unavailable', 'Сеть недоступна');
            canonical = stateFor({revision: 2, guesses: [payload.guess]});
        }
        return canonical;
    });
    await client.refresh();
    client.setDraft('КОШКА');
    await assert.rejects(client.submit(), /Сеть недоступна/);
    assert.equal(client.setDraft('ПАРОМ'), false);
    await client.submit();
    const guesses = calls.filter(call => call.endpoint === '/api/guess');
    assert.equal(guesses.length, 2);
    assert.deepEqual(guesses[0].payload, guesses[1].payload);
    assert.equal(client.state.game.guesses[0], 'КОШКА');
});

test('двойное нажатие Играть ещё раз не создаёт двух операций', async () => {
    const replay = deferred();
    const initial = stateFor({revision: 1, guesses: ['ТАЧКА'], outcome: 'win'});
    const next = stateFor({revision: 2, index: 1});
    let states = 0;
    const {client, calls} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return ++states === 1 ? initial : next;
        return replay.promise;
    });
    await client.refresh();
    const pending = client.nextGame();
    assert.equal(await client.nextGame(), null);
    replay.resolve(next);
    await pending;
    assert.equal(calls.filter(call => call.endpoint === '/api/next').length, 1);
    assert.equal(client.state.game.index, 1);
});

test('после идемпотентного ответа побеждает более новая версия с другого устройства', async () => {
    const initial = stateFor({revision: 1, guesses: ['ТАЧКА'], outcome: 'win'});
    const ledger = stateFor({revision: 2, index: 1});
    const latest = stateFor({revision: 5, index: 1, guesses: ['ПАРОМ'], draft: 'КО'});
    let reads = 0;
    const {client, states} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return ++reads === 1 ? initial : latest;
        return ledger;
    });
    await client.refresh();
    await client.nextGame();
    assert.equal(client.state.revision, 5);
    assert.deepEqual(client.state.game.guesses, ['ПАРОМ']);
    assert.equal(client.state.game.draft, 'КО');
    assert.equal(states.at(-1).source, 'refresh');
});

test('отказ контрольного чтения после принятого хода сохраняет UUID для безопасного повтора', async () => {
    const initial = stateFor({revision: 1, draft: 'КОШКА'});
    const accepted = stateFor({revision: 2, guesses: ['КОШКА']});
    let reads = 0;
    const {client, calls} = makeClient(async endpoint => {
        if (endpoint === '/api/state') {
            reads += 1;
            if (reads === 1) return initial;
            if (reads === 2) throw new ui.LocalApiError('unavailable', 'Контрольное чтение недоступно');
            return accepted;
        }
        return accepted;
    });
    await client.refresh();
    await assert.rejects(client.submit(), /Контрольное чтение/);
    assert.equal(client.setDraft('ПАРОМ'), false);
    await client.submit();
    const guesses = calls.filter(call => call.endpoint === '/api/guess');
    assert.equal(guesses.length, 2);
    assert.deepEqual(guesses[0].payload, guesses[1].payload);
    assert.equal(client.state.revision, 2);
});

test('без готовой партии и при неверном алфавите мутации не отправляются', async () => {
    const {client, calls} = makeClient(async () => stateFor());
    assert.equal(client.setDraft('КОШ'), false);
    await client.submit();
    assert.deepEqual(calls, []);
    await client.refresh();
    for (const raw of ['LATIN', '12345', 'КОШКАА', null]) assert.equal(client.setDraft(raw), false);
    await client.nextGame();
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state']);
});

test('публичный flush сохраняет черновик сразу, не дожидаясь 250 мс и не дублируя таймер', async () => {
    let canonical = stateFor();
    const {client, calls} = makeClient(async (endpoint, payload) => {
        if (endpoint === '/api/draft') canonical = stateFor({revision: 1, draft: payload.draft});
        return canonical;
    });
    await client.refresh();
    assert.equal(client.setDraft('КОШ'), true);
    assert.equal(calls.length, 1);
    await client.flush();
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state', '/api/draft']);
    assert.equal(calls[1].payload.draft, 'КОШ');
    assert.equal(client.state.revision, 1);
    await wait(280);
    assert.equal(calls.filter(call => call.endpoint === '/api/draft').length, 1);
});

test('keepalive сохраняет подпись и JSON-заголовки при отправке перед закрытием', async () => {
    const calls = [];
    const initData = 'query_id=keepalive-synthetic&hash=fake';
    const fetch = createAuthenticatedFetch({
        Telegram: {WebApp: {initData}},
        fetch: async (endpoint, options) => { calls.push({endpoint, options}); return {ok: true}; },
    });
    const body = JSON.stringify({day: DAY, index: 0, revision: 2, draft: 'КОШ'});
    await fetch('/api/draft', {
        method: 'POST', mode: 'same-origin', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Telegram-Init-Data': 'spoofed'},
        body, keepalive: false,
    });
    assert.equal(calls[0].options.keepalive, true);
    assert.equal(calls[0].options.headers['X-Telegram-Init-Data'], initData);
    assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
    assert.equal(calls[0].options.mode, 'same-origin');
    assert.equal(calls[0].options.credentials, 'same-origin');
    assert.equal(calls[0].options.body, body);
    assert.equal(calls[0].endpoint, '/api/draft');
});
