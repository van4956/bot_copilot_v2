"""Общий кэш определений без изменений партий и статистики."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from weakref import WeakValueDictionary

from .engine import GameError
from .storage import WordliStorage
from .wiktionary import DefinitionUnavailable

FOUND_TTL = 30 * 24 * 60 * 60
NOT_FOUND_TTL = 24 * 60 * 60
ERROR_RETRY_SECONDS = 60


def _now() -> datetime:
    """Возвращает серверное время в UTC."""
    return datetime.now(timezone.utc)


class DefinitionService:
    """Разделяет определения между игроками, но проверяет доступ каждого."""

    def __init__(
        self,
        storage: WordliStorage,
        lookup: Callable[[str], Awaitable[dict[str, Any]]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Принимает хранилище, словарь и подменяемые часы."""
        self._storage = storage
        self._lookup = lookup
        self._clock = clock or _now
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _time(self) -> int:
        """Проверяет часы до использования сроков годности кэша."""
        value = self._clock()
        if value.utcoffset() is None:
            raise ValueError("Часы кэша должны включать часовой пояс")
        return int(value.timestamp())

    @staticmethod
    def _available(cache: dict[str, Any] | None, now: int) -> dict[str, Any] | None:
        """Не превращает временный сетевой сбой в отсутствие определения."""
        if cache is None:
            return None
        data = cache["data"]
        status = data.get("status")
        ttl = FOUND_TTL if status == "found" else NOT_FOUND_TTL
        if status in {"found", "not_found"} and cache["checked_at"] + ttl > now:
            return data
        if cache["retry_after"] > now:
            if status == "found":
                return data
            raise DefinitionUnavailable("Повторная попытка позже")
        return None

    async def get(self, user_id: int, value: str) -> dict[str, Any]:
        """Возвращает определение только после проверки сохранённой победы."""
        word = await self._storage.require_won_word(user_id, value)
        try:
            return await self._get_cached(word)
        except DefinitionUnavailable:
            raise GameError(
                "definition_unavailable", "Значение временно недоступно.", 503
            ) from None

    async def _get_cached(self, word: str) -> dict[str, Any]:
        """Объединяет одновременные промахи кэша одного слова."""
        cache = await self._storage.definition_cache(word)
        available = self._available(cache, self._time())
        if available is not None:
            return available
        lock = self._locks.setdefault(word, asyncio.Lock())
        async with lock:
            cache = await self._storage.definition_cache(word)
            now = self._time()
            available = self._available(cache, now)
            if available is not None:
                return available
            try:
                # Сетевой запрос выполняется без открытой транзакции БД.
                data = await asyncio.wait_for(self._lookup(word), timeout=8)
            except (DefinitionUnavailable, asyncio.TimeoutError):
                await self._storage.save_definition_cache(
                    word,
                    cache["data"] if cache else {},
                    cache["checked_at"] if cache else 0,
                    self._time() + ERROR_RETRY_SECONDS,
                )
                if cache and cache["data"].get("status") == "found":
                    return cache["data"]
                raise DefinitionUnavailable("Не удалось обновить определение") from None
            await self._storage.save_definition_cache(word, data, self._time())
            return data
