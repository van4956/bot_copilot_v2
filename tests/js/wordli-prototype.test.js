// Проверки интерфейса без ботов, БД и настоящих HTTP-запросов.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../../prototypes/wordli/prototype.js');
const DAY = '2026-11-05';

test('цепочка показывает заглавные слова, будущие не превращаются в текстовые звёздочки', () => {
    assert.equal(ui.formatProgressWord('кросс'), 'КРОСС');
    assert.equal(ui.formatProgressWord('ЗАПАЛ'), 'ЗАПАЛ');
    assert.equal(ui.formatProgressWord(null), '');
});

test('будущее слово состоит из пяти отдельных кружков, отгаданное остаётся текстом', () => {
    const doc = {createElement: tag => ({
        tagName: tag, children: [], attributes: {}, textContent: '',
        appendChild(child) {this.children.push(child);},
        setAttribute(name, value) {this.attributes[name] = value;},
    })};
    const hidden = ui.createProgressWord(doc, null);
    assert.equal(hidden.className, 'progress-word progress-word--pending');
    assert.equal(hidden.attributes['aria-hidden'], 'true');
    assert.equal(hidden.textContent, '');
    assert.equal(hidden.children.length, 5);
    assert(hidden.children.every(circle => circle.tagName === 'span'
        && circle.className === 'progress-placeholder' && circle.textContent === ''));
    const solved = ui.createProgressWord(doc, 'кросс');
    assert.equal(solved.className, 'progress-word');
    assert.equal(solved.textContent, 'КРОСС');
    assert.equal(solved.children.length, 0);
});

function stateFor({guesses = [], draft = '', outcome = null, day = DAY, index = 0, history} = {}) {
    return {
        day, server_time: `${day}T12:00:00Z`, expires_at: '2026-11-06T00:00:00Z',
        dictionary: {id: 'test', size: 2},
        history: history || [{day, index, guesses: [...guesses], draft}],
        game: {
            day, index, guesses: [...guesses], draft,
            status: outcome === 'win' ? 'won' : outcome === 'loss' ? 'lost' : 'in_progress',
            rows: guesses.map(word => ({word, states: Array(5).fill(outcome === 'win' ? 'correct' : 'absent')})),
            outcome, answer: outcome ? 'ТАЧКА' : null, keyboard: {},
        },
        stats: {played: 0, wins: 0, strict_streak: 0, win_streak: 0, max_streak: 0, average: null, win_rate: 0, distribution: [0, 0, 0, 0, 0, 0]},
    };
}

function fakeStorage(initial = null) {
    let raw = initial;
    return {getItem: () => raw, setItem: (_, value) => { raw = value; }, get raw() { return raw; }};
}

function makeClient(request, storage = fakeStorage(), options = {}) {
    const warnings = [];
    const messages = [];
    const states = [];
    const store = ui.createHistoryStore(storage, message => warnings.push(message));
    const client = ui.createGameClient({
        request, store, onMessage: message => messages.push(message),
        onState: (state, source) => states.push({state, source}), ...options,
    });
    return {client, store, storage, warnings, messages, states};
}

test('query выбирает только навигацию, не подменяет игру или статистику', () => {
    assert.equal(ui.initialScreen('?scene=stats'), 'stats');
    assert.equal(ui.initialScreen('?scene=settings'), 'settings');
    for (const scene of ['empty', 'win', 'loss', '<script>']) {
        assert.equal(ui.initialScreen(`?scene=${scene}&stats=empty`), 'game');
    }
});

test('ввод нормализует Ё, но не принимает латиницу, числа и лишние буквы', () => {
    assert.equal(ui.normalizeDraft('ёжик  '), 'ЕЖИК');
    assert.equal(ui.normalizeDraft(' е'), ' Е');
    assert.equal(ui.normalizeDraft('КО ШК'), 'КО ШК');
    assert.equal(ui.normalizeDraft('елка\u0301'), null);
    assert.equal(ui.normalizeDraft(''), '');
    for (const value of ['КОШКАА', 'КOШКА', '12345', '  ёжик  ', '\tКОТ', null]) assert.equal(ui.normalizeDraft(value), null);
});

