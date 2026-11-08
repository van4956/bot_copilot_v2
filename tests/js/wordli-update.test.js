// Проверки редактора, подсказок и публичных настроек без настоящих сервисов.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../../prototypes/wordli/prototype.js');

test('редактор сохраняет пустые позиции, нормализует Ё и не сдвигает буквы', () => {
    assert.equal(ui.normalizeDraft(' а  ё '), ' А  Е');
    assert.equal(ui.normalizeDraft('     '), '');
    assert.equal(ui.normalizeDraft('А\nБ'), null);
    assert.deepEqual(ui.editDraftCells('КОШКА', 0, 'letter', 'Л'), {draft: 'ЛОШКА', cursor: 1});
    assert.deepEqual(ui.editDraftCells('КОШКА', 2, 'letter', 'Ч'), {draft: 'КОЧКА', cursor: 3});
    assert.deepEqual(ui.editDraftCells('', 3, 'letter', 'Ё'), {draft: '   Е', cursor: 4});
    assert.deepEqual(ui.editDraftCells('КОШКА', 2, 'clear'), {draft: 'КО КА', cursor: 2});
    assert.deepEqual(ui.editDraftCells('КО КА', 2, 'letter', 'Ш'), {draft: 'КОШКА', cursor: 3});
});

test('Backspace очищает текущую букву или предыдущую пустой позиции', () => {
    assert.deepEqual(ui.editDraftCells('КОШКА', 4, 'delete'), {draft: 'КОШК', cursor: 4});
    assert.deepEqual(ui.editDraftCells('КОШК', 4, 'delete'), {draft: 'КОШ', cursor: 3});
    assert.deepEqual(ui.editDraftCells('КОШКА', 0, 'delete'), {draft: ' ОШКА', cursor: 0});
    assert.deepEqual(ui.editDraftCells('', 0, 'delete'), {draft: '', cursor: 0});
    assert.deepEqual(ui.editDraftCells('КОШКА', 2, 'letter', 'A'), {draft: 'КОШКА', cursor: 2});
});

test('курсором считается первая незаполненная позиция, а не конец строки', () => {
    assert.equal(ui.draftCursor(''), 0);
    assert.equal(ui.draftCursor('КО'), 2);
    assert.equal(ui.draftCursor('К ШКА'), 1);
    assert.equal(ui.draftCursor('КОШКА'), 4);
});

test('подсказки берутся только из точных публичных совпадений', () => {
    const hints = ui.correctHints([
        {word: 'ПАРОМ', states: ['absent', 'present', 'absent', 'absent', 'absent']},
        {word: 'БИЛЕТ', states: ['absent', 'correct', 'absent', 'present', 'present']},
    ]);
    assert.deepEqual(hints, ['', 'И', '', '', '']);
    assert.deepEqual(ui.correctHints(), ['', '', '', '', '']);
    assert.equal(ui.normalizeDraft(''), '');
});

test('дырявый черновик хранится и восстанавливается без сдвига позиций', () => {
    let raw = null;
    const store = ui.createHistoryStore({getItem: () => raw, setItem: (_, value) => {raw = value;}});
    store.write([{day: '2026-11-08', index: 0, guesses: [], draft: ' А К'}]);
    assert.equal(store.read()[0].draft, ' А К');
    assert.equal(ui.parseSavedHistory(raw)[0].draft, ' А К');
});

test('пояс браузера не требует геолокацию и имеет резерв UTC+4', () => {
    assert.equal(ui.browserTimezone({DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: 'Asia/Yerevan'})})}), 'Asia/Yerevan');
    assert.equal(ui.browserTimezone({DateTimeFormat: () => ({resolvedOptions: () => ({})})}), 'Etc/GMT-4');
    assert.equal(ui.browserTimezone({DateTimeFormat() {throw new Error('No timezone');}}), 'Etc/GMT-4');
});

test('рейтинг допускает одинаковые ранги, но отклоняет неверные публичные показатели', () => {
    const value = {rows: [
        {rank: 1, name: 'Имя', played: 12, score: 400, is_self: false},
        {rank: 1, name: '<img src=x onerror=alert(1)>', played: 12, score: 400, is_self: true},
    ], separator_after: null, total: 2, self_eligible: true};
    assert.equal(ui.validateLeaderboard(value), value);
    for (const patch of [{played: 9}, {score: -1}, {score: 10001}, {rank: 0}, {is_self: 1}]) {
        assert.throws(() => ui.validateLeaderboard({...value, rows: [{...value.rows[0], ...patch}]}));
    }
    assert.throws(() => ui.validateLeaderboard({...value, rows: Array(11).fill(value.rows[0])}));
});

