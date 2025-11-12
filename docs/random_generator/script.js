// Инициализация Telegram WebApp
const webapp = window.Telegram.WebApp;
webapp.ready();

// Получаем элементы со страницы по их ID
const minInput = document.getElementById('min');
const maxInput = document.getElementById('max');
const resultDisplay = document.getElementById('result');
const generateBtn = document.getElementById('generateBtn');

// Функция анимации чисел - создает эффект "прокрутки" чисел
function animateNumber(from, to, duration = 2000) {
    const start = performance.now();
    const range = to - from;
    let lastNumber = from;

    function update(currentTime) { // Функция обновления анимации
        const elapsed = currentTime - start;
        const progress = Math.min(elapsed / duration, 1);

        if (progress < 1) {
            if (Math.random() < 0.3) {
                const randomValue = Math.floor(Math.random() * range + from);
                if (randomValue !== lastNumber) {
                    lastNumber = randomValue;
                    resultDisplay.textContent = randomValue;
                }
            }
            requestAnimationFrame(update); // Запрашиваем следующий кадр анимации
        } else { // Если анимация завершена
            resultDisplay.textContent = to;
            resultDisplay.classList.add('animate');
            if (window.navigator.vibrate) {
                window.navigator.vibrate(100);
            }
            setTimeout(() => resultDisplay.classList.remove('animate'), 300); // Убираем класс анимации через 300мс
        }
    }

    requestAnimationFrame(update); // Запускаем анимацию
}

// Функция генерации случайного числа
function generateNumber() {
    const min = parseInt(minInput.value) || 1; // Получаем минимальное значение (или 1, если ввод некорректный)
    const max = parseInt(maxInput.value) || 100; // Получаем максимальное значение (или 100, если ввод некорректный)

    const validMin = Math.min(min, max);
    const validMax = Math.max(min, max);

    minInput.value = validMin;
    maxInput.value = validMax;

    const result = Math.floor(Math.random() * (validMax - validMin + 1)) + validMin; // Генерируем случайное число
    animateNumber(validMin, result);
}

// Обработчики событий при вводе значений
minInput.addEventListener('input', () => {
    const min = parseInt(minInput.value) || 1;
    const max = parseInt(maxInput.value) || 100;
    if (min > max) {
        maxInput.value = min;
    }
});

maxInput.addEventListener('input', () => { // При изменении максимального значения
    const min = parseInt(minInput.value) || 1;
    const max = parseInt(maxInput.value) || 100;
    if (max < min) {
        minInput.value = max;
    }
});

generateBtn.addEventListener('click', generateNumber);

// Инициализация - генерируем первое число при загрузке
generateNumber();