test('локальное сохранение имеет отдельное имя и явную версию', () => {
    assert.equal(ui.STORAGE_KEY, 'wordli.local.v01');
    const history = [{day: DAY, guesses: ['ПАРОМ'], draft: 'ТАЧ'}];
    assert.deepEqual(ui.parseSavedHistory(JSON.stringify({schema_version: 1, history})), [{...history[0], index: 0}]);
    assert.deepEqual(ui.parseSavedHistory(null), []);
    for (const raw of ['broken', '{}', 'null', JSON.stringify({schema_version: 4, history}),
        JSON.stringify({schema_version: 1, history: [{day: DAY, guesses: ['LATIN'], draft: ''}]})]) {
        assert.throws(() => ui.parseSavedHistory(raw));
    }
});

test('повреждённое сохранение не затирается при переходе к памяти', () => {
    const storage = fakeStorage('not-json');
    const warnings = [];
    const store = ui.createHistoryStore(storage, value => warnings.push(value));
    assert.deepEqual(store.read(), []);
    store.write([{day: DAY, guesses: [], draft: 'КОШКА'}]);
    assert.equal(storage.raw, 'not-json');
    assert.equal(store.persistent, false);
    assert.equal(warnings.length, 1);
    assert.equal(store.read()[0].draft, 'КОШКА');
});

test('отказ localStorage предупреждается, но не останавливает текущую игру', () => {
    const warnings = [];
    const store = ui.createHistoryStore({
        getItem() { return null; }, setItem() { throw new Error('QuotaExceededError'); },
    }, value => warnings.push(value));
    store.read();
    store.write([{day: DAY, guesses: [], draft: ''}]);
    assert.equal(store.persistent, false);
    assert.equal(warnings.length, 1);
    assert.equal(store.read().length, 1);
});

test('предупреждение о несохранении защищено от очистки при наборе на 4200 мс', () => {
    const warnings = [];
    ui.createHistoryStore(null, message => warnings.push(message)).read();
    const shownAt = 100;
    const until = shownAt + 4200;
    assert.equal(ui.preserveStorageWarning(warnings[0], shownAt + 100, until), true);
    assert.equal(ui.preserveStorageWarning(warnings[0], until - 1, until), true);
    assert.equal(ui.preserveStorageWarning(warnings[0], until, until), false);
    assert.equal(ui.preserveStorageWarning('Такого слова нет в словаре.', shownAt + 100, until), false);
});

test('ответ сервера проверяется до отрисовки', () => {
    assert.equal(ui.validateState(stateFor()).day, DAY);
    assert.throws(() => ui.validateState({game: {}}), /состояние игры/);
    const bad = stateFor({guesses: ['ПАРОМ']});
    bad.game.rows[0].states[0] = '<script>';
    assert.throws(() => ui.validateState(bad), /состояние игры/);
});

test('дельта принимает целые значения и сохраняет совместимость старого ответа', () => {
    const original = stateFor();
    original.stats = {...original.stats, played: 6, score: 787};
    for (const score_delta of [undefined, null, 0, 15, -7, 10000, -10000]) {
        const candidate = {...original, stats: {...original.stats, score_delta}};
        assert.equal(ui.validateState(candidate), candidate);
    }
    for (const score_delta of ['15', true, [], {}, 0.5, NaN, Infinity, 10001, -10001]) {
        assert.throws(() => ui.validateState({...original, stats: {...original.stats, score_delta}}));
    }
    assert.throws(() => ui.validateState({...original, stats: {...original.stats, score: null, score_delta: 15}}));
    assert.equal(ui.validateState(stateFor()).stats.score_delta, undefined);
});

