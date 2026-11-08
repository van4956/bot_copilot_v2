"""Погода без блокирующих запросов."""

import datetime
import logging
import math
from html import escape

import aiohttp
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __

from common import keyboard
from common.http import ExternalServiceError, get_json
from filters.chat_type import ChatTypeFilter
from handlers.navigation import open_main, reset_dialog

logger = logging.getLogger(__name__)


weather_router = Router()
weather_router.message.filter(ChatTypeFilter(["private"]))


class City(StatesGroup):
    """Выбор способа и ручной ввод населённого пункта."""

    forecast_moment = State()
    forecast_period = State()
    city_name_moment = State()
    city_name_period = State()


dict_weather_descriptions = {
    "ясно": "☀️",
    "несколько облаков": "🌤",
    "небольшая облачность": "🌤",
    "переменная облачность": "⛅️",
    "облачно с прояснениями": "🌥",
    "пасмурно": "☁️",
    "слабый дождь": "🌦",
    "небольшой дождь": "🌦",
    "умеренный дождь": "🌧",
    "дождь": "🌧",
    "сильный дождь": "🌧",
    "очень сильный дождь": "🌧",
    "проливной дождь": "🌧",
    "моросящий дождь": "🌧",
    "морось": "🌦",
    "слабая морось": "🌦",
    "гроза": "🌩",
    "гроза с дождем": "⛈",
    "сильная гроза": "⛈",
    "гроза с крупным градом": "⛈",
    "слабый снег": "🌨",
    "снег": "❄️",
    "сильный снег": "🌨",
    "снегопад": "🌨",
    "снежные заряды": "🌨",
    "туман": "🌫",
    "дымка": "🌫",
    "мгла": "🌫",
    "пыль": "🌪",
    "песок": "🌪",
    "пепел": "🌋",
    "шквал": "🌬",
    "торнадо": "🌪",
}

dict_weather_icon = {
    # Группа 800: Ясно
    "01d": "☀️",  # ясно (день)
    "01n": "🌙",  # ясно (ночь)
    # Группа 80x: Облачность
    "02d": "🌤",  # малооблачно (день)
    "02n": "🌤",  # малооблачно (ночь)
    "03d": "⛅️",  # облачно (день)
    "03n": "⛅️",  # облачно (ночь)
    "04d": "☁️",  # пасмурно (день)
    "04n": "☁️",  # пасмурно (ночь)
    # Группа 3xx: Морось
    "09d": "🌧",  # морось/ливень (день)
    "09n": "🌧",  # морось/ливень (ночь)
    # Группа 5xx: Дождь
    "10d": "🌦",  # дождь (день)
    "10n": "🌦",  # дождь (ночь)
    # Группа 2xx: Гроза
    "11d": "⛈",  # гроза (день)
    "11n": "⛈",  # гроза (ночь)
    # Группа 6xx: Снег
    "13d": "🌨",  # снег (день)
    "13n": "🌨",  # снег (ночь)
    # Группа 7xx: Атмосферные явления
    "50d": "🌫",  # туман/мгла/пыль/песок/дым (день)
    "50n": "🌫",  # туман/мгла/пыль/песок/дым (ночь)
    # Детальные коды погодных условий
    # Группа 2xx: Гроза
    "200": "⛈",  # гроза со слабым дождем
    "201": "⛈",  # гроза с дождем
    "202": "⛈",  # гроза с сильным дождем
    "210": "🌩",  # легкая гроза
    "211": "🌩",  # гроза
    "212": "🌩",  # сильная гроза
    "221": "🌩",  # прерывистая гроза
    "230": "⛈",  # гроза с легкой моросью
    "231": "⛈",  # гроза с моросью
    "232": "⛈",  # гроза с сильной моросью
    # Группа 3xx: Морось
    "300": "🌦",  # легкая морось
    "301": "🌦",  # морось
    "302": "🌧",  # сильная морось
    "310": "🌦",  # легкий моросящий дождь
    "311": "🌧",  # моросящий дождь
    "312": "🌧",  # сильный моросящий дождь
    "313": "🌧",  # дождь и морось
    "314": "🌧",  # сильный дождь и морось
    "321": "🌧",  # ливневая морось
    # Группа 5xx: Дождь
    "500": "🌦",  # легкий дождь
    "501": "🌧",  # умеренный дождь
    "502": "🌧",  # сильный дождь
    "503": "🌧",  # очень сильный дождь
    "504": "🌧",  # экстремальный дождь
    "511": "🌨",  # ледяной дождь
    "520": "🌧",  # легкий ливень
    "521": "🌧",  # ливень
    "522": "🌧",  # сильный ливень
    "531": "🌧",  # прерывистый ливень
    # Группа 6xx: Снег
    "600": "🌨",  # легкий снег
    "601": "❄️",  # снег
    "602": "❄️",  # сильный снег
    "611": "🌨",  # мокрый снег
    "612": "🌨",  # легкий мокрый снег
    "613": "🌨",  # ливневый мокрый снег
    "615": "🌨",  # легкий дождь со снегом
    "616": "🌨",  # дождь со снегом
    "620": "🌨",  # легкий снегопад
    "621": "🌨",  # снегопад
    "622": "🌨",  # сильный снегопад
    # Группа 7xx: Атмосферные явления
    "701": "🌫",  # мгла
    "711": "🌫",  # дым
    "721": "🌫",  # дымка
    "731": "🌪",  # песчаные/пыльные вихри
    "741": "🌫",  # туман
    "751": "🌪",  # песок
    "761": "🌫",  # пыль
    "762": "🌋",  # вулканический пепел
    "771": "💨",  # шквалы
    "781": "🌪",  # торнадо
}

