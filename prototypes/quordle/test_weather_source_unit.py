"""Проверки тестового прогноза без Telegram и сетевых запросов."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from common.http import ExternalServiceError
from prototypes.quordle import test_weather_source as source


def _current() -> dict:
    """Возвращает минимальные искусственные измерения для проверки формата."""
    return {
        "time": 0, "temperature_2m": -2.5, "relative_humidity_2m": 81,
        "wind_speed_10m": 1.5, "wind_direction_10m": 0, "weather_code": 0,
        "is_day": 0,
    }


class TestWeatherSource(unittest.IsolatedAsyncioTestCase):
    """Проверяет геокодирование, данные, ошибки и локальное время."""

    async def test_city_current(self) -> None:
        """Проверяет очистку HTML, м/с, ночь и округление половин."""
        place = {"name": "<Berlin>", "latitude": 52.5, "longitude": 13.4}
        fetch = AsyncMock(side_effect=[
            {"results": [place]}, {"timezone": "UTC", "current": _current()},
        ])
        with patch.object(source, "get_json", fetch):
            html = await source.fetch_test_weather(
                1, " Berlin ", None, None, "de", api_key="unused"
            )
        self.assertIn("&lt;Berlin&gt;", html)
        self.assertIn("-3°C", html)
        self.assertIn("1.5𝑣 | ⬇️ 🌙", html)
        self.assertIn("GeoNames", html)
        self.assertEqual(
            fetch.await_args_list[0].kwargs["params"]["name"], "Berlin"
        )
        params = fetch.await_args_list[1].kwargs["params"]
        self.assertEqual(params["wind_speed_unit"], "ms")
        self.assertNotIn("apikey", params)

    async def test_three_local_dates(self) -> None:
        """Проверяет будущие сроки и переход на летнее время."""
        start = int(datetime(2026, 3, 28, tzinfo=timezone.utc).timestamp())
        clock = [start + i * 3600 for i in range(76)]
        hourly = {k: [_current()[k]] * len(clock) for k in source.FIELDS}
        payload = {
            "timezone": "Europe/Berlin", "current": {"time": start + 60},
            "hourly": {**hourly, "time": clock},
        }
        with patch.object(source, "get_json", AsyncMock(return_value=payload)):
            html = await source.fetch_test_weather(2, None, 0.0, 0.0)
        self.assertIn("GPS", html)
        self.assertNotIn("GeoNames", html)
        self.assertIn("28.03\n04:00", html)
        self.assertIn("29.03\n01:00", html)
        self.assertIn("30.03\n02:00", html)
        self.assertNotIn("31.03", html)
        self.assertIn("05:00 |", html)

    async def test_no_fake_location_or_forecast(self) -> None:
        """Пустой ввод, неведомый город и сбой сети не заменяются образцом."""
        fetch = AsyncMock(side_effect=[{}, ExternalServiceError()])
        with patch.object(source, "get_json", fetch):
            no_place = await source.fetch_test_weather(2, " ", None, None)
            fetch.assert_not_awaited()
            unknown = await source.fetch_test_weather(2, "нет", None, None)
            down = await source.fetch_test_weather(1, None, 0, 0)
        self.assertIn("Неверные параметры", no_place)
        self.assertIn("не найден", unknown)
        self.assertIn("недоступен", down)
        self.assertNotIn("Open-Meteo", down)