test('дельта форматируется со знаком, нулевая и первое начисление скрываются', () => {
    const stats = {played: 6, score: 787};
    assert.equal(ui.formatScoreDelta({...stats, score_delta: 15}), '+15');
    assert.equal(ui.formatScoreDelta({...stats, score_delta: -7}), '−7');
    assert.equal(ui.formatScoreDelta({...stats, score_delta: 117}), '+117');
    for (const score_delta of [undefined, null, 0, -0, '15', true, 1.5, NaN, Infinity, 10001]) {
        assert.equal(ui.formatScoreDelta({...stats, score_delta}), '');
    }
    for (const played of [0, 4, 5]) {
        assert.equal(ui.formatScoreDelta({...stats, played, score_delta: 117}), '');
    }
    assert.equal(ui.formatScoreDelta({...stats, score: null, score_delta: 117}), '');
    assert.equal(ui.formatScoreDelta({...stats, score: undefined, score_delta: 117}), '');
    assert.equal(ui.formatScoreDelta({...stats, score_delta: 15}), '+15');
    assert.equal(ui.formatScoreDelta(stats), '');
});

test('API получает только разрешённый относительный адрес и JSON истории', async () => {
    const calls = [];
    const state = stateFor();
    const result = await ui.requestApi(async (...args) => {
        calls.push(args);
        return {ok: true, status: 200, json: async () => state};
    }, '/api/state', {history: []});
    assert.equal(result, state);
    assert.equal(calls[0][0], '/api/state');
    assert.equal(calls[0][1].mode, 'same-origin');
    assert.equal(calls[0][1].redirect, 'error');
    assert.deepEqual(JSON.parse(calls[0][1].body), {history: []});
    await assert.rejects(ui.requestApi(() => assert.fail('network not expected'), 'https://example.com/api/state', {}), /Unsupported/);
});

test('ошибка словаря сохраняет код сервера и не становится успешным ходом', async () => {
    const fetch = async () => ({ok: false, status: 400, json: async () => ({error: {code: 'unknown_word', message: 'Такого слова нет в словаре'}})});
    await assert.rejects(ui.requestApi(fetch, '/api/guess', {}), error => error.code === 'unknown_word' && error.status === 400);
});

test('старый статический сервер и отказ сети показываются как недоступный API', async () => {
    const html = async () => ({ok: false, status: 404, json: async () => { throw new Error('HTML'); }});
    for (const fetch of [html, async () => { throw new Error('NetworkError'); }]) {
        await assert.rejects(ui.requestApi(fetch, '/api/state', {}), error => error.code === 'unavailable');
    }
});

test('черновик сохраняется без запроса к серверу и без новой попытки', async () => {
    let calls = 0;
    const {client, storage} = makeClient(async () => { calls += 1; return stateFor(); });
    await client.refresh();
    assert.equal(client.setDraft('кошка'), true);
    assert.equal(client.state.game.draft, 'КОШКА');
    assert.equal(calls, 1);
    assert.equal(JSON.parse(storage.raw).history[0].draft, 'КОШКА');
    assert.deepEqual(client.state.game.guesses, []);
});

test('после хода хранятся канонические попытки Python и его результат', async () => {
    const calls = [];
    const {client, store} = makeClient(async (endpoint, payload) => {
        calls.push({endpoint, payload});
        return endpoint === '/api/state' ? stateFor() : stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    });
    await client.refresh();
    client.setDraft('ТАЧКА');
    await client.submit();
    assert.equal(calls[1].endpoint, '/api/guess');
    assert.equal(calls[1].payload.guess, 'ТАЧКА');
    assert.equal(calls[1].payload.index, 0);
    assert.equal(client.state.game.outcome, 'win');
    assert.deepEqual(store.read()[0].guesses, ['ТАЧКА']);
    assert.equal(client.setDraft('КОШКА'), false);
});

test('двойной Enter не создаёт второго параллельного запроса', async () => {
    let complete;
    let guesses = 0;
    const {client} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return stateFor();
        guesses += 1;
        return new Promise(resolve => { complete = resolve; });
    });
    await client.refresh();
    client.setDraft('КОШКА');
    const first = client.submit();
    assert.equal(client.busy, true);
    assert.equal(await client.submit(), null);
    complete(stateFor({guesses: ['КОШКА']}));
    await first;
    assert.equal(guesses, 1);
    assert.equal(client.busy, false);
});

