"""Общая защита автотестов от Telegram и внешних HTTP-сервисов."""

from pathlib import Path
import socket

import aiohttp
import pytest
from aiogram import Bot


@pytest.fixture(autouse=True)
def block_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оставляет доступ только локальным интеграционным службам."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(
        "APP_ENV_FILE",
        str(
            Path(__file__).resolve().parents[1] / ".test-data" / "offline-settings.env"
        ),
    )
    real_resolver = socket.getaddrinfo

    def safe_resolver(host, *args, **kwargs):
        """Запрещает внешнее разрешение имён в автотестах."""
        if host not in {
            None,
            "localhost",
            "127.0.0.1",
            "::1",
            "db-test",
            "redis-test",
            b"localhost",
            b"127.0.0.1",
        }:
            raise AssertionError("Внешняя сеть запрещена в автотестах")
        return real_resolver(host, *args, **kwargs)

    async def forbidden_bot_call(*args, **kwargs):
        """Не позволяет отправить настоящий запрос боту."""
        raise AssertionError("Telegram запрещён в автотестах; используйте заглушку")

    async def forbidden_http(*args, **kwargs):
        """Не позволяет отправить настоящий HTTP-запрос."""
        raise AssertionError("HTTP запрещён в автотестах; используйте заглушку")

    monkeypatch.setattr(socket, "getaddrinfo", safe_resolver)
    monkeypatch.setattr(Bot, "__call__", forbidden_bot_call)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbidden_http)