test('API настроек и рейтинга проверяет собственные ответы, не ожидая игровую историю', async () => {
    const calls = [];
    const settings = {settings: {reminder_enabled: true, timezone: 'Asia/Yerevan'}};
    const fetch = async (endpoint, options) => {
        calls.push({endpoint, options});
        return {ok: true, status: 200, json: async () => settings};
    };
    assert.equal(await ui.requestApi(fetch, '/api/settings', {reminder_enabled: true}), settings);
    assert.equal(calls[0].options.mode, 'same-origin');
    assert.deepEqual(JSON.parse(calls[0].options.body), {reminder_enabled: true});
    await assert.rejects(ui.requestApi(async () => ({ok: true, json: async () => ({})}), '/api/leaderboard', {}), /рейтинг/);
    await assert.rejects(ui.requestApi(async () => ({ok: true, json: async () => ({settings: {reminder_enabled: 'true'}})}), '/api/settings', {}), /настройки/);
});

test('утверждённая страница формулы использует K и новые веса без префикса Очки=', () => {
    const html = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/index.html'), 'utf8');
    const formula = html.split('<section id="formula-screen"')[1].split('<section id="leaderboard-screen"')[0];
    assert.match(formula, /<b>M<\/b> — максимальная серия побед\.<\/li>\s*<li><b>K<\/b> — общий размер словаря\./);
    assert.match(formula, /src="assets\/score-formula\.png"[^>]*alt="10000 · √\(N\/K\) · P · \(0,4 \+ 0,4Q \+ 0,2√\(M\/K\)\)"/);
    assert.match(formula, /class="formula-quality" src="assets\/quality-formula\.png" alt="Q = \(7 − A\) \/ 6"/);
    assert.doesNotMatch(formula, /<math|<msqrt|<mfrac/);
    assert.doesNotMatch(formula, /<b>Q = \(7 − A\) \/ 6<\/b>/);
    assert.doesNotMatch(formula, /⌊|⌋|<mtext>Очки<\/mtext>|округление вниз/);
    assert.match(formula, /Очки появляются после 10 завершённых игр\./);
    assert.match(html, /data-action="open-formula"/);
    assert.match(html, /data-action="open-leaderboard"/);
});

test('раздел Смысл использует согласованную короткую строку и точки', () => {
    const html = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/index.html'), 'utf8');
    const sense = html.split('<section class="formula-sense">')[1].split('</section>')[0];
    assert.deepEqual([...sense.matchAll(/<li>(.*?)<\/li>/g)].map(match => match[1]), [
        'количество игр увеличивает очки.',
        'доля побед влияет напрямую.',
        'меньше попыток — выше результат.',
        'максимальная серия даёт дополнительный, но не решающий вклад.',
    ]);
    assert.doesNotMatch(sense, /;/);
});

test('обычная типографика Формулы совпадает с Правилами, без мелкого текста для Смысла', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/styles.css'), 'utf8');
    const body = css.match(/\.rules-content\.formula-content\s*\{([^}]+)\}/)[1];
    assert.match(body, /font-size:\s*18px/);
    assert.match(body, /line-height:\s*1\.45/);
    assert.match(css, /\.rules-content\.formula-content h2\s*\{[^}]*font-size:\s*24px;[^}]*font-weight:\s*400/);
    assert.match(css, /\.rules-content\.formula-content\s*\{[^}]*font-size:\s*17px/);
    assert.doesNotMatch(css, /\.formula-sense\s*\{[^}]*font-size:/);
    assert.match(css, /\.formula-content \.formula-note\s*\{[^}]*font-size:\s*15px/);
    assert.match(css, /\.formula-content \.formula-note\s*\{[^}]*font-size:\s*14px/);
    assert.match(css, /\.score-formula img\s*\{[^}]*width:\s*100%;[^}]*height:\s*auto/);
});

test('формулы поставляются готовыми прозрачными PNG без зависимости от шрифтов WebView', () => {
    for (const name of ['score-formula', 'quality-formula']) {
        const png = fs.readFileSync(path.resolve(__dirname, `../../prototypes/wordli/assets/${name}.png`));
        assert.equal(png.subarray(1, 4).toString(), 'PNG');
        assert.equal(png[25], 6); // RGBA: прозрачный фон, без растровой подложки.
        assert(png.readUInt32BE(16) >= 500);
        assert(png.readUInt32BE(20) >= 250);
    }
});