test('ошибка слова оставляет черновик и пустую историю попыток', async () => {
    const {client, store} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return stateFor();
        throw new ui.LocalApiError('unknown_word', 'Такого слова нет в словаре', 400);
    });
    await client.refresh();
    client.setDraft('АБВГД');
    await assert.rejects(client.submit(), /нет в словаре/);
    assert.equal(client.state.game.draft, 'АБВГД');
    assert.deepEqual(store.read()[0].guesses, []);
    assert.equal(client.busy, false);
});

test('смена дня принимает новое поле, не перенося туда старый ход', async () => {
    const next = stateFor({day: '2026-11-06'});
    next.expires_at = '2026-11-07T00:00:00Z';
    const {client, messages} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return stateFor();
        throw new ui.LocalApiError('day_changed', 'Новый день', 409, next);
    });
    await client.refresh();
    client.setDraft('КОШКА');
    await client.submit();
    assert.equal(client.state.day, '2026-11-06');
    assert.deepEqual(client.state.game.guesses, []);
    assert.deepEqual(messages, ['Начался новый игровой день']);
});

test('устаревшая вкладка обновляется, а не отправляет старый ход', async () => {
    const calls = [];
    const latest = stateFor({guesses: ['ПАРОМ']});
    const {client, storage, messages} = makeClient(async endpoint => {
        calls.push(endpoint);
        return calls.length === 1 ? stateFor() : latest;
    });
    await client.refresh();
    client.setDraft('КОШКА');
    storage.setItem(ui.STORAGE_KEY, JSON.stringify({schema_version: 1, history: latest.history}));
    await client.submit();
    assert.deepEqual(calls, ['/api/state', '/api/state']);
    assert.deepEqual(client.state.game.guesses, ['ПАРОМ']);
    assert.equal(messages.length, 1);
});

test('поздний day_changed не стирает первый ход нового дня из другой вкладки', async () => {
    let rejectLate;
    const calls = [];
    const old = stateFor();
    const emptyNext = stateFor({day: '2026-11-06'});
    emptyNext.expires_at = '2026-11-07T00:00:00Z';
    const latest = stateFor({day: '2026-11-06', guesses: ['ПАРОМ']});
    latest.expires_at = emptyNext.expires_at;
    latest.history.unshift({day: DAY, guesses: [], draft: 'КОШКА'});
    const {client, storage, store, messages} = makeClient(async (endpoint, payload) => {
        calls.push({endpoint, payload});
        if (endpoint === '/api/guess') return new Promise((_, reject) => { rejectLate = reject; });
        return calls.length === 1 ? old : latest;
    });
    await client.refresh();
    client.setDraft('КОШКА');
    const pending = client.submit();
    storage.setItem(ui.STORAGE_KEY, JSON.stringify({schema_version: 1, history: latest.history}));
    rejectLate(new ui.LocalApiError('day_changed', 'Новый день', 409, emptyNext));
    await pending;
    assert.equal(client.state.day, '2026-11-06');
    assert.deepEqual(client.state.game.guesses, ['ПАРОМ']);
    assert.deepEqual(store.read().find(game => game.day === '2026-11-06').guesses, ['ПАРОМ']);
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state', '/api/guess', '/api/state']);
    assert.deepEqual(calls[2].payload.history.find(game => game.day === '2026-11-06').guesses, ['ПАРОМ']);
    assert.deepEqual(messages, ['Начался новый игровой день']);
});

test('Web Locks использует собственное имя сохранения', async () => {
    const calls = [];
    const locks = {request(name, options, action) { calls.push({name, options}); return action(); }};
    const {client} = makeClient(async () => stateFor(), fakeStorage(), {locks});
    await client.refresh();
    assert.deepEqual(calls, [{name: ui.STORAGE_KEY, options: {mode: 'exclusive'}}]);
});

