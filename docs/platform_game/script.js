// Проверяем, запущено ли приложение в Telegram
if (!window.Telegram || !window.Telegram.WebApp) {
    console.error('Это приложение должно быть запущено в Telegram');
    document.body.innerHTML = '<h1>Пожалуйста, откройте это приложение через Telegram</h1>';
    throw new Error('WebApp API не доступен');
}

// Инициализация Telegram WebApp
const webapp = window.Telegram.WebApp;
webapp.ready(); // Сообщаем Telegram, что приложение готово
webapp.expand(); // Раскрываем на весь экран

// Настраиваем главную кнопку Telegram
webapp.MainButton.setText('Завершить игру');
webapp.MainButton.show();

// Обработчик нажатия на главную кнопку
webapp.onEvent('mainButtonClicked', function() {
    if (window.game) window.game.finishGame();
});

// Подстраиваем тему под настройки пользователя Telegram
document.documentElement.style.setProperty('--tg-theme-bg-color', webapp.backgroundColor);
document.documentElement.style.setProperty('--tg-theme-text-color', webapp.textColor);
document.documentElement.style.setProperty('--tg-theme-button-color', webapp.buttonColor);
document.documentElement.style.setProperty('--tg-theme-button-text-color', webapp.buttonTextColor);

// Инициализируем игру после загрузки DOM
document.addEventListener('DOMContentLoaded', () => {
    // Создаем экземпляр игры и сохраняем его глобально
    const game = new PlatformGame();
    window.game = game; // Делаем игру доступной глобально для доступа к счету
});

// Обработка ошибок
window.onerror = function(msg, url, lineNo, columnNo, error) {
    console.error('Ошибка: ', msg, 'Строка: ', lineNo, 'Столбец: ', columnNo);
    return false;
};
