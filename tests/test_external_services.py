"""Отказы внешних сервисов, фильтры погоды и точность конвертации."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from common.http import ExternalServiceError, get_json
from handlers import currency, private, weather


@pytest.fixture(autouse=True)
def translations(monkeypatch):
    """Убирает зависимость проверок от текущей локали."""
    for module in (currency, private, weather):
        monkeypatch.setattr(module, "_", lambda text: text)


async def test_currency_missing_rate_is_not_assumed_one(monkeypatch):
    """Отсутствующий курс не превращается в вымышленный 1:1."""
    monkeypatch.setattr(
        currency,
        "get_json",
        AsyncMock(
            return_value={
                "rates": {"USD": 1},
                "timestamp": 1730000000,
            }
        ),
    )
    with pytest.raises(ExternalServiceError):
        await currency.get_currency_rate("USD", "EUR", api_key="dummy")


async def test_currency_decimal_and_api_key_after_import(monkeypatch):
    """Ключ поступает при вызове, расчёт не зависит от float."""
    request = AsyncMock(
        return_value={
            "rates": {"USD": 1, "EUR": 0.9},
            "timestamp": 1730000000,
        }
    )
    monkeypatch.setattr(currency, "get_json", request)
    _, _, rate = await currency.get_currency_rate("EUR", "USD", api_key="dummy")
    assert rate == Decimal("1") / Decimal("0.9")
    assert request.await_args.kwargs["params"]["app_id"] == "dummy"


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity"])
async def test_currency_rejects_invalid_rates(monkeypatch, value):
    """Нулевые, отрицательные и бесконечные курсы не используются."""
    monkeypatch.setattr(
        currency,
        "get_json",
        AsyncMock(
            return_value={
                "rates": {"USD": 1, "EUR": value},
                "timestamp": 1730000000,
            }
        ),
    )
    with pytest.raises(ExternalServiceError):
        await currency.get_currency_rate("USD", "EUR", api_key="dummy")


async def test_network_failure_finishes_currency_dialog(monkeypatch):
    """При отказе API диалог не зависает в ожидании целевой валюты."""
    monkeypatch.setattr(
        currency,
        "get_currency_rate",
        AsyncMock(side_effect=ExternalServiceError("offline")),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "currency_amount": "10",
                "currency_base": "USD",
            }
        ),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )
    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    callback = SimpleNamespace(
        data="EUR",
        bot=SimpleNamespace(api_currency="dummy"),
        message=message,
        answer=AsyncMock(),
    )
    monkeypatch.setattr(currency, "_next_keyboard", lambda: None)
    await currency.process_target_currency(callback, state)
    state.set_state.assert_awaited_once_with(None)
    assert "недоступен" in message.edit_text.await_args.args[0]


async def test_weather_accepts_zero_coordinates(monkeypatch):
    """Экватор и нулевой меридиан не отвергаются по ложному bool."""
    request = AsyncMock(
        return_value={
            "main": {"temp": 10, "humidity": 50, "pressure": 1000},
            "wind": {"speed": 1},
            "dt": 1730000000,
            "timezone": 0,
            "name": "<Test>",
            "coord": {"lon": 0, "lat": 0},
            "weather": [{"icon": "01d"}],
        }
    )
    monkeypatch.setattr(weather, "get_json", request)
    result = await weather.parse_weather_data(1, None, 0, 0, api_key="dummy")
    assert request.await_args.kwargs["params"]["lat"] == 0
    assert request.await_args.kwargs["params"]["lon"] == 0
    assert "&lt;Test&gt;" in result


async def test_weather_timeout_is_safe(monkeypatch):
    """Ошибка сервиса не раскрывает ключ пользователю."""
    monkeypatch.setattr(
        weather,
        "get_json",
        AsyncMock(side_effect=ExternalServiceError("private-url-with-key")),
    )
    result = await weather.parse_weather_data(1, "Test", None, None, api_key="dummy")
    assert "недоступен" in result
    assert "private-url" not in result


def test_edited_messages_do_not_start_weather():
    """Редактирование обычного сообщения больше не меняет сценарий."""
    assert weather.weather_router.edited_message.handlers == []


async def test_cats_timeout_is_safe(monkeypatch):
    """Тайм-аут котиков даёт ответ вместо исключения."""
    monkeypatch.setattr(private.r, "random", lambda: 0.9)
    monkeypatch.setattr(
        private, "get_json", AsyncMock(side_effect=ExternalServiceError("private-url"))
    )
    message = SimpleNamespace(
        bot=SimpleNamespace(),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    await private.cat_cmd(message, {})
    message.answer.assert_awaited_once()


class FakeResponse:
    """Контекст ответа без сетевого соединения."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        raise aiohttp.ClientConnectionError("URL contains secret")

    async def json(self):
        return {}


async def test_http_normalizes_error_and_timeout():
    """Обёртка задаёт тайм-аут и возвращает безопасную ошибку."""
    seen = {}

    def request(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse()

    session = SimpleNamespace(get=request)
    with pytest.raises(ExternalServiceError) as error:
        await get_json("https://example.invalid", session=session, timeout=2)
    assert seen["timeout"].total == 2
    assert "secret" not in str(error.value)


async def test_async_service_does_not_block_event_loop(monkeypatch):
    """Пока внешний сервис ожидается, другая задача продолжает работать."""
    entered = asyncio.Event()
    released = asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await released.wait()
        return {"rates": {"USD": 1, "EUR": 0.9}, "timestamp": 1730000000}

    monkeypatch.setattr(currency, "get_json", delayed)
    task = asyncio.create_task(
        currency.get_currency_rate("USD", "EUR", api_key="dummy")
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert not task.done()
    released.set()
    await asyncio.wait_for(task, 1)