test('статистика форматирует серверные значения без демонстрационных чисел', () => {
    const stats = stateFor().stats;
    assert.deepEqual(ui.formatStatistics(stats), {played: '0', 'win-rate': '0%', score: '—', 'win-streak': '0', average: '—', 'max-streak': '0'});
    assert.deepEqual(ui.formatStatistics({...stats, played: 2, wins: 1, win_rate: 50, average: 4, strict_streak: 1, win_streak: 1, max_streak: 1}), {
        played: '2', 'win-rate': '50,0%', score: '—', 'win-streak': '1', average: '4,0', 'max-streak': '1',
    });
});

test('доля побед и средние попытки округляются до одного знака только при отображении', () => {
    const samples = [
        {win_rate: 94.29, average: 4.06, shownRate: '94,3%', shownAverage: '4,1'},
        {win_rate: 100, average: 1, shownRate: '100,0%', shownAverage: '1,0'},
        {win_rate: 0, average: null, shownRate: '0,0%', shownAverage: '—'},
        {win_rate: 99.95, average: 1.25, shownRate: '100,0%', shownAverage: '1,3'},
        {win_rate: 42.04, average: 4.04, shownRate: '42,0%', shownAverage: '4,0'},
        {win_rate: 50, average: 6, shownRate: '50,0%', shownAverage: '6,0'},
    ];
    for (const {win_rate, average, shownRate, shownAverage} of samples) {
        const stats = Object.freeze({...stateFor().stats, played: 35, win_rate, average, score: 821});
        const formatted = ui.formatStatistics(stats);
        assert.equal(formatted['win-rate'], shownRate);
        assert.equal(formatted.average, shownAverage);
        assert.equal(formatted.score, '821');
        assert.equal(stats.win_rate, win_rate);
        assert.equal(stats.average, average);
    }
});

test('очки отображаются целыми, до квалификации показывается прочерк', () => {
    const stats = stateFor().stats;
    assert.equal(ui.formatStatistics({...stats, score: null}).score, '—');
    assert.equal(ui.formatStatistics({...stats, score: 0}).score, '0');
    assert.equal(ui.formatStatistics({...stats, score: 10000}).score, '10000');
    for (const score of [-1, 10001, 1.5, '123', NaN]) {
        const state = stateFor();
        state.stats.score = score;
        assert.throws(() => ui.validateState(state), /состояние игры/);
    }
});

test('шесть карточек имеют согласованные подписи и порядок 3 на 2', () => {
    const html = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/index.html'), 'utf8');
    const labels = [...html.matchAll(/class="stat-label">([^<]+)</gu)].map(match => match[1]);
    assert.deepEqual(labels, ['Всего сыграно', 'Доля побед', 'Попыток на победу',
        'Максимальная серия', 'Текущая серия', 'Очков']);
    assert.doesNotMatch(html, /data-stat="strict-streak"/);
});

test('формат 3 сохраняет закреплённый ответ и словарь при записи и черновике', async () => {
    const pinned = {day: DAY, index: 0, guesses: [], draft: '', answer: 'ТАЧКА', dictionary_id: 'test-v1'};
    const storage = fakeStorage(JSON.stringify({schema_version: 3, history: [pinned]}));
    assert.deepEqual(ui.parseSavedHistory(storage.raw), [pinned]);
    const state = stateFor({history: [pinned]});
    const {client, store} = makeClient(async () => state, storage);
    await client.refresh();
    assert.equal(client.setDraft('ПАР'), true);
    assert.deepEqual(store.read(), [{...pinned, draft: 'ПАР'}]);
    assert.equal(JSON.parse(storage.raw).schema_version, 3);
    for (const patch of [{answer: 'LATIN'}, {dictionary_id: null}, {dictionary_id: ''}, {answer: undefined}]) {
        assert.throws(() => ui.parseSavedHistory(JSON.stringify({schema_version: 3, history: [{...pinned, ...patch}]})));
    }
});

