"""Публичные справочные курсы для конвертера тестового Telegram-бота."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from time import monotonic

import aiohttp

from common.http import ExternalServiceError, get_json
from handlers.currency import CURRENCY_CODES

RATES_URL = "https://open.er-api.com/v6/latest/USD"
ATTRIBUTION_HTML = (
    '<a href="https://www.exchangerate-api.com">'
    "Rates By Exchange Rate API</a>"
)


@dataclass(frozen=True)
class _Snapshot:
    """Проверенная котировка в USD и срок её локального кэширования."""

    updated: datetime
    expires: datetime
    rates: dict[str, Decimal]


_cache: _Snapshot | None = None
_lock = asyncio.Lock()
_retry_after = 0.0


def _parse_snapshot(payload: object, now: datetime) -> _Snapshot:
    """Проверяет дату источника, базу и численное значение каждого курса."""
    try:
        if not isinstance(payload, dict) or payload.get("result") != "success":
            raise ValueError
        if payload.get("base_code") != "USD":
            raise ValueError
        last = payload["time_last_update_unix"]
        following = payload["time_next_update_unix"]
        if type(last) is not int or type(following) is not int:
            raise ValueError
        updated = datetime.fromtimestamp(last, tz=timezone.utc)
        next_update = datetime.fromtimestamp(following, tz=timezone.utc)
        if not now - timedelta(days=2) < updated <= now + timedelta(minutes=5):
            raise ValueError
        if next_update <= updated or not isinstance(payload["rates"], dict):
            raise ValueError

        rates = {
            code: Decimal(str(payload["rates"][code]))
            for code in CURRENCY_CODES
        }
        if rates["USD"] != 1 or any(
            not rate.is_finite() or rate <= 0 for rate in rates.values()
        ):
            raise ValueError
    except (
        KeyError, ValueError, TypeError, DecimalException, OSError, OverflowError
    ) as error:
        raise ExternalServiceError(
            "Некорректный ответ сервиса валют"
        ) from error

    # При задержке публикации повторяем запрос позже, а не на каждый расчёт.
    expires = min(
        max(next_update, now + timedelta(minutes=5)),
        now + timedelta(days=1),
        updated + timedelta(days=2),
    )
    return _Snapshot(updated, expires, rates)


async def get_currency_rate(
    base_currency: str,
    target_currency: str,
    *,
    api_key: str = "",
    session: aiohttp.ClientSession | None = None,
) -> tuple[str, str, Decimal]:
    """Возвращает время UTC, дату публикации и кросс-курс без API-ключа."""
    global _cache, _retry_after
    if (
        base_currency not in CURRENCY_CODES
        or target_currency not in CURRENCY_CODES
    ):
        raise ExternalServiceError("Неизвестная валюта")
    # api_key нужен только для совместимости; источник его не получает.
    async with _lock:
        now = datetime.now(timezone.utc)
        if _cache is None or now >= _cache.expires:
            if monotonic() < _retry_after:
                raise ExternalServiceError("Сервис валют временно недоступен")
            try:
                payload = await get_json(
                    RATES_URL, session=session, timeout=10
                )
                _cache = _parse_snapshot(payload, datetime.now(timezone.utc))
            except ExternalServiceError as error:
                status = getattr(error.__cause__, "status", None)
                _retry_after = monotonic() + (1200 if status == 429 else 60)
                raise
        snapshot = _cache

    try:
        rate = snapshot.rates[target_currency] / snapshot.rates[base_currency]
        if not rate.is_finite() or rate <= 0:
            raise ValueError
    except (DecimalException, ValueError) as error:
        raise ExternalServiceError("Некорректный курс валюты") from error
    return (
        snapshot.updated.strftime("%H:%M"),
        snapshot.updated.strftime("%d-%m-%Y"),
        rate,
    )
