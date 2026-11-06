"""Сетевая изоляция тестов образа Wordli без импорта Telegram-бота."""

import socket
from urllib.parse import urlsplit

import aiohttp
import pytest

LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1", "db-test", "redis-test"}


@pytest.fixture(autouse=True)
def isolate_wordli_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оставляет только loopback и отдельные review-службы Docker."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_ENV_FILE", "/app/.test-data/absent-offline-settings.env")
    real_resolver = socket.getaddrinfo
    real_request = aiohttp.ClientSession._request

    def local_resolver(host, *args, **kwargs):
        """Не разрешает внешние имена даже при ошибочной настройке теста."""
        value = host.decode() if isinstance(host, bytes) else host
        if value not in LOCAL_HOSTS:
            raise AssertionError("Внешняя сеть запрещена в тестах Wordli")
        return real_resolver(host, *args, **kwargs)

    async def local_request(session, method, url, *args, **kwargs):
        """Запрещает внешние HTTP-запросы, включая прямые числовые IP."""
        if urlsplit(str(url)).hostname not in LOCAL_HOSTS:
            raise AssertionError("Внешний HTTP запрещён в тестах Wordli")
        return await real_request(session, method, url, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_resolver)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", local_request)
