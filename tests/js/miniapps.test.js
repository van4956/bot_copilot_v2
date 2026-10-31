// Локальные проверки DOM, таймеров и Telegram SDK без браузера и сети.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const docs = path.resolve(__dirname, '../../docs');

function sandbox(directory) {
    const elements = new Map();
    const documentEvents = {};
    const sdkEvents = {};
    const pending = new Map();
    const sent = [];
    let timerId = 0;
    const context2d = new Proxy({}, {
        get: (target, key) => target[key] ?? (() => {}),
        set: (target, key, value) => { target[key] = value; return true; }
    });
    const get = id => {
        if (!elements.has(id)) {
            elements.set(id, {
                id, value: '', textContent: '', events: {},
                classList: {add() {}, remove() {}},
                addEventListener(type, callback) { this.events[type] = callback; },
                getContext() { return context2d; }
            });
        }
        return elements.get(id);
    };
    const schedule = callback => {
        const id = ++timerId;
        pending.set(id, callback);
        return id;
    };
    const webapp = {
        ready() {}, expand() {},
        MainButton: {setText() {}, show() {}},
        onEvent(type, callback) { sdkEvents[type] = callback; },
        sendData(data) { sent.push(JSON.parse(data)); }
    };
    const document = {
        getElementById: get,
        addEventListener(type, callback) { documentEvents[type] = callback; },
        documentElement: {style: {setProperty() {}}},
        body: {innerHTML: ''}
    };
    const window = {Telegram: {WebApp: webapp}, navigator: {}};
    const context = vm.createContext({
        document, window, console: {log() {}, error() {}},
        performance: {now: () => 0},
        setTimeout: schedule, clearTimeout: id => pending.delete(id),
        requestAnimationFrame: schedule,
        cancelAnimationFrame: id => pending.delete(id)
    });
    const run = filename => vm.runInContext(
        fs.readFileSync(path.join(docs, directory, filename), 'utf8'),
        context, {filename: `${directory}/${filename}`, timeout: 500}
    );
    const tick = (time = 0) => {
        const callbacks = [...pending.entries()];
        for (const [id, callback] of callbacks) {
            if (!pending.delete(id)) continue;
            callback(time);
        }
    };
    return {get, documentEvents, sdkEvents, pending, sent, context, run, tick};
}

function startGame(directory) {
    const state = sandbox(directory);
    state.run('game.js');
    state.run('script.js');
    assert.equal(state.sent.length, 0, 'загрузка не должна вызывать sendData');
    state.documentEvents.DOMContentLoaded();
    state.game = state.context.window.game;
    return state;
}

test('все локальные JS разбираются, ссылки HTML ведут на существующие файлы', () => {
    for (const directory of fs.readdirSync(docs)) {
        for (const filename of fs.readdirSync(path.join(docs, directory))) {
            if (filename.endsWith('.js')) {
                new vm.Script(fs.readFileSync(path.join(docs, directory, filename), 'utf8'));
            }
        }
        const html = fs.readFileSync(path.join(docs, directory, 'index.html'), 'utf8');
        for (const match of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
            if (!match[1].startsWith('https://')) {
                assert.ok(fs.existsSync(path.join(docs, directory, match[1])));
            }
        }
    }
});

for (const directory of ['snake_game', 'platform_game']) {
    test(`${directory}: sendData только один раз при завершении`, () => {
        const state = startGame(directory);
        state.game.score = 100;
        state.sdkEvents.mainButtonClicked();
        state.sdkEvents.mainButtonClicked();
        state.game.sendScore();
        assert.deepEqual(state.sent, [{
            action: 'game_end',
            game: directory === 'snake_game' ? 'snake' : 'platform',
            score: 100
        }]);
        assert.equal(state.pending.size, 0);
    });

    test(`${directory}: пауза отменяет цикл, продолжение не размножает его`, () => {
        const state = startGame(directory);
        state.game.toggleGameState();
        assert.equal(state.pending.size, 1);
        state.game.toggleGameState();
        assert.equal(state.pending.size, 0);
        const before = directory === 'snake_game'
            ? state.game.snake[0].x : state.game.ballX;
        state.tick();
        assert.equal(directory === 'snake_game'
            ? state.game.snake[0].x : state.game.ballX, before);
        state.game.toggleGameState();
        state.game.gameLoop();
        assert.equal(state.pending.size, 1);
        state.tick();
        assert.equal(state.pending.size, 1);
        assert.equal(state.sent.length, 0);
    });
}

