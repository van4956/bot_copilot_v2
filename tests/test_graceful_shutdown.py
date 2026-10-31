"""Проверки завершения незаконченных обновлений без внешних запросов."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from app import GracefulDispatcher, create_dispatcher
from config_data.config import Config, DataBase, Redis, TgBot


@pytest.mark.asyncio
async def test_shutdown_waits_before_storage_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запись обновления завершается раньше закрытия общего хранилища."""
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    async def feed(self: Dispatcher, bot: Bot, update: Update, **kwargs: Any) -> str:
        """Имитирует обработчик, ожидающий завершения записи."""
        entered.set()
        await release.wait()
        events.append("saved")
        return "handled"

    async def close() -> None:
        """Фиксирует момент закрытия зависимости."""
        events.append("closed")

    monkeypatch.setattr(Dispatcher, "feed_update", feed)
    storage = MemoryStorage()
    monkeypatch.setattr(storage, "close", AsyncMock(side_effect=close))
    dispatcher = GracefulDispatcher(storage=storage)
    update_task = asyncio.create_task(
        dispatcher.feed_update(SimpleNamespace(), Update(update_id=1))
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    shutdown_task = asyncio.create_task(dispatcher.emit_shutdown())
    await asyncio.sleep(0)
    assert not shutdown_task.done()
    storage.close.assert_not_awaited()
    release.set()
    await asyncio.wait_for(shutdown_task, timeout=1)
    assert await update_task == "handled"
    assert events == ["saved", "closed"]


@pytest.mark.asyncio
async def test_shutdown_cancels_after_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Зависший обработчик отменяется и очищается до закрытия хранилища."""
    entered = asyncio.Event()
    events: list[str] = []

    async def feed(self: Dispatcher, bot: Bot, update: Update, **kwargs: Any) -> None:
        """Имитирует долгую операцию с обязательным завершением finally."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cleanup")

    async def close() -> None:
        """Проверяет порядок закрытия зависимости."""
        events.append("closed")

    monkeypatch.setattr(Dispatcher, "feed_update", feed)
    storage = MemoryStorage()
    monkeypatch.setattr(storage, "close", AsyncMock(side_effect=close))
    dispatcher = GracefulDispatcher(storage=storage, shutdown_timeout=0.01)
    task = asyncio.create_task(
        dispatcher.feed_update(SimpleNamespace(), Update(update_id=1))
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(dispatcher.emit_shutdown(), timeout=1)
    assert task.cancelled()
    assert events == ["cleanup", "closed"]


@pytest.mark.asyncio
async def test_queued_update_is_included_in_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Созданная перед shutdown задача учитывается до первого её запуска."""
    events: list[str] = []

    async def feed(self: Dispatcher, bot: Bot, update: Update, **kwargs: Any) -> None:
        """Имитирует уже поставленное в очередь обновление."""
        await asyncio.sleep(0.01)
        events.append("saved")

    async def close() -> None:
        """Фиксирует закрытие после очереди обновлений."""
        events.append("closed")

    monkeypatch.setattr(Dispatcher, "feed_update", feed)
    storage = MemoryStorage()
    monkeypatch.setattr(storage, "close", AsyncMock(side_effect=close))
    dispatcher = GracefulDispatcher(storage=storage)
    task = asyncio.create_task(
        dispatcher.feed_update(SimpleNamespace(), Update(update_id=1))
    )
    await dispatcher.emit_shutdown()
    assert task.done() and not task.cancelled()
    assert events == ["saved", "closed"]


@pytest.mark.asyncio
async def test_failed_update_does_not_skip_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Исключение активного обработчика не мешает закрыть зависимости."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def feed(self: Dispatcher, bot: Bot, update: Update, **kwargs: Any) -> None:
        """Имитирует ошибку одного обновления во время завершения."""
        entered.set()
        await release.wait()
        raise RuntimeError("handler error")

    monkeypatch.setattr(Dispatcher, "feed_update", feed)
    storage = MemoryStorage()
    monkeypatch.setattr(storage, "close", AsyncMock())
    dispatcher = GracefulDispatcher(storage=storage)
    task = asyncio.create_task(
        dispatcher.feed_update(SimpleNamespace(), Update(update_id=1))
    )
    await entered.wait()
    shutdown = asyncio.create_task(dispatcher.emit_shutdown())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(shutdown, timeout=1)
    assert isinstance(task.exception(), RuntimeError)
    storage.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_factory_uses_graceful_dispatcher() -> None:
    """Фабрика выбирает диспетчер с завершением активных обновлений."""
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_TEST_ONLY"),
        db=DataBase(),
        redis=Redis(),
    )
    dispatcher = create_dispatcher(config, AsyncMock(), routers=[])
    assert isinstance(dispatcher, GracefulDispatcher)
    await dispatcher.emit_shutdown()
