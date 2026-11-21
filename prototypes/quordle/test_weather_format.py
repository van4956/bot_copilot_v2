"""Проверяет формат строк погоды в полном тестовом Copilot без сети."""

import unittest

from handlers.weather import _numeric_row


class WeatherFormatTest(unittest.TestCase):
    """Фиксирует различие между прогнозом и погодой сейчас."""

    def test_wind_separator(self) -> None:
        """Убирает последнюю черту только из строки прогноза."""
        values = ("12", "82", "2.7", "↘️", "🌦")
        forecast = f"19:00 | {_numeric_row(values)} {values[4]}"
        self.assertEqual(
            forecast, "19:00 | <code>12°</code> | 82% | 2.7𝑣  ↘️ 🌦"
        )
        self.assertEqual(
            _numeric_row(values, current=True),
            "<code>12°C</code> | 82% | 2.7𝑣 | ↘️",
        )
