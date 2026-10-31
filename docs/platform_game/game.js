class PlatformGame {
    constructor() {
        // Получаем элемент canvas из HTML и его контекст для рисования
        this.canvas = document.getElementById('gameCanvas');
        this.ctx = this.canvas.getContext('2d');

        // Устанавливаем размеры игрового поля
        this.canvas.width = 300;
        this.canvas.height = 300;

        // Настройки платформы
        this.paddleHeight = 10;
        this.paddleWidth = 120;
        this.paddleX = (this.canvas.width - this.paddleWidth) / 2;
        this.paddleSpeed = 7;

        // Настройки мяча
        this.ballRadius = 5;
        this.ballX = this.canvas.width / 2;
        this.ballY = this.canvas.height - 30;
        this.ballSpeedX = 1 * 1.3;
        this.ballSpeedY = -1 * 1.3;

        // Настройки блоков
        this.blockRows = 5;
        this.blockColumns = 8;
        this.blockWidth = 30;
        this.blockHeight = 15;
        this.blockPadding = 5;
        this.blockOffsetTop = 30;
        this.blockOffsetLeft = 20;
        this.blocks = [];

        // Состояние движения
        this.rightPressed = false;
        this.leftPressed = false;

        // Счет игры
        this.score = 0;

        // Состояние игры
        this.isStarted = false;
        this.isPaused = false;
        this.gameOver = false;
        this.animationFrameId = null;
        this.scoreSent = false;

        // Цветовая схема (как в змейке)
        this.colors = {
            background: 'rgb(14, 22, 33)',     // сине-серый глубокий темный
            paddle: '#A9A9A9',                 // Металлический серый
            ball: 'rgb(245, 245, 245)',        // Нежно-белый
            blocks: '#A9A9A9',                 // Металлический серый
            gameOverBg: 'rgba(14, 22, 33, 0.85)', // полупрозрачный фон
            gameOverText: '#F5F5F5'            // Нежно-белый
        };

        // Инициализация блоков
        this.initBlocks();

        // Кнопка управления игрой
        this.pauseButton = document.getElementById('pauseButton');
        this.pauseButton.textContent = 'Начать';
        this.pauseButton.addEventListener('click', () => this.toggleGameState());

        // Запускаем инициализацию игры
        this.init();
    }

    // Инициализация блоков
    initBlocks() {
        for(let c = 0; c < this.blockColumns; c++) {
            this.blocks[c] = [];
            for(let r = 0; r < this.blockRows; r++) {
                this.blocks[c][r] = {
                    x: 0,
                    y: 0,
                    status: 1 // 1 = блок существует, 0 = блок разрушен
                };
            }
        }
    }

    // Метод инициализации игры
    init() {
        this.bindControls();
        this.draw();
    }

    // Метод сброса игры
    reset() {
        this.stopLoop();
        this.paddleX = (this.canvas.width - this.paddleWidth) / 2;
        this.ballX = this.canvas.width / 2;
        this.ballY = this.canvas.height - 30;
        this.ballSpeedX = 1.3;
        this.ballSpeedY = -1.3;
        this.score = 0;
        this.scoreSent = false;
        this.rightPressed = false;
        this.leftPressed = false;
        this.gameOver = false;
        this.isPaused = false;
        this.initBlocks();
        document.getElementById('score').textContent = '0';
    }

    // Управление состоянием игры
    toggleGameState() {
        if (this.gameOver) {
            this.reset();
            this.pauseButton.textContent = 'Пауза';
            this.isStarted = true;
            this.gameLoop();
        } else if (!this.isStarted) {
            this.isStarted = true;
            this.pauseButton.textContent = 'Пауза';
            this.gameLoop();
        } else {
            this.isPaused = !this.isPaused;
            this.pauseButton.textContent = this.isPaused ? 'Продолжить' : 'Пауза';
            if (this.isPaused) {
                this.stopLoop();
            } else {
                this.gameLoop();
            }
        }
    }

    // Привязка элементов управления
    bindControls() {
        // Обработчики клавиатуры
        document.addEventListener('keydown', (e) => {
            if (this.isPaused || !this.isStarted) return;
            if (e.key === 'Right' || e.key === 'ArrowRight') {
                this.rightPressed = true;
            } else if (e.key === 'Left' || e.key === 'ArrowLeft') {
                this.leftPressed = true;
            }
        });

        document.addEventListener('keyup', (e) => {
            if (e.key === 'Right' || e.key === 'ArrowRight') {
                this.rightPressed = false;
            } else if (e.key === 'Left' || e.key === 'ArrowLeft') {
                this.leftPressed = false;
            }
        });

        // Обработчики экранных кнопок
        document.getElementById('leftButton').addEventListener('mousedown', () => {
            if (!this.isPaused && this.isStarted) this.leftPressed = true;
        });
        document.getElementById('leftButton').addEventListener('mouseup', () => {
            this.leftPressed = false;
        });
        document.getElementById('leftButton').addEventListener('touchstart', (e) => {
            e.preventDefault();
            if (!this.isPaused && this.isStarted) this.leftPressed = true;
        });
        document.getElementById('leftButton').addEventListener('touchend', () => {
            this.leftPressed = false;
        });

        document.getElementById('rightButton').addEventListener('mousedown', () => {
            if (!this.isPaused && this.isStarted) this.rightPressed = true;
        });
        document.getElementById('rightButton').addEventListener('mouseup', () => {
            this.rightPressed = false;
        });
        document.getElementById('rightButton').addEventListener('touchstart', (e) => {
            e.preventDefault();
            if (!this.isPaused && this.isStarted) this.rightPressed = true;
        });
        document.getElementById('rightButton').addEventListener('touchend', () => {
            this.rightPressed = false;
        });
    }

    // Обработка столкновений с блоками
    collisionDetection() {
        for(let c = 0; c < this.blockColumns; c++) {
            for(let r = 0; r < this.blockRows; r++) {
                let b = this.blocks[c][r];
                if(b.status === 1) {
                    if(this.ballX > b.x &&
                       this.ballX < b.x + this.blockWidth &&
                       this.ballY > b.y &&
                       this.ballY < b.y + this.blockHeight) {
                        this.ballSpeedY = -this.ballSpeedY;
                        b.status = 0;
                        this.score += 10;
                        document.getElementById('score').textContent = this.score;

                        // Проверка победы
                        if(this.score === this.blockRows * this.blockColumns * 10) {
                            this.finishGame();
                        }
                    }
                }
            }
        }
    }

    // Отправка счета в Telegram
    sendScore() {
        if (this.scoreSent) return;
        this.scoreSent = true;
        console.log('Sending score:', this.score);
        const gameData = {
            action: 'game_end',
            game: 'platform',
            score: this.score
        };
        window.Telegram.WebApp.sendData(JSON.stringify(gameData));
    }

    stopLoop() {
        if (this.animationFrameId !== null) cancelAnimationFrame(this.animationFrameId);
        this.animationFrameId = null;
    }

    finishGame() {
        if (this.gameOver) return;
        this.gameOver = true;
        this.stopLoop();
        this.pauseButton.textContent = 'Начать';
        this.sendScore();
    }

    // Планируем только один кадр и отменяем его при паузе.
    gameLoop() {
        if (this.gameOver || this.isPaused || !this.isStarted || this.animationFrameId !== null) return;

        this.animationFrameId = requestAnimationFrame(() => {
            this.animationFrameId = null;
            if (this.gameOver || this.isPaused || !this.isStarted) return;
            this.update();
            this.draw();
            this.gameLoop();
        });
    }

    // Обновление состояния игры
    update() {
        // Движение платформы
        if(this.rightPressed && this.paddleX < this.canvas.width - this.paddleWidth) {
            this.paddleX += this.paddleSpeed;
        }
        else if(this.leftPressed && this.paddleX > 0) {
            this.paddleX -= this.paddleSpeed;
        }

        // Движение мяча
        this.ballX += this.ballSpeedX;
        this.ballY += this.ballSpeedY;

        // Отскок от стен
        if(this.ballX + this.ballSpeedX > this.canvas.width - this.ballRadius ||
           this.ballX + this.ballSpeedX < this.ballRadius) {
            this.ballSpeedX = -this.ballSpeedX;
        }
        if(this.ballY + this.ballSpeedY < this.ballRadius) {
            this.ballSpeedY = -this.ballSpeedY;
        }

        // Отскок от платформы
        if(this.ballY + this.ballSpeedY > this.canvas.height - this.ballRadius - this.paddleHeight) {
            if(this.ballX > this.paddleX && this.ballX < this.paddleX + this.paddleWidth) {
                let hitPoint = (this.ballX - (this.paddleX + this.paddleWidth/2)) / (this.paddleWidth/2);
                let angle = hitPoint * (Math.PI / 3); // угол отклонения: -60° ... +60°
                let speed = Math.sqrt(this.ballSpeedX * this.ballSpeedX + this.ballSpeedY * this.ballSpeedY);
                this.ballSpeedX = speed * Math.sin(angle);
                this.ballSpeedY = -Math.abs(speed * Math.cos(angle));
            }
            else if(this.ballY > this.canvas.height) {
                // Мяч упал - игра окончена
                this.finishGame();
                return;
            }
        }

        // Проверка столкновений с блоками
        this.collisionDetection();
    }

    // Отрисовка игры
    draw() {
        // Очищаем поле
        this.ctx.fillStyle = this.colors.background;
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

        // Рисуем блоки
        for(let c = 0; c < this.blockColumns; c++) {
            for(let r = 0; r < this.blockRows; r++) {
                if(this.blocks[c][r].status === 1) {
                    let blockX = (c * (this.blockWidth + this.blockPadding)) + this.blockOffsetLeft;
                    let blockY = (r * (this.blockHeight + this.blockPadding)) + this.blockOffsetTop;
                    this.blocks[c][r].x = blockX;
                    this.blocks[c][r].y = blockY;
                    this.ctx.fillStyle = this.colors.blocks;
                    this.ctx.fillRect(blockX, blockY, this.blockWidth, this.blockHeight);
                }
            }
        }

        // Рисуем платформу
        this.ctx.fillStyle = this.colors.paddle;
        this.ctx.fillRect(this.paddleX, this.canvas.height - this.paddleHeight,
                         this.paddleWidth, this.paddleHeight);

        // Рисуем мяч
        this.ctx.beginPath();
        this.ctx.arc(this.ballX, this.ballY, this.ballRadius, 0, Math.PI * 2);
        this.ctx.fillStyle = this.colors.ball;
        this.ctx.fill();
        this.ctx.closePath();

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
