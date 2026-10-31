"""Асинхронные запросы без раскрытия ключей во внешних ошибках."""

import asyncio
from typing import Any

import aiohttp


class ExternalServiceError(Exception):
    """Безопасная для пользователя ошибка внешнего сервиса."""


async def get_json(
    url: str,
    *,
    params: dict | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 10,
) -> Any:
    """Получает JSON с ограничением времени и проверкой статуса."""
    try:
        if session is None:
            async with aiohttp.ClientSession() as client:
                return await get_json(
                    url, params=params, session=client, timeout=timeout
                )
        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            response.raise_for_status()
            return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        raise ExternalServiceError("Внешний сервис временно недоступен") from error