test('исчерпанный словарь оставляет статистику доступной и запрещает ввод', async () => {
    const exhausted = stateFor({history: []});
    exhausted.game.status = 'exhausted';
    exhausted.availability = {remaining: 0, exhausted: true};
    const {client, states} = makeClient(async () => exhausted);
    await client.refresh();
    assert.equal(client.state.game.status, 'exhausted');
    assert.equal(client.setDraft('ПАРОМ'), false);
    await client.submit();
    await client.nextGame();
    assert.equal(states.length, 1);
    assert.equal(ui.formatStatistics(client.state.stats).played, '0');
    const broken = structuredClone(exhausted);
    broken.availability.remaining = 1;
    assert.throws(() => ui.validateState(broken), /состояние игры/);
});

test('интерфейс не содержит демоответа и не загружает SDK или внешнее API', () => {
    const source = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/prototype.js'), 'utf8');
    assert.doesNotMatch(source, /DEMO_WORD|EXAMPLE_ROWS|KEY_STATES|createDemoState|applyDemoInput|getDemoStats/);
    assert.doesNotMatch(source, /sendData|initData|telegram-web-app\.js|https?:\/\//);
    assert.doesNotMatch(source, /ТАЧКА/);
});
// Дополнительные партии сохраняются без сброса первой ежедневной игры.
test('версии сохранения 1 и 2 читаются совместимо, индекс не теряется', () => {
    const legacy = [{day: DAY, guesses: ['ПАРОМ'], draft: 'КОШ'}];
    const canonical = [{...legacy[0], index: 0}];
    assert.deepEqual(ui.parseSavedHistory(JSON.stringify({schema_version: 1, history: legacy})), canonical);
    const multiple = [...canonical, {day: DAY, index: 1, guesses: [], draft: 'БИ'}];
    assert.deepEqual(ui.parseSavedHistory(JSON.stringify({schema_version: 2, history: multiple})), multiple);
    const storage = fakeStorage();
    const store = ui.createHistoryStore(storage);
    store.write(multiple);
    assert.equal(JSON.parse(storage.raw).schema_version, 3);
    assert.deepEqual(store.read(), multiple);
});

test('идентичность партии учитывает индекс, а старую запись считает ежедневной', () => {
    assert.equal(ui.gameIdentity({day: DAY}), `${DAY}:0`);
    assert.equal(ui.gameIdentity({day: DAY, index: 0}), `${DAY}:0`);
    assert.equal(ui.gameIdentity({day: DAY, index: 1}), `${DAY}:1`);
});

test('конфетти после каждой новой победы, но не при повторном открытии', () => {
    for (const index of [0, 1, 2]) {
        for (const outcome of [null, 'win', 'loss']) {
            for (const source of ['guess', 'refresh', 'draft', 'next', 'day_changed']) {
                assert.equal(ui.shouldCelebrate({day: DAY, index, outcome}, source),
                    outcome === 'win' && source === 'guess');
            }
        }
    }
});

test('таймер до слова не отрицателен и имеет постоянный формат', () => {
    const midnight = Date.parse('2026-11-06T00:00:00Z');
    assert.equal(ui.formatCountdown('2026-11-06T00:00:00Z', midnight - 3723000), '01:02:03');
    assert.equal(ui.formatCountdown('2026-11-06T00:00:00Z', midnight), '00:00:00');
    assert.equal(ui.formatCountdown('2026-11-06T00:00:00Z', midnight + 999), '00:00:00');
});

test('API разрешает только собственный адрес следующей партии', async () => {
    const calls = [];
    const result = await ui.requestApi(async (endpoint, options) => {
        calls.push({endpoint, options});
        return {ok: true, status: 200, json: async () => stateFor({index: 1})};
    }, '/api/next', {history: [], day: DAY, index: 0});
    assert.equal(result.game.index, 1);
    assert.equal(calls[0].endpoint, '/api/next');
    assert.deepEqual(JSON.parse(calls[0].options.body), {history: [], day: DAY, index: 0});
    await assert.rejects(ui.requestApi(() => assert.fail('No network'), '/api/reset', {}), /Unsupported/);
});

test('повтор после завершения явно создаёт одну следующую партию', async () => {
    const first = stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    const second = stateFor({index: 1, history: [...first.history, {day: DAY, index: 1, guesses: [], draft: ''}]});
    const calls = [];
    const {client, store} = makeClient(async (endpoint, payload) => {
        calls.push({endpoint, payload});
        return endpoint === '/api/state' ? first : second;
    });
    await client.refresh();
    await client.nextGame();
    assert.deepEqual(calls.map(call => call.endpoint), ['/api/state', '/api/next']);
    assert.equal(calls[1].payload.day, DAY);
    assert.equal(calls[1].payload.index, 0);
    assert.equal(client.state.game.index, 1);
    assert.deepEqual(store.read()[0].guesses, ['ТАЧКА']);
    assert.equal(store.read().length, 2);
});

test('следующее слово нельзя пропустить двойным нажатием Играть ещё раз', async () => {
    let complete;
    let requests = 0;
    const first = stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    const second = stateFor({index: 1, history: [...first.history, {day: DAY, index: 1, guesses: [], draft: ''}]});
    const {client} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return first;
        requests += 1;
        return new Promise(resolve => { complete = resolve; });
    });
    await client.refresh();
    const pending = client.nextGame();
    assert.equal(client.busy, true);
    assert.equal(await client.nextGame(), null);
    complete(second);
    await pending;
    assert.equal(requests, 1);
    assert.equal(client.state.game.index, 1);
});

