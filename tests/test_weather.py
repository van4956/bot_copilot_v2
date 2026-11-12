"""Погода: один выбор города, устойчивые столбцы и направление ветра."""

import datetime
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update
from aiogram.utils.i18n import I18n

from common import keyboard
from common.http import ExternalServiceError
from handlers import private, weather


@pytest.fixture(autouse=True)
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оставляет сообщения проверок в исходной локали."""
    for module in (weather, private, keyboard):
        monkeypatch.setattr(module, "_", lambda text: text)


@pytest.fixture
def state() -> FSMContext:
    """Создаёт отдельное состояние без Redis и файлов."""
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=2, user_id=2))


@pytest.fixture
def message() -> SimpleNamespace:
    """Подменяет отправку Telegram и настройки погодного API."""
    return SimpleNamespace(
        answer=AsyncMock(),
        bot=SimpleNamespace(
            api_weather="offline-key",
            http_session=object(),
            test_mode=True,
            wordli_webapp_url="https://game.invalid",
        ),
        text="Тбилиси",
        location=SimpleNamespace(latitude=0, longitude=0),
    )


def _entry(moment: datetime.datetime, **changes: object) -> dict:
    """Создаёт фрагмент ответа OpenWeather без обязательного давления."""
    entry = {
        "dt": int(moment.timestamp()),
        "main": {"temp": 9.2, "humidity": 7},
        "wind": {"speed": 2.0, "deg": 0},
        "weather": [{"icon": "01d"}],
    }
    entry.update(changes)
    return entry


def _forecast(entries: list[dict], offset: int = 0) -> dict:
    """Создаёт прогноз для проверок экранирования и часового пояса."""
    return {
        "city": {
            "name": "<Город & море>",
            "coord": {"lat": 0, "lon": 0},
            "timezone": offset,
        },
        "list": entries,
    }


def _moment(day: int = 8, hour: int = 0) -> datetime.datetime:
    """Возвращает фиксированный момент UTC."""
    return datetime.datetime(2026, 11, day, hour, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    ("degrees", "arrow"),
    [
        (0, "⬇️"),
        (45, "↙️"),
        (90, "⬅️"),
        (135, "↖️"),
        (180, "⬆️"),
        (225, "↗️"),
        (270, "➡️"),
        (315, "↘️"),
        (360, "⬇️"),
        (-90, "➡️"),
        (22.49, "⬇️"),
        (22.5, "↙️"),
    ],
)
def test_wind_arrow_points_to_destination(degrees: float, arrow: str) -> None:
    """Метеорологическое направление источника разворачивается на 180°."""
    assert weather._wind_direction(degrees) == arrow


@pytest.mark.parametrize("degrees", range(0, 360, 45))
def test_direction_uses_supported_standard_emoji(degrees: int) -> None:
    """Все восемь направлений используют стандартную эмодзи-стрелку с VS16."""
    arrow = weather._wind_direction(degrees)
    assert len(arrow) == 2
    assert arrow[0] in {"⬆", "↗", "➡", "↘", "⬇", "↙", "⬅", "↖"}
    assert arrow[-1] == "\ufe0f" and "\ufe0e" not in arrow


@pytest.mark.parametrize("degrees", [None, "unknown", float("nan"), float("inf"), True])
def test_missing_or_invalid_direction_is_not_invented(degrees: object) -> None:
    """Без корректного угла выводится нейтральный прочерк."""
    assert weather._wind_direction(degrees) == "—"


@pytest.mark.parametrize(
    ("temperature", "expected"),
    [
        (9.4, "9"),
        (9.45, "9"),
        (9.49, "9"),
        (9.5, "10"),
        (9.9, "10"),
        (2.5, "3"),
        (-9.4, "-9"),
        (-9.45, "-9"),
        (-9.49, "-9"),
        (-9.5, "-10"),
        (-2.5, "-3"),
        (0.0, "0"),
        (-0.0, "0"),
        (0.4, "0"),
        (-0.4, "0"),
        (0.49999999999999994, "0"),
        (-0.49999999999999994, "0"),
        (0.5, "1"),
        (-0.5, "-1"),
        ("9.5", "10"),
    ],
)
def test_temperature_rounds_once_with_half_away_from_zero(
    temperature: object, expected: str
) -> None:
    """Половины округляются от нуля без банковского и двойного округления."""
    assert weather._rounded_temperature(temperature) == expected


async def test_weather_entry_skips_period_and_clears_previous_city(
    state: FSMContext, message: SimpleNamespace
) -> None:
    """Новый вход спрашивает способ, а не период или прежний город."""
    await state.set_data({"locale": "en", "weather_location": {"city": "Old"}})
    await weather.water_cmd(message, state)
    assert await state.get_state() == weather.City.forecast_period.state
    assert await state.get_data() == {"locale": "en"}
    message.answer.assert_awaited_once()
    call = message.answer.await_args
    assert call.args[0] == "Каким способом определить прогноз?"
    buttons = call.kwargs["reply_markup"].keyboard
    assert [button.text for row in buttons for button in row] == [
        "По названию 🏙",
        "По локации 🗺",
        "Назад на главную ↩️",
    ]
    assert buttons[0][1].request_location is True


def _assert_main_panel(call: object, translate=lambda text: text) -> None:
    """Проверяет главную панель с четырьмя обычными кнопками 2×2."""
    assert call.args[0] == translate("Главная панель")
    markup = call.kwargs["reply_markup"]
    assert [len(row) for row in markup.keyboard] == [2, 2]
    assert [button.text for row in markup.keyboard for button in row] == [
        translate("Погода 🌊"),
        translate("Валюта 💵"),
        translate("Котики 🐱"),
        translate("Игры 🕹"),
    ]


async def test_name_forecast_returns_main_and_forgets_city(
    monkeypatch: pytest.MonkeyPatch, state: FSMContext, message: SimpleNamespace
) -> None:
    """После прогноза остаются главная панель и язык, но не выбранный город."""
    parse = AsyncMock(return_value="Готовая погода")
    monkeypatch.setattr(weather, "parse_weather_data", parse)
    await state.set_data({"locale": "en", "weather_location": {"city": "Old"}})
    await weather.water_cmd(message, state)
    await weather.city_message_period(message, state)
    assert await state.get_state() == weather.City.city_name_period.state
    message.text = "  Тбилиси  "
    message.answer.reset_mock()
    await weather.process_city_period(message, state)
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": "en"}
    parse.assert_awaited_once()
    assert parse.await_args.args == (2,)
    assert parse.await_args.kwargs["city"] == "Тбилиси"
    assert parse.await_args.kwargs["locale"] == "en"
    assert [call.args[0] for call in message.answer.await_args_list] == [
        "Готовая погода",
        "Главная панель",
    ]
    _assert_main_panel(message.answer.await_args)


async def test_location_forecast_returns_main_with_zero_coordinates(
    monkeypatch: pytest.MonkeyPatch, state: FSMContext, message: SimpleNamespace
) -> None:
    """Экватор и нулевой меридиан принимаются, а после ответа геоданные удаляются."""
    parse = AsyncMock(return_value="Погода")
    monkeypatch.setattr(weather, "parse_weather_data", parse)
    await state.set_data({"locale": "en", "city_name_period": "Old"})
    await weather.loc_message_period(message, state)
    parse.assert_awaited_once()
    assert parse.await_args.args == (2,)
    assert parse.await_args.kwargs["lat"] == parse.await_args.kwargs["lon"] == 0
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": "en"}
    _assert_main_panel(message.answer.await_args)


@pytest.mark.parametrize("handler", ["water_moment_cmd", "water_period_cmd"])
@pytest.mark.parametrize("initial_state", [None, weather.City.city_name_period])
async def test_old_period_buttons_only_return_main_without_http(
    monkeypatch: pytest.MonkeyPatch,
    state: FSMContext,
    message: SimpleNamespace,
    handler: str,
    initial_state: object,
) -> None:
    """Устаревшие кнопки не запрашивают период, способ или новый прогноз."""
    parse = AsyncMock()
    monkeypatch.setattr(weather, "parse_weather_data", parse)
    await state.set_state(initial_state)
    await state.set_data({"locale": "en", "weather_location": {"city": "Old"}})
    await getattr(weather, handler)(message, state)
    parse.assert_not_awaited()
    message.answer.assert_awaited_once()
    _assert_main_panel(message.answer.await_args)
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": "en"}


@pytest.mark.parametrize("handler", ["process_city_moment", "loc_message_moment"])
async def test_legacy_moment_states_always_use_three_day_forecast(
    monkeypatch: pytest.MonkeyPatch,
    state: FSMContext,
    message: SimpleNamespace,
    handler: str,
) -> None:
    """Старое состояние текущей погоды не создаёт скрытого выбора периода."""
    parse = AsyncMock(return_value="Погода")
    monkeypatch.setattr(weather, "parse_weather_data", parse)
    await getattr(weather, handler)(message, state)
    parse.assert_awaited_once()
    assert parse.await_args.args == (2,)
    _assert_main_panel(message.answer.await_args)


async def test_blank_city_stays_in_input_state(
    monkeypatch: pytest.MonkeyPatch, state: FSMContext, message: SimpleNamespace
) -> None:
    """Пустая строка не сохраняется как город и не запускает HTTP."""
    parse = AsyncMock()
    monkeypatch.setattr(weather, "parse_weather_data", parse)
    await state.set_state(weather.City.city_name_period)
    message.text = "   "
    await weather.process_city_period(message, state)
    assert await state.get_state() == weather.City.city_name_period.state
    parse.assert_not_awaited()
    assert "не допустимые данные" in message.answer.await_args.args[0]


async def test_current_monospace_only_temperature_and_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Город, время, влажность, ветер и стрелка остаются обычным текстом."""
    data = _entry(_moment())
    data.update(name="<Город & море>", coord={"lat": 0, "lon": 0}, timezone=0)
    data["main"].update(pressure=123456, humidity=7)
    data["wind"].update(speed=12.2, deg=45)
    monkeypatch.setattr(weather, "get_json", AsyncMock(return_value=data))
    result = await weather.parse_weather_data(1, "Город", None, None, api_key="key")
    assert result == (
        "&lt;Город &amp; море&gt;\n<code>0° 0°</code>\n\n"
        "<code>9°C</code> | 7% | 12.2𝑣 | ↙️ ☀️\n\n"
        "08-11-2026  00:00"
    )
    assert re.findall(r"<code>(.*?)</code>", result) == ["0° 0°", "9°C"]
    assert "123456" not in result