test('змейка: между тиками разрешён только один поворот', () => {
    const state = startGame('snake_game');
    state.game.toggleGameState();
    state.documentEvents.keydown({key: 'ArrowUp', preventDefault() {}});
    state.documentEvents.keydown({key: 'ArrowLeft', preventDefault() {}});
    state.game.update();
    assert.equal(state.game.direction, 'up');
    assert.equal(state.game.snake[0].x, 7);
    assert.equal(state.game.snake[0].y, 6);
    assert.equal(state.game.gameOver, false);
    state.get('leftButton').events.click();
    state.game.update();
    assert.equal(state.game.direction, 'left');
    assert.equal(state.game.gameOver, false);
});

test('змейка: прямой разворот запрещён', () => {
    const state = startGame('snake_game');
    state.game.toggleGameState();
    state.get('leftButton').events.click();
    state.game.update();
    assert.equal(state.game.direction, 'right');
    assert.equal(state.game.gameOver, false);
});

test('змейка: отсутствие свободной клетки не зацикливает генерацию еды', () => {
    const state = startGame('snake_game');
    vm.runInContext(`
        window.game.snake = [];
        for (let y = 0; y < 15; y++) {
            for (let x = 0; x < 15; x++) {
                if (x !== 14 || y !== 14) window.game.snake.push({x, y});
            }
        }
        window.game.foods = [{x: 14, y: 14}];
        window.game.createFood();
    `, state.context, {timeout: 100});
    assert.equal(state.game.foods.length, 1);
});

test('змейка: заполнение поля завершает игру ровно одним результатом', () => {
    const state = startGame('snake_game');
    vm.runInContext(`
        window.game.snake = [{x: 13, y: 14}];
        for (let y = 0; y < 15; y++) {
            for (let x = 0; x < 15; x++) {
                if (!(y === 14 && (x === 13 || x === 14))) {
                    window.game.snake.push({x, y});
                }
            }
        }
        window.game.foods = [{x: 14, y: 14}];
        window.game.score = 2210;
        window.game.update();
    `, state.context, {timeout: 100});
    assert.equal(state.game.snake.length, 225);
    assert.equal(state.game.score, 2220);
    assert.equal(state.game.gameOver, true);
    assert.equal(state.sent.length, 1);
    assert.equal(state.sent[0].score, 2220);
});

test('змейка: можно зайти на клетку хвоста, который уходит на этом тике', () => {
    const state = startGame('snake_game');
    state.game.snake = [{x: 1, y: 1}, {x: 1, y: 2}, {x: 0, y: 2}, {x: 0, y: 1}];
    state.game.direction = 'left';
    state.game.foods = [{x: 10, y: 10}];
    state.game.update();
    assert.equal(state.game.gameOver, false);
    assert.equal(state.game.snake.length, 4);
});

test('змейка: столкновение с телом завершает игру', () => {
    const state = startGame('snake_game');
    state.game.direction = 'left';
    state.game.update();
    assert.equal(state.game.gameOver, true);
    assert.equal(state.sent.length, 1);
});

for (const [min, max] of [[0, 0], [0, 10], [-10, 0], [10, 0]]) {
    test(`генератор: диапазон [${min}, ${max}] сохраняет ноль`, () => {
        const state = sandbox('random_generator');
        state.get('min').value = '1000';
        state.get('max').value = '9999';
        state.run('script.js');
        state.pending.clear();
        state.get('min').value = String(min);
        state.get('max').value = String(max);
        vm.runInContext('Math.random = () => 0.5; generateNumber()', state.context);
        state.tick(2500);
        const lower = Math.min(min, max);
        const upper = Math.max(min, max);
        assert.equal(Number(state.get('min').value), lower);
        assert.equal(Number(state.get('max').value), upper);
        const result = Number(state.get('result').textContent);
        assert.ok(result >= lower && result <= upper);
        assert.equal(state.sent.length, 0);
    });
}
