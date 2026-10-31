class SnakeGame {
    constructor() {
        // Получаем элемент canvas из HTML и его контекст для рисования
        this.canvas = document.getElementById('gameCanvas');
        this.ctx = this.canvas.getContext('2d');

        // Устанавливаем размеры игрового поля
        this.canvas.width = 300;  // ширина в пикселях
        this.canvas.height = 300; // высота в пикселях

        // Настройки сетки игрового поля
        this.gridSize = 15;  // поле 15x15 клеток
        this.tileSize = this.canvas.width / this.gridSize;  // размер одной клетки

        // Начальное состояние змейки - массив координат сегментов
        this.snake = [
            {x: 7, y: 7}, // Голова змейки
            {x: 6, y: 7}, // Тело
            {x: 5, y: 7}  // Хвост
        ];

        // Направление движения змейки (по умолчанию вправо)
        this.direction = 'right';

        // Скорость обновления игры в миллисекундах
        this.speed = 200; // чем меньше число, тем быстрее движение

        // Текущий счет игрока
        this.score = 0;

        // Массив для хранения фруктов
        this.foods = [];

        // Начальное состояние
        this.isStarted = false;
        this.isPaused = false;
        this.gameOver = false;
        this.timerId = null;
        this.pendingDirection = null;
        this.scoreSent = false;

        // Цветовая схема
        this.colors = {
            background: 'rgb(14, 22, 33)',     // сине-серый глубокий темный - поле игры
            snake: '#A9A9A9',          // Металлический серый для змейки
            snakeHead: 'rgb(245, 245, 245)',      // Чуть светлее серый для головы
            food: 'rgb(245, 245, 245)',           // Нежно-белый для еды
            gameOverBg: 'rgba(14, 22, 33, 0.85)', // полупрозрачный фон с тем же цветом для окончания игры
            gameOverText: '#F5F5F5'    // Нежно-белый для текста
        };

        // Кнопка управления игрой
        this.pauseButton = document.getElementById('pauseButton');
        this.pauseButton.textContent = 'Начать';
        this.pauseButton.addEventListener('click', () => this.toggleGameState());

        // Запускаем инициализацию игры
        this.init();
    }

    // Метод инициализации игры
    init() {
        this.bindControls();
        // Создаем два фрукта при инициализации
        this.createFood();
        this.createFood();
        this.draw();
    }

    // Метод сброса игры
    reset() {
        this.stopLoop();
        this.snake = [
            {x: 7, y: 7},
            {x: 6, y: 7},
            {x: 5, y: 7}
        ];
        this.direction = 'right';
        this.pendingDirection = null;
        this.score = 0;
        this.scoreSent = false;
        this.gameOver = false;
        this.isPaused = false;
        document.getElementById('score').textContent = '0';
        // Очищаем массив фруктов и создаем два новых
        this.foods = [];
        this.createFood();
        this.createFood();
    }

    // Метод для управления состоянием игры
    toggleGameState() {
        if (this.gameOver) {
            // Перезапуск игры
            this.reset();
            this.pauseButton.textContent = 'Пауза';
            this.isStarted = true;
            this.gameLoop();
        } else if (!this.isStarted) {
            // Первый запуск
            this.isStarted = true;
            this.pauseButton.textContent = 'Пауза';
            this.gameLoop();
        } else {
            // Переключение паузы
            this.isPaused = !this.isPaused;
            this.pauseButton.textContent = this.isPaused ? 'Продолжить' : 'Пауза';
            if (this.isPaused) {
                this.stopLoop();
            } else {
                this.gameLoop();
            }
        }
    }

    // Метод привязки элементов управления
    bindControls() {
        // Обработчик нажатий клавиш на клавиатуре
        document.addEventListener('keydown', (e) => {
            if (this.isPaused || !this.isStarted) return; // Игнорируем управление на паузе или до старта

            switch(e.key) {
                case 'ArrowUp':
                case 'ArrowDown':
                case 'ArrowLeft':
                case 'ArrowRight':
                    e.preventDefault(); // Предотвращаем прокрутку
                    break;
            }

            const directions = {
                ArrowUp: 'up', ArrowDown: 'down',
                ArrowLeft: 'left', ArrowRight: 'right'
            };
            if (directions[e.key]) this.changeDirection(directions[e.key]);
        });

        // Обработчики нажатий экранных кнопок
        document.getElementById('upButton').addEventListener('click', () => {
            this.changeDirection('up');
        });
        document.getElementById('downButton').addEventListener('click', () => {
            this.changeDirection('down');
        });
        document.getElementById('leftButton').addEventListener('click', () => {
            this.changeDirection('left');
        });
        document.getElementById('rightButton').addEventListener('click', () => {
            this.changeDirection('right');
        });
    }

    // Принимаем не больше одного поворота между шагами игры.
    changeDirection(direction) {
        if (this.isPaused || !this.isStarted || this.gameOver || this.pendingDirection !== null) return;
        const opposite = {up: 'down', down: 'up', left: 'right', right: 'left'};
        if (direction !== this.direction && direction !== opposite[this.direction]) {
            this.pendingDirection = direction;
        }
    }

    // Выбираем из конечного списка клеток, даже когда поле почти заполнено.
    createFood() {
        if (this.foods.length >= 2) return;
        const freeCells = [];
        for (let y = 0; y < this.gridSize; y++) {
            for (let x = 0; x < this.gridSize; x++) {
                const occupied = this.snake.some(cell => cell.x === x && cell.y === y)
                    || this.foods.some(cell => cell.x === x && cell.y === y);
                if (!occupied) freeCells.push({x, y});
            }
        }
        if (freeCells.length > 0) {
            this.foods.push(freeCells[Math.floor(Math.random() * freeCells.length)]);
        }
    }

    stopLoop() {
        if (this.timerId !== null) clearTimeout(this.timerId);
        this.timerId = null;
    }

    finishGame() {
        if (this.gameOver) return;
        this.gameOver = true;
        this.stopLoop();
        this.pauseButton.textContent = 'Начать';
        this.sendScore();
    }

    // Этот метод отправляет счет игры в телеграм
    sendScore() {
        if (this.scoreSent) return;
        this.scoreSent = true;
        console.log('Sending score:', this.score);
        const gameData = {
            action: 'game_end',
            game: 'snake',
            score: this.score
        };
        window.Telegram.WebApp.sendData(JSON.stringify(gameData));
    }

    // Основной игровой цикл
    gameLoop() {
        if (this.gameOver || this.isPaused || !this.isStarted || this.timerId !== null) return;

        this.timerId = setTimeout(() => {
            this.timerId = null;
            if (this.gameOver || this.isPaused || !this.isStarted) return;
            this.update();
            this.draw();
            this.gameLoop();
        }, this.speed);
    }

    // Метод обновления состояния игры
    update() {
        if (this.pendingDirection !== null) {
            this.direction = this.pendingDirection;
            this.pendingDirection = null;
        }
        const head = {x: this.snake[0].x, y: this.snake[0].y};

        // Обновляем позицию головы
        switch(this.direction) {
            case 'up': head.y--; break;
            case 'down': head.y++; break;
            case 'left': head.x--; break;
            case 'right': head.x++; break;
        }

        // Обработка перехода через границы
        head.x = (head.x + this.gridSize) % this.gridSize;
        head.y = (head.y + this.gridSize) % this.gridSize;

        const eatenFoodIndex = this.foods.findIndex(food =>
            food.x === head.x && food.y === head.y
        );
        // Хвост освободит клетку на этом шаге, если змейка не растёт.
        const body = eatenFoodIndex === -1 ? this.snake.slice(0, -1) : this.snake;
        if (body.some(segment => segment.x === head.x && segment.y === head.y)) {
            this.finishGame();
            return;
        }

        // Добавляем новую голову в начало массива
        this.snake.unshift(head);

        if (eatenFoodIndex !== -1) {
            // Удаляем съеденный фрукт
            this.foods.splice(eatenFoodIndex, 1);
            this.score += 10;
            document.getElementById('score').textContent = this.score;
            if (this.snake.length === this.gridSize * this.gridSize) {
                this.finishGame();
            } else {
                this.createFood();
            }
        } else {
            this.snake.pop();
        }
    }

    // Метод отрисовки игры
    draw() {
        // Очищаем все игровое поле
        this.ctx.fillStyle = this.colors.background;
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

        // Рисуем змейку
        this.snake.forEach((segment, index) => {
            this.ctx.fillStyle = index === 0 ? this.colors.snakeHead : this.colors.snake;
            this.ctx.fillRect(
                segment.x * this.tileSize,
                segment.y * this.tileSize,
                this.tileSize - 1,
                this.tileSize - 1
            );
        });

        // Рисуем фрукты
        this.foods.forEach(food => {
            this.ctx.fillStyle = this.colors.food;
            this.ctx.beginPath();
            this.ctx.arc(
                food.x * this.tileSize + this.tileSize/2,
                food.y * this.tileSize + this.tileSize/2,
                this.tileSize/2 - 1,
                0,
                Math.PI * 2
            );
            this.ctx.fill();
        });

        // Если игра окончена, показываем сообщение
        if (this.gameOver) {
            this.ctx.fillStyle = this.colors.gameOverBg;
            this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

            this.ctx.fillStyle = this.colors.gameOverText;
            this.ctx.font = '20px Arial';
            this.ctx.textAlign = 'center';
            this.ctx.fillText(
                'Игра окончена',
                this.canvas.width/2,
                this.canvas.height/2
            );
        }
    }
}
