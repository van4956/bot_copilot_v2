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
    for (const patch of [{played: 4}, {score: -1}, {score: 10001}, {rank: 0}, {is_self: 1}]) {
        assert.throws(() => ui.validateLeaderboard({...value, rows: [{...value.rows[0], ...patch}]}));
    }
    assert.throws(() => ui.validateLeaderboard({...value, rows: Array(11).fill(value.rows[0])}));
});

test('рейтинг принимает участника после пятой завершённой игры, включая нулевые очки', () => {
    const value = {rows: [
        {rank: 1, name: 'Игрок', played: 5, score: 0, is_self: true},
    ], separator_after: null, total: 1, self_eligible: true};
    for (const played of [5, 6, 9, 10]) {
        const candidate = {...value, rows: [{...value.rows[0], played}]};
        assert.equal(ui.validateLeaderboard(candidate), candidate);
    }
    for (const played of [0, 4, 4.9, 5.1, '5']) {
        assert.throws(() => ui.validateLeaderboard({...value, rows: [{...value.rows[0], played}]}));
    }
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
    assert.match(formula, /Очки появляются после 5 завершённых игр\./);
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
    assert.match(body, /font-size:\s*16px/);
    assert.match(body, /line-height:\s*1\.45/);
    assert.match(css, /\.rules-content\.formula-content h2\s*\{[^}]*font-size:\s*22px;[^}]*font-weight:\s*400/);
    assert.match(css, /\.rules-content h2\s*\{[^}]*font-size:\s*22px/);
    assert.match(css, /\.rules-content p\s*\{[^}]*font-size:\s*16px/);
    assert.match(css, /\.rules-content p\s*\{[^}]*font-size:\s*15px/);
    assert.match(css, /\.rules-content\.formula-content\s*\{[^}]*font-size:\s*15px/);
    assert.doesNotMatch(css, /\.formula-sense\s*\{[^}]*font-size:/);
    assert.match(css, /\.formula-content \.formula-note\s*\{[^}]*font-size:\s*13px/);
    assert.match(css, /\.formula-content \.formula-note\s*\{[^}]*font-size:\s*12px/);
    assert.match(css, /\.score-formula img\s*\{[^}]*width:\s*100%;[^}]*height:\s*auto/);
});

test('формула имеет светлую серую подложку без новой обводки и сохраняет PNG', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/styles.css'), 'utf8');
    const body = css.match(/\.score-formula\s*\{([^}]+)\}/)[1];
    const self = css.match(/\.leaderboard-self td\s*\{([^}]+)\}/)[1];
    const background = block => block.match(/background:\s*([^;]+)/)[1];
    assert.equal(background(body), background(self));
    assert.equal(background(body), '#ffffff09');
    assert.match(body, /min-width:\s*0/);
    assert.match(body, /padding:\s*16px 8px/);
    assert.match(body, /border-radius:\s*12px/);
    assert.doesNotMatch(body, /(?:^|;)\s*(?:border|outline|color)\s*:/);
    assert.match(css, /\.formula-quality\s*\{[^}]*height:\s*2\.2em;[^}]*vertical-align:\s*middle/);
});

test('собственная строка сохраняет подсветку без полосок, разделение остаётся только отступом', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/styles.css'), 'utf8');
    assert.match(css, /\.leaderboard-self td\s*\{[^}]*border-top:\s*0;[^}]*background:\s*#ffffff09/);
    assert.match(css, /\.leaderboard-self \+ tr:not\(\.leaderboard-separator\) td\s*\{\s*border-top:\s*0;/);
    assert.doesNotMatch(css, /\.leaderboard-self \+ tr td\s*\{/);
    assert.match(css, /\.leaderboard-separator td\s*\{[^}]*height:\s*15px;[^}]*border-top:\s*0/);
    assert.doesNotMatch(css, /\.leaderboard-separator td\s*\{[^}]*dashed/);
    assert.match(css, /\.leaderboard-table tbody > tr:first-child td, \.leaderboard-separator \+ tr td\s*\{\s*border-top:\s*0;/);
});

test('в рейтинге нет линий между любыми участниками, подсветка и отступ сохранены', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/styles.css'), 'utf8');
    const cells = css.match(/\.leaderboard-table td\s*\{([^}]+)\}/)[1];
    assert.match(cells, /border-top:\s*0;/);
    for (const [, selector, declarations] of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        if (!selector.includes('.leaderboard')) continue;
        for (const [, value] of declarations.matchAll(/\bborder(?:-(?:top|right|bottom|left))?\s*:\s*([^;]+)/g)) {
            assert.match(value.trim(), /^(?:0(?:px)?|none)(?:\s|$)/);
        }
    }
    assert.match(css, /\.leaderboard-self td\s*\{[^}]*background:\s*#ffffff09/);
    assert.match(css, /\.leaderboard-separator td\s*\{[^}]*height:\s*15px;[^}]*padding:\s*0;/);
});