async def test_forecast_aligns_only_temperature_across_all_three_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Общая ширина температуры учитывает минус и экстремум на другом дне."""
    cases = [
        (8, 0, 0.0, 7, 12.2),
        (8, 3, -9.0, 100, 0.0),
        (9, 0, 9.0, 99, 9.9),
        (9, 3, -99.9, 100, 15.5),
        (10, 0, 100.0, 1, 105.0),
        (10, 3, -100.0, 50, 1.0),
    ]
    entries = []
    for day, hour, temperature, humidity, wind in cases:
        entry = _entry(_moment(day, hour))
        entry["main"].update(temp=temperature, humidity=humidity, pressure=123456)
        entry["wind"]["speed"] = wind
        entries.append(entry)
    monkeypatch.setattr(weather, "get_json", AsyncMock(return_value=_forecast(entries)))
    result = await weather.parse_weather_data(2, "Город", None, None, api_key="key")
    lines = re.findall(r"^\d\d:\d\d .*?$", result, re.MULTILINE)
    temperature_cells = [
        f"{temperature:>4}°" for temperature in ("0", "-9", "9", "-100", "100", "-100")
    ]
    assert re.findall(r"<code>(.*?)</code>", result) == ["0° 0°", *temperature_cells]
    assert len(lines) == len(cases)
    for line, case, temperature_cell in zip(lines, cases, temperature_cells):
        _, hour, _, humidity, wind = case
        assert line == (
            f"{hour:02}:00 | <code>{temperature_cell}</code> | "
            f"{humidity}% | {wind:.1f}𝑣 | ⬇️ ☀️"
        )
        assert line.count("<code>") == line.count("</code>") == 1
    assert "123456" not in result
    assert all(f"{day:02}.11" in result for day in (8, 9, 10))
    assert result.startswith("&lt;Город &amp; море&gt;\n<code>0° 0°</code>")


@pytest.mark.parametrize(
    ("temperatures", "formatted"),
    [
        ([0.0, 1.2, 9.9], ["0", "1", "10"]),
        ([9.2, 10.0], ["9", "10"]),
        ([-0.01, 0.0], ["0", "0"]),
        ([-1.2, 1.2], ["-1", "1"]),
        ([-100.0, 9.0], ["-100", "9"]),
        ([9.49, 9.5], ["9", "10"]),
        ([-9.5, 9.5], ["-10", "10"]),
        ([-0.4, 0.4], ["0", "0"]),
    ],
)
async def test_temperature_width_is_actual_max_without_minimum_five(
    monkeypatch: pytest.MonkeyPatch,
    temperatures: list[float],
    formatted: list[str],
) -> None:
    """Пробелы добавляются только слева до длины самой длинной температуры."""
    entries = []
    for index, temperature in enumerate(temperatures):
        entry = _entry(_moment(hour=index * 3))
        entry["main"]["temp"] = temperature
        entries.append(entry)
    monkeypatch.setattr(weather, "get_json", AsyncMock(return_value=_forecast(entries)))
    result = await weather.parse_weather_data(2, "Город", None, None, api_key="key")
    cells = re.findall(r"<code>(.*?)</code>", result)[1:]
    width = max(map(len, formatted))
    assert cells == [f"{temperature:>{width}}°" for temperature in formatted]
    assert all(len(cell) == width + 1 for cell in cells)
    assert all(cell.endswith("°") and cell == cell.rstrip() for cell in cells)


async def test_forecast_keeps_three_local_calendar_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Граница трёх дней считается в часовом поясе выбранного города."""
    entries = [_entry(_moment(day=day, hour=21)) for day in [8, 9, 10, 11]]
    entries[0]["wind"].pop("deg")
    monkeypatch.setattr(
        weather, "get_json", AsyncMock(return_value=_forecast(entries, 4 * 3600))
    )
    result = await weather.parse_weather_data(2, "Город", None, None, api_key="key")
    assert "09.11" in result and "10.11" in result and "11.11" in result
    assert "12.11" not in result
    assert " | — ☀️" in result
    assert result.count("01:00") == 3