test('устаревшая кнопка повтора обновляет уже начатую партию из другой вкладки', async () => {
    const first = stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    const second = stateFor({index: 1, history: [...first.history, {day: DAY, index: 1, guesses: [], draft: 'БИ'}], draft: 'БИ'});
    const calls = [];
    const {client, storage, store} = makeClient(async endpoint => {
        calls.push(endpoint);
        return calls.length === 1 ? first : second;
    });
    await client.refresh();
    storage.setItem(ui.STORAGE_KEY, JSON.stringify({schema_version: 2, history: second.history}));
    await client.nextGame();
    assert.deepEqual(calls, ['/api/state', '/api/state']);
    assert.equal(client.state.game.index, 1);
    assert.equal(store.read()[1].draft, 'БИ');
});

test('черновик дополнительной партии не попадает в ежедневную запись', async () => {
    const history = [
        {day: DAY, index: 0, guesses: ['ТАЧКА'], draft: ''},
        {day: DAY, index: 1, guesses: [], draft: ''},
    ];
    const {client, store} = makeClient(async () => stateFor({index: 1, history}));
    await client.refresh();
    assert.equal(client.setDraft('КОШ'), true);
    assert.equal(store.read()[0].draft, '');
    assert.equal(store.read()[1].draft, 'КОШ');
});

test('при смене активного индекса чужой черновик не перезаписывается', async () => {
    const old = stateFor({index: 1, history: [
        {day: DAY, index: 0, guesses: ['ТАЧКА'], draft: ''},
        {day: DAY, index: 1, guesses: [], draft: ''},
    ]});
    const {client, storage, store} = makeClient(async () => old);
    await client.refresh();
    const latest = [
        ...old.history.slice(0, 1),
        {day: DAY, index: 1, guesses: ['КОШКА'], draft: ''},
        {day: DAY, index: 2, guesses: [], draft: 'БИ'},
    ];
    storage.setItem(ui.STORAGE_KEY, JSON.stringify({schema_version: 2, history: latest}));
    assert.equal(client.setDraft('ПАР'), false);
    assert.equal(store.read()[2].draft, 'БИ');
});

test('поздний ответ повтора не затирает ход уже принятой следующей партии', async () => {
    let complete;
    const first = stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    const empty = stateFor({index: 1, history: [...first.history, {day: DAY, index: 1, guesses: [], draft: ''}]});
    const latest = stateFor({index: 1, guesses: ['ПАРОМ'], history: [...first.history, {day: DAY, index: 1, guesses: ['ПАРОМ'], draft: ''}]});
    const calls = [];
    const {client, storage, store} = makeClient(async endpoint => {
        calls.push(endpoint);
        if (endpoint === '/api/next') return new Promise(resolve => { complete = resolve; });
        return calls.length === 1 ? first : latest;
    });
    await client.refresh();
    const pending = client.nextGame();
    storage.setItem(ui.STORAGE_KEY, JSON.stringify({schema_version: 2, history: latest.history}));
    complete(empty);
    await pending;
    assert.deepEqual(calls, ['/api/state', '/api/next', '/api/state']);
    assert.deepEqual(client.state.game.guesses, ['ПАРОМ']);
    assert.deepEqual(store.read()[1].guesses, ['ПАРОМ']);
});

