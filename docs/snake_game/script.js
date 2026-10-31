// Проверяем, запущено ли приложение в Telegram
if (!window.Telegram || !window.Telegram.WebApp) {
    console.error('Это приложение должно быть запущено в Telegram');
    document.body.innerHTML = '<h1>Пожалуйста, откройте это приложение через Telegram</h1>';
    throw new Error('WebApp API не доступен');
}

// Инициализация Telegram WebApp
const webapp = window.Telegram.WebApp;
webapp.ready();
webapp.expand();

// В начале файла после webapp.ready()
webapp.MainButton.setText('Завершить игру');
webapp.MainButton.show();

// Настраиваем обработчики событий
webapp.onEvent('mainButtonClicked', function() {
    if (window.game) window.game.finishGame();
});

// Подстраиваем тему под настройки пользователя
document.documentElement.style.setProperty('--tg-theme-bg-color', webapp.backgroundColor);
document.documentElement.style.setProperty('--tg-theme-text-color', webapp.textColor);
document.documentElement.style.setProperty('--tg-theme-button-color', webapp.buttonColor);
document.documentElement.style.setProperty('--tg-theme-button-text-color', webapp.buttonTextColor);

// Инициализируем игру после загрузки DOM
document.addEventListener('DOMContentLoaded', () => {
    const game = new SnakeGame();
    window.game = game; // Делаем игру доступной глобально
});