@pytest.mark.parametrize(
    "temperature", [float("nan"), float("inf"), -float("inf"), "<script>", None, True]
)
async def test_invalid_numeric_payload_returns_safe_error(
    monkeypatch: pytest.MonkeyPatch, temperature: object
) -> None:
    """Некорректные числа не попадают в HTML и не обрывают обработчик."""
    entry = _entry(_moment())
    entry["main"]["temp"] = temperature
    monkeypatch.setattr(weather, "get_json", AsyncMock(return_value=_forecast([entry])))
    result = await weather.parse_weather_data(2, "Город", None, None, api_key="key")
    assert result == "Сервис погоды вернул некорректный ответ"


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("method", ["city", "location"])
@pytest.mark.parametrize("api_error", [False, True])
async def test_real_router_three_days_returns_main_on_success_and_api_error(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    method: str,
    api_error: bool,
) -> None:
    """Настоящий Dispatcher завершает прогноз главной панелью, без выбора периода."""
    bot = Bot("123456:OFFLINE_TEST_ONLY")
    bot.test_mode = True
    bot.wordli_webapp_url = "https://game.invalid"
    bot.api_weather = "offline-key"
    dispatcher = Dispatcher()
    dispatcher.include_router(weather.weather_router)
    i18n = I18n(
        path=Path(__file__).resolve().parents[1] / "locales",
        default_locale="ru",
        domain="bot_00_template",
    )
    for module in (weather, private, keyboard):
        monkeypatch.setattr(module, "_", i18n.gettext)
    request = AsyncMock(
        side_effect=ExternalServiceError("offline") if api_error else None,
        return_value=_forecast([_entry(_moment(day=day)) for day in (8, 9, 10)]),
    )
    monkeypatch.setattr(weather, "get_json", request)
    outgoing = []

    async def send_message(
        active_bot: Bot, api_method: object, **kwargs: object
    ) -> Message:
        """Имитирует ответ Telegram, не обращаясь к сети."""
        outgoing.append(api_method)
        return Message.model_validate(
            {
                "message_id": len(outgoing),
                "date": _moment(),
                "chat": {"id": 99, "type": "private"},
                "text": getattr(api_method, "text", ""),
            }
        )

    monkeypatch.setattr(Bot, "__call__", send_message)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=99, user_id=99)
    await state.set_data(
        {
            "locale": language,
            "weather_location": {"city": "Old"},
            "city_name_period": "Old",
            "city_name_moment": "Old",
        }
    )
    try:
        with i18n.context(), i18n.use_locale(language):
            inputs = [{"text": i18n.gettext("Погода 🌊")}]
            if method == "city":
                inputs += [{"text": i18n.gettext("По названию 🏙")}, {"text": "Тбилиси"}]
            else:
                inputs.append({"location": {"latitude": 0, "longitude": 0}})
            inputs += [
                {"text": i18n.gettext("Текущий ⏺")},
                {"text": i18n.gettext("На 3 дня ⏩")},
            ]
            for number, content in enumerate(inputs, 1):
                update = Update.model_validate(
                    {
                        "update_id": number,
                        "message": {
                            "message_id": number,
                            "date": _moment(),
                            "chat": {"id": 99, "type": "private"},
                            "from": {"id": 99, "is_bot": False, "first_name": "Тест"},
                            **content,
                        },
                    }
                )
                await dispatcher.feed_update(bot, update)
            question = i18n.gettext("Каким способом определить прогноз?")
            panel = i18n.gettext("Главная панель")
            assert sum(getattr(item, "text", "") == question for item in outgoing) == 1
            assert sum(getattr(item, "text", "") == panel for item in outgoing) == 3
            for item in outgoing:
                markup = getattr(item, "reply_markup", None)
                if getattr(item, "text", "") == panel:
                    assert [len(row) for row in markup.keyboard] == [2, 2]
                    assert [
                        button.text for row in markup.keyboard for button in row
                    ] == [
                        i18n.gettext("Погода 🌊"),
                        i18n.gettext("Валюта 💵"),
                        i18n.gettext("Котики 🐱"),
                        i18n.gettext("Игры 🕹"),
                    ]
                if markup is not None and hasattr(markup, "keyboard"):
                    labels = [button.text for row in markup.keyboard for button in row]
                    assert i18n.gettext("Текущий ⏺") not in labels
                    assert i18n.gettext("На 3 дня ⏩") not in labels
                assert getattr(item, "text", "") != i18n.gettext(
                    "Выберите период прогноза"
                )
            if api_error:
                assert any(
                    getattr(item, "text", "")
                    == i18n.gettext("Сервис погоды временно недоступен")
                    for item in outgoing
                )
        request.assert_awaited_once()
        assert request.await_args.args[0].endswith("/forecast")
        params = request.await_args.kwargs["params"]
        if method == "city":
            assert params["q"] == "Тбилиси"
        else:
            assert params["lat"] == params["lon"] == 0
        assert await state.get_state() is None
        assert await state.get_data() == {"locale": language}
    finally:
        weather.weather_router._parent_router = None
        dispatcher.sub_routers.clear()
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("handler", ["open_main", "open_info"])
async def test_global_navigation_forgets_weather_location(
    monkeypatch: pytest.MonkeyPatch,
    state: FSMContext,
    handler: str,
) -> None:
    """Выход на главную или в настройки не сохраняет устаревший город."""
    from handlers import navigation

    target_module, target = (
        (navigation.private, "cancel_cmd")
        if handler == "open_main"
        else (navigation.other, "process_help_command")
    )
    monkeypatch.setattr(target_module, target, AsyncMock())
    await state.set_state(weather.City.city_name_period)
    await state.set_data({"locale": "en", "weather_location": {"city": "Old"}})
    arguments = [SimpleNamespace(), state]
    if handler == "open_info":
        arguments.append(SimpleNamespace())
    await getattr(navigation, handler)(*arguments)
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": "en"}