test('кнопка повтора не запускает новую игру поверх незавершённой', async () => {
    const calls = [];
    const {client} = makeClient(async endpoint => { calls.push(endpoint); return stateFor(); });
    await client.refresh();
    await client.nextGame();
    assert.deepEqual(calls, ['/api/state']);
    assert.equal(client.state.game.index, 0);
});

test('отказ сети при повторе не теряет завершённую игру и позволяет повторить запрос', async () => {
    const first = stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    const second = stateFor({index: 1, history: [...first.history, {day: DAY, index: 1, guesses: [], draft: ''}]});
    let requests = 0;
    const {client, store} = makeClient(async endpoint => {
        if (endpoint === '/api/state') return first;
        requests += 1;
        if (requests === 1) throw new ui.LocalApiError('unavailable', 'Сервер недоступен');
        return second;
    });
    await client.refresh();
    await assert.rejects(client.nextGame(), /недоступен/);
    assert.equal(client.busy, false);
    assert.equal(client.state.game.index, 0);
    assert.equal(store.read().length, 1);
    assert.deepEqual(store.read()[0].guesses, ['ТАЧКА']);
    await client.nextGame();
    assert.equal(client.state.game.index, 1);
    assert.equal(store.read().length, 2);
});

test('ответ game_changed обновляет текущую партию, не отправляя второй ход', async () => {
    const first = stateFor({guesses: ['ТАЧКА'], outcome: 'win'});
    const second = stateFor({index: 1, history: [...first.history, {day: DAY, index: 1, guesses: [], draft: ''}]});
    const calls = [];
    const {client, messages} = makeClient(async endpoint => {
        calls.push(endpoint);
        if (endpoint === '/api/state') return first;
        throw new ui.LocalApiError('game_changed', 'Партия изменилась', 409, second);
    });
    await client.refresh();
    await client.nextGame();
    assert.equal(client.state.game.index, 1);
    assert.deepEqual(calls, ['/api/state', '/api/next']);
    assert.equal(messages.length, 1);
});

test('цепочка принимает пять корректных узлов и отклоняет повреждённые подсказки', () => {
    const progression = {
        total_wins: 1, start: 1, end: 5,
        nodes: Array.from({length: 5}, (_, index) => ({
            number: index + 1, complete: index === 0, word: index === 0 ? 'ТАЧКА' : null,
        })),
    };
    const valid = {...stateFor(), progression};
    assert.equal(ui.validateState(valid), valid);
    const variants = [
        {nodeIndex: 1, changes: {word: 'ПАРОМ'}},
        {nodeIndex: 0, changes: {word: ''}},
        {nodeIndex: 0, changes: {word: null}},
        {nodeIndex: 1, changes: {number: 0}},
        {nodeIndex: 1, changes: {number: 1.5}},
    ];
    for (const {nodeIndex, changes} of variants) {
        const malformed = {
            ...progression,
            nodes: progression.nodes.map((node, index) => index === nodeIndex ? {...node, ...changes} : {...node}),
        };
        assert.throws(() => ui.validateState({...stateFor(), progression: malformed}), /состояние игры/);
    }
});

test('индекс сохранённой партии обязан быть целым неотрицательным числом, отсутствие совместимо', () => {
    const legacy = {day: DAY, guesses: [], draft: 'БИ'};
    const parsed = ui.parseSavedHistory(JSON.stringify({schema_version: 1, history: [legacy]}));
    assert.equal(parsed[0].index, 0);
    for (const index of [null, -1, false, true, 0.5]) {
        assert.throws(() => ui.parseSavedHistory(JSON.stringify({
            schema_version: 2, history: [{...legacy, index}],
        })));
    }
});