# Создаем константы для текстов, которые используются в декораторах
WATER = __("Погода 🌊")
CURRENT = __("Текущий ⏺")
FOR_3_DAYS = __("На 3 дня ⏩")
BACK_TO_MAIN = __("Назад на главную ↩️")
CITY_BY_NAME = __("По названию 🏙")


async def _ask_location_method(message: Message, state: FSMContext) -> None:
    """Запрашивает город или геолокацию для прогноза на три дня."""
    await state.set_state(City.forecast_period)
    await message.answer(
        _("Каким способом определить прогноз?"),
        reply_markup=keyboard.get_keyboard(
            _("По названию 🏙"),
            _("По локации 🗺"),
            _("Назад на главную ↩️"),
            request_location=1,
            sizes=(2, 1),
            placeholder="⬇️",
        ),
    )


@weather_router.message(F.text == WATER)
async def water_cmd(message: Message, state: FSMContext) -> None:
    """Открывает погоду с трёхдневным прогнозом по умолчанию."""
    await reset_dialog(state)
    await _ask_location_method(message, state)


def _finite_number(value: object) -> float:
    """Проверяет числовые значения внешнего сервиса."""
    if isinstance(value, bool):
        raise ValueError("Ожидалось число")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Ожидалось конечное число")
    return result


