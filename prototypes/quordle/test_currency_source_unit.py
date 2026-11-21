"""Автономные проверки тестового конвертера без сети и Telegram."""

import asyncio
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from prototypes.quordle import test_currency_source as source


class CurrencySourceTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет тип курса, кэш, дату и поведение при ошибках."""

    def setUp(self) -> None:
        """Подменяет сетевой вызов и изолирует кэш каждой проверки."""
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(source, "_cache", None))
        patches.enter_context(patch.object(source, "_lock", asyncio.Lock()))
        patches.enter_context(patch.object(source, "_retry_after", 0.0))
        self.http = patches.enter_context(patch.object(source, "get_json"))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.payload = {
            "result": "success",
            "base_code": "USD",
            "time_last_update_unix": int(self.now.timestamp()),
            "time_next_update_unix": int(
                (self.now + timedelta(days=1)).timestamp()
            ),
            "rates": dict.fromkeys(source.CURRENCY_CODES, 1)
            | {"EUR": "0.8", "GEL": "2.4"},
        }
        self.http.return_value = self.payload

    async def test_cross_rate_and_private_request(self) -> None:
        """Отправляет один фиксированный запрос без пары, суммы и ключа."""
        results = await asyncio.gather(
            source.get_currency_rate("EUR", "GEL", api_key="unused-test"),
            source.get_currency_rate("GEL", "GEL"),
        )
        self.assertEqual(
            results[0],
            (
                self.now.strftime("%H:%M"), self.now.strftime("%d-%m-%Y"),
                Decimal("3"),
            ),
        )
        self.assertIsInstance(results[0][2], Decimal)
        self.assertEqual(results[1][2], Decimal("1"))
        self.http.assert_awaited_once_with(
            source.RATES_URL, session=None, timeout=10
        )

    async def test_unsupported_code_without_network(self) -> None:
        """Отклоняет чужой код до сетевого запроса."""
        with self.assertRaises(source.ExternalServiceError):
            await source.get_currency_rate("USD", "NOT-A-CURRENCY")
        self.http.assert_not_awaited()

    async def test_failure_has_no_fake_rate_and_backs_off(self) -> None:
        """Не подставляет единицу и не повторяет запрос при недоступности."""
        self.http.side_effect = source.ExternalServiceError("Сбой")
        for _ in range(2):
            with self.assertRaises(source.ExternalServiceError):
                await source.get_currency_rate("USD", "USD")
        self.http.assert_awaited_once()

    def test_invalid_snapshots(self) -> None:
        """Не принимает просроченные, неполные и нечисловые данные."""
        for change in (
            {"result": "error"},
            {"base_code": "EUR"},
            {"rates": {}},
            {"rates": self.payload["rates"] | {"EUR": 0}},
            {"rates": self.payload["rates"] | {"EUR": "Infinity"}},
            {"time_last_update_unix": int(self.now.timestamp()) - 172801},
            {"time_next_update_unix": True},
        ):
            with self.subTest(change=change):
                with self.assertRaises(source.ExternalServiceError):
                    source._parse_snapshot(self.payload | change, self.now)
