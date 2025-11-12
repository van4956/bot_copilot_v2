// Инициализация Telegram WebApp
const webapp = window.Telegram.WebApp;
webapp.ready();

// Подстраиваем тему под настройки пользователя
document.documentElement.style.setProperty('--tg-theme-bg-color', webapp.backgroundColor);
document.documentElement.style.setProperty('--tg-theme-text-color', webapp.textColor);
document.documentElement.style.setProperty('--tg-theme-button-color', webapp.buttonColor);
document.documentElement.style.setProperty('--tg-theme-button-text-color', webapp.buttonTextColor);

// Получаем элементы со страницы
const diameterInput = document.getElementById('diameter');
const quantityInput = document.getElementById('quantity');
const priceInput = document.getElementById('price');
const pricePerCmSpan = document.getElementById('price-per-cm');
const pricePerPizzaSpan = document.getElementById('price-per-pizza');

// Функция расчета стоимости
function calculatePricePerCm() {
    const diameter = parseFloat(diameterInput.value);
    const quantity = parseFloat(quantityInput.value) || 1;
    const totalPrice = parseFloat(priceInput.value);

    if (diameter && totalPrice) {
        const area = Math.PI * Math.pow(diameter / 2, 2);
        const pricePerPizza = totalPrice / quantity;
        const pricePerCm = pricePerPizza / area;

        // Выводим результаты с округлением до двух знаков после запятой
        pricePerCmSpan.textContent = pricePerCm.toFixed(2);
        pricePerPizzaSpan.textContent = Math.round(pricePerPizza); // Округляем до целого числа
    } else {
        // Если не хватает данных, выводим нули
        pricePerCmSpan.textContent = '0.00';
        pricePerPizzaSpan.textContent = '0';
    }
}

// Добавляем обработчики событий для автоматического пересчета
diameterInput.addEventListener('input', calculatePricePerCm);
quantityInput.addEventListener('input', calculatePricePerCm);
priceInput.addEventListener('input', calculatePricePerCm);