def _wind_direction(degrees: object) -> str:
    """Показывает направление движения ветра, а не его источник."""
    try:
        direction = (_finite_number(degrees) + 180) % 360
    except (TypeError, ValueError, OverflowError):
        return "—"
    # Стандартные эмодзи-стрелки поддерживаются шрифтами мобильного Telegram.
    arrows = ("⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️")
    return arrows[int((direction + 22.5) // 45) % 8]


def _weather_values(entry: dict) -> tuple[str, str, str, str, str]:
    """Подготавливает столбцы, сохраняя реальные значения погоды."""
    return (
        f"{_finite_number(entry['main']['temp']):.1f}",
        f"{_finite_number(entry['main']['humidity']):g}",
        f"{_finite_number(entry['wind']['speed']):.1f}",
        _wind_direction(entry["wind"].get("deg")),
        dict_weather_icon.get(entry["weather"][0]["icon"], "❓"),
    )


def _numeric_row(
    values: tuple[str, str, str, str, str],
    temperature_width: int | None = None,
    *,
    current: bool = False,
) -> str:
    """Выравнивает только температуру, оставляя остальное обычным текстом."""
    temperature, humidity, wind, direction, _ = values
    unit = "°C" if current else "°"
    width = len(temperature) if temperature_width is None else temperature_width
    return (
        f"<code>{temperature:>{width}}{unit}</code> | "
        f"{humidity}% | {wind}𝑣 | {direction}"
    )


def _weather_header(name: object, coordinates: dict) -> str:
    """Оставляет город обычным текстом, а координаты — моноширинными."""
    latitude, longitude = coordinates["lat"], coordinates["lon"]
    _finite_number(latitude)
    _finite_number(longitude)
    return (
        f"{escape(str(name))}\n"
        f"<code>{escape(str(latitude))}° {escape(str(longitude))}°</code>"
    )


async def parse_weather_data(
    type_forecast: int,
    city: str | None,
    lat: float | None,
    lon: float | None,
    locale: str | None = "ru",
    *,
    api_key: str = "",
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Получает погоду без блокировки обработки остальных событий."""
    if not api_key:
        return _("Сервис погоды не настроен")
    if type_forecast not in {1, 2}:
        return _("Неверные параметры запроса погоды")
    params = {"appid": api_key, "units": "metric", "lang": locale or "ru"}
    if city and city.strip():
        params["q"] = city.strip()[:100]
    elif (
        lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
    ):
        params.update(lat=lat, lon=lon)
    else:
        return _("Неверные параметры запроса погоды")
    endpoint = "weather" if type_forecast == 1 else "forecast"
    try:
        data = await get_json(
            f"https://api.openweathermap.org/data/2.5/{endpoint}",
            params=params,
            session=session,
            timeout=10,
        )
    except ExternalServiceError:
        return _("Сервис погоды временно недоступен")

    try:
        if type_forecast == 1:
            moment = datetime.datetime.fromtimestamp(
                data["dt"] + data["timezone"], tz=datetime.timezone.utc
            )
            values = _weather_values(data)
            header = _weather_header(data["name"], data["coord"])
            return (
                f"{header}\n\n"
                f"{_numeric_row(values, current=True)} {values[4]}\n\n"
                f"{moment:%d-%m-%Y  %H:%M}"
            )

        city_data = data["city"]
        header = _weather_header(city_data["name"], city_data["coord"])
        rows = []
        dates = []
        for entry in data["list"]:
            moment = datetime.datetime.fromtimestamp(
                entry["dt"] + city_data["timezone"], tz=datetime.timezone.utc
            )
            if moment.date() not in dates:
                if len(dates) == 3:
                    break
                dates.append(moment.date())
            rows.append((moment, _weather_values(entry)))
        if not rows:
            raise ValueError("Пустой прогноз")
        # Ширина температуры едина для всех трёх дней, без лишнего запаса.
        temperature_width = max(len(values[0]) for _, values in rows)
        result = [header]
        previous_date = None
        for moment, values in rows:
            if moment.date() != previous_date:
                result.append(f"\n{moment:%d.%m}")
                previous_date = moment.date()
            line = f"{moment:%H:%M} | {_numeric_row(values, temperature_width)}"
            result.append(f"{line} {values[4]}")
        return "\n".join(result)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError):
        logger.warning("Некорректный ответ сервиса погоды")
        return _("Сервис погоды вернул некорректный ответ")


async def _show_forecast(
    message: Message,
    state: FSMContext,
    location: dict,
) -> None:
    """Показывает три дня и возвращает главную даже при отказе погодного API."""
    data = await state.get_data()
    await reset_dialog(state)
    weather_info = await parse_weather_data(
        2,
        city=location.get("city"),
        lat=location.get("lat"),
        lon=location.get("lon"),
        locale=data.get("locale"),
        api_key=getattr(message.bot, "api_weather", ""),
        session=getattr(message.bot, "http_session", None),
    )
    await message.answer(weather_info, parse_mode=ParseMode.HTML)
    await open_main(message, state)


@weather_router.message(F.text == CURRENT)
async def water_moment_cmd(message: Message, state: FSMContext) -> None:
    """Убирает старую кнопку периода, не запрашивая новую погоду."""
    await open_main(message, state)


@weather_router.message(F.text == FOR_3_DAYS)
async def water_period_cmd(message: Message, state: FSMContext) -> None:
    """Возвращает главную вместо прежнего выбора периода."""
    await open_main(message, state)


async def _ask_city_name(message: Message, state: FSMContext, target: State) -> None:
    """Разрешает ручной ввод города только на соответствующем шаге."""
    await message.answer(
        _("Введите название населенного пункта"), reply_markup=keyboard.del_kb
    )
    await state.set_state(target)


@weather_router.message(City.forecast_moment, F.text == CITY_BY_NAME)
async def city_message_moment(message: Message, state: FSMContext) -> None:
    """Продолжает старый диалог, переводя его на прогноз трёх дней."""
    await _ask_city_name(message, state, City.city_name_period)


@weather_router.message(City.forecast_period, F.text == CITY_BY_NAME)
async def city_message_period(message: Message, state: FSMContext) -> None:
    """Запрашивает город для прогноза на три дня."""
    await _ask_city_name(message, state, City.city_name_period)


async def _process_city(message: Message, state: FSMContext) -> None:
    """Запрашивает прогноз по введённому названию."""
    city = (message.text or "").strip()
    if not city:
        await message.answer(
            _("Вы ввели не допустимые данные, введите название населенного пункта")
        )
        return
    await _show_forecast(message, state, {"city": city[:100]})


@weather_router.message(City.city_name_moment, F.text)
async def process_city_moment(message: Message, state: FSMContext) -> None:
    """Завершает старый ввод города прогнозом на три дня."""
    await _process_city(message, state)


@weather_router.message(City.city_name_moment)
async def process_city_moment_(message: Message) -> None:
    """Сообщает о неверном формате города."""
    await message.answer(
        _("Вы ввели не допустимые данные, введите название населенного пункта")
    )


@weather_router.message(City.city_name_period, F.text)
async def process_city_period(message: Message, state: FSMContext) -> None:
    """Показывает трёхдневный прогноз по названию."""
    await _process_city(message, state)


@weather_router.message(City.city_name_period)
async def process_city_period_(message: Message) -> None:
    """Сообщает о неверном формате города."""
    await message.answer(
        _("Вы ввели не допустимые данные, введите название населенного пункта")
    )


async def _process_location(message: Message, state: FSMContext) -> None:
    """Запрашивает прогноз по отправленной геолокации."""
    await message.answer(_("Локация получена"))
    await _show_forecast(
        message,
        state,
        {
            "lat": message.location.latitude,
            "lon": message.location.longitude,
        },
    )


@weather_router.message(F.location, City.forecast_moment)
async def loc_message_moment(message: Message, state: FSMContext) -> None:
    """Завершает старый выбор геолокации прогнозом на три дня."""
    await _process_location(message, state)


@weather_router.message(F.location, City.forecast_period)
async def loc_message_period(message: Message, state: FSMContext) -> None:
    """Показывает трёхдневный прогноз по геолокации."""
    await _process_location(message, state)
