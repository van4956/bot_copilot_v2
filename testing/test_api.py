"""
Тестовый скрипт для проверки работоспособности API ключей
Погода: OpenWeatherMap
Валюта: API обменных курсов
"""
import os
import sys

# Добавляем путь к корневой директории проекта
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from config_data.config import load_config

# Загружаем конфигурацию
config = load_config()


def test_weather_api():
    """
    Тестирует API ключ для погоды
    Получает текущую погоду в Москве
    """
    print("\n" + "="*50)
    print("ТЕСТ API ПОГОДЫ (OpenWeatherMap)")
    print("="*50)

    api_key = config.tg_bot.api_weather
    city = "Moscow"

    # URL для получения текущей погоды
    url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=ru"

    try:
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()

            print(f"\nГород: {data['name']}")
            print(f"Температура: {data['main']['temp']}°C")
            print(f"Ощущается как: {data['main']['feels_like']}°C")
            # print(f"Влажность: {data['main']['humidity']}%")
            # print(f"Описание: {data['weather'][0]['description']}")
            # print(f"Ветер: {data['wind']['speed']} м/с")
            print(f"\n✅ API ключ работает!")

            return True

        elif response.status_code == 401:
            print("❌ ОШИБКА: Неверный API ключ!")
            print("   Проверьте значение API_WEATHER в файле .env")
            return False

        elif response.status_code == 404:
            print("❌ ОШИБКА: Город не найден!")
            return False

        else:
            print(f"❌ ОШИБКА: Код {response.status_code}")
            print(f"   Ответ: {response.text}")
            return False

    except requests.exceptions.Timeout:
        print("❌ ОШИБКА: Превышено время ожидания!")
        print("   Проверьте интернет-соединение")
        return False

    except requests.exceptions.ConnectionError:
        print("❌ ОШИБКА: Нет соединения с сервером!")
        print("   Проверьте интернет-соединение")
        return False

    except Exception as e:
        print(f"❌ НЕИЗВЕСТНАЯ ОШИБКА: {e}")
        return False


def test_currency_api():
    """
    Тестирует API ключ для валюты
    Получает курс USD к RUB (как в handlers/currency.py)
    """
    print("\n" + "="*50)
    print("ТЕСТ API ВАЛЮТЫ (OpenExchangeRates)")
    print("="*50)

    api_key = config.tg_bot.api_currency
    base_currency = "USD"
    target_currency = "RUB"

    # URL для получения курса валют (как в currency.py)
    url = f'https://openexchangerates.org/api/latest.json?app_id={api_key}'

    try:
        response = requests.get(url, timeout=20)

        if response.status_code == 200:
            data = response.json()

            # Проверяем наличие rates
            if 'rates' in data:
                # Получаем курс рубля к доллару (как в currency.py)
                base_rate = data['rates'].get(base_currency, 1.0)
                target_rate = data['rates'].get(target_currency)

                if target_rate:
                    # Рассчитываем кросс-курс (как в currency.py)
                    rate = target_rate / base_rate

                    print(f"\nКурс: 1 {base_currency} = {rate:.2f} {target_currency}")

                    # Получаем время обновления (как в currency.py)
                    if 'timestamp' in data:
                        from datetime import datetime, timezone
                        dt_object = datetime.fromtimestamp(data['timestamp'], tz=timezone.utc)
                        time_str = dt_object.strftime('%H:%M')
                        date_str = dt_object.strftime('%d-%m-%Y')
                        print(f"Время обновления: {time_str} (UTC) | Дата: {date_str}")

                    print(f"\n✅ API ключ работает!")
                    return True
                else:
                    print(f"❌ ОШИБКА: Валюта {target_currency} не найдена в rates")
                    return False

            elif 'error' in data:
                error_msg = data.get('message', 'Unknown error')
                print(f"❌ ОШИБКА API: {error_msg}")

                if 'invalid' in error_msg.lower():
                    print("   Неверный API ключ!")
                    print("   Проверьте значение API_CURRENCY в файле .env")

                return False

        elif response.status_code == 401:
            print("❌ ОШИБКА: Неверный API ключ!")
            print("   Проверьте значение API_CURRENCY в файле .env")
            return False

        elif response.status_code == 403:
            print("❌ ОШИБКА: Доступ запрещен!")
            print("   Проверьте API ключ в файле .env")
            return False

        else:
            print(f"❌ ОШИБКА: Код {response.status_code}")
            print(f"   Ответ: {response.text}")
            return False

    except requests.exceptions.Timeout:
        print("❌ ОШИБКА: Превышено время ожидания!")
        print("   Проверьте интернет-соединение")
        return False

    except requests.exceptions.ConnectionError:
        print("❌ ОШИБКА: Нет соединения с сервером!")
        print("   Проверьте интернет-соединение")
        return False

    except Exception as e:
        print(f"❌ НЕИЗВЕСТНАЯ ОШИБКА: {e}")
        return False


def main():
    """Запускает все тесты API"""
    print("\n" + "="*50)
    print("ПРОВЕРКА НАЛИЧИЯ API КЛЮЧЕЙ")
    print("="*50)

    # Проверяем наличие API ключей
    print(f"\n{'✅ Weather API:  Найден' if config.tg_bot.api_weather else '❌ Weather API:  Отсутствует'}")
    print(f"{'✅ Currency API: Найден' if config.tg_bot.api_currency else '❌ Currency API: Отсутствует'}")

    # Запускаем тесты
    weather_ok = test_weather_api()
    currency_ok = test_currency_api()

    # Итоговый отчет
    print("\n" + "="*50)
    print("ИТОГИ ТЕСТИРОВАНИЯ")
    print("="*50)
    print(f"\nAPI Погоды: {'✅ OK' if weather_ok else '❌ FAIL'}")
    print(f"API Валюты: {'✅ OK' if currency_ok else '❌ FAIL'}")

    if weather_ok and currency_ok:
        print("\n🎉 Все API ключи работают корректно!")
    else:
        print("\nЕсть проблемы с API ключами!")
        print("   Проверьте файл .env и исправьте ключи")

    print("\n" + "="*50 + "\n")


if __name__ == "__main__":
    main()