test('имя рейтинга сохраняет 14 символов, две точки идут сверх лимита', () => {
    assert.equal(ui.LEADERBOARD_NAME_LIMIT, 14);
    assert.equal(ui.formatLeaderboardName('Ivan Goncharov'), 'Ivan Goncharov');
    assert.equal(ui.formatLeaderboardName('12345678901234'), '12345678901234');
    assert.equal(ui.formatLeaderboardName('123456789012345'), '12345678901234..');
    assert.equal(ui.formatLeaderboardName('Тестовый игрок 10'), 'Тестовый игрок..');
    assert.equal(ui.formatLeaderboardName('Alex'), 'Alex');
    assert.equal(ui.formatLeaderboardName(''), '');
});

test('Unicode-имена не режутся внутри emoji и комбинированных букв', () => {
    const family = '👨‍👩‍👧‍👦';
    const letter = 'е\u0301';
    assert.equal(ui.formatLeaderboardName(family.repeat(14)), family.repeat(14));
    assert.equal(ui.formatLeaderboardName(family.repeat(15)), family.repeat(14) + '..');
    assert.equal(ui.formatLeaderboardName(letter.repeat(15)), letter.repeat(14) + '..');
    assert.equal(ui.formatLeaderboardName('😀'.repeat(15), Infinity, null, {}), '😀'.repeat(14) + '..');
});

test('слишком широкие имена дополнительно сокращаются двумя точками без изменения источника', () => {
    const original = 'WWWWWWWWWWWWWWW';
    const measure = text => Array.from(text).reduce((width, letter) => width + (letter === '.' ? 2 : 10), 0);
    assert.equal(ui.formatLeaderboardName(original, 104, measure), 'WWWWWWWWWW..');
    assert.equal(ui.formatLeaderboardName(original, 144, measure), 'WWWWWWWWWWWWWW..');
    assert.equal(ui.formatLeaderboardName('Ivan Goncharov', 140, text => text.length * 10), 'Ivan Goncharov');
    assert.equal(original, 'WWWWWWWWWWWWWWW');
});

test('имя рейтинга не переносится и пересчитывается при изменении ширины таблицы', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/styles.css'), 'utf8');
    const js = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/prototype.js'), 'utf8');
    assert.match(css, /\.leaderboard-label\s*\{[^}]*min-width:\s*0;[^}]*white-space:\s*nowrap;[^}]*overflow:\s*hidden/);
    assert.match(js, /label\.dataset\.fullName = item\.name/);
    assert.match(js, /new win\.ResizeObserver\(fitLeaderboardNames\)/);
    assert.doesNotMatch(css, /\.leaderboard-name span:last-child[^}]*overflow-wrap:\s*anywhere/);
});

test('дельта мелкая и серая, её очистка не меняет центральное основное число', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/styles.css'), 'utf8');
    const html = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/index.html'), 'utf8');
    const js = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/prototype.js'), 'utf8');
    assert.match(html, /class="stat-score-number"><strong class="stat-value" data-stat="score"/);
    assert.match(html, /class="stat-score-delta" data-score-delta hidden/);
    assert.match(css, /\.stat-score-delta\s*\{[^}]*position:\s*absolute;[^}]*color:\s*#929292;[^}]*font-size:\s*10px/);
    assert.match(css, /\.stat-score-delta\s*\{[^}]*font-size:\s*8px/);
    assert.match(js, /deltaElement\.textContent = delta;\s*deltaElement\.hidden = !delta/);
    assert.match(css, /\.progress-placeholder\s*\{[^}]*width:\s*5px;[^}]*height:\s*5px;[^}]*border-radius:\s*50%;[^}]*background:\s*#555555/);
    assert.doesNotMatch(js, /'\*\*\*\*\*'/);
});

test('подписи рейтинга и пояснения порога согласованы с пятью играми', () => {
    const html = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/index.html'), 'utf8');
    const js = fs.readFileSync(path.resolve(__dirname, '../../prototypes/wordli/prototype.js'), 'utf8');
    assert.match(html, /data-action="open-leaderboard"[^>]*>\s*<span class="rules-link-title">Рейтинг<\/span>\s*<span class="rules-link-caption">топ участников<\/span>/);
    assert.match(html, /aria-label="Топ участников за всё время"/);
    assert.match(js, /Пока нет участников с пятью завершёнными играми\./);
    assert.match(js, /Для участия завершите 5 игр\./);
    assert.match(js, /Топ участников доступен в Telegram\./);
    assert.doesNotMatch(html + js, /Очки появляются после 10|завершите 10 игр|десятью завершёнными|турнирная таблица/i);
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
