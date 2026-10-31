"""Запуск приложения с настоящими тестовыми БД и заглушкой Telegram."""

import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncEngine

import app
from config_data.config import Config, DataBase, Redis, TgBot, validate_test_database
from testing.seed_test_data import seed_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Отдельные PostgreSQL/Redis не запущены",
    ),
]


@pytest.mark.parametrize("polling_fails", [False, True])
async def test_application_starts_and_closes_real_services(
    monkeypatch: pytest.MonkeyPatch, polling_fails: bool
) -> None:
    """Доходит до polling и закрывает службы при штатном и аварийном выходе."""
    database_url = os.environ["TEST_DATABASE_URL"]
    redis_url = os.environ["TEST_REDIS_URL"]
    validate_test_database(database_url)
    assert redis_url.startswith("redis://redis-test:") and redis_url.endswith("/15")
    await seed_database(database_url)
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_TEST_ONLY"),
        db=DataBase(),
        redis=Redis(url=redis_url, prefix=f"review-startup:{uuid4().hex}"),
        database_url=database_url,
        create_schema=False,
        expected_test_bot_id=123456,
    )
    bot = app.create_bot(config)
    bot.get_me = AsyncMock(
        return_value=User(id=123456, is_bot=True, first_name="Offline review")
    )
    bot.delete_webhook = AsyncMock(return_value=True)
    bot.set_my_commands = AsyncMock(return_value=True)
    bot.session.close = AsyncMock(wraps=bot.session.close)
    monkeypatch.setattr(app, "create_bot", lambda _: bot)
    original_engine = app.create_async_engine
    original_dispatcher = app.create_dispatcher
    original_dispose = AsyncEngine.dispose
    resources = {}
    disposed = []

    async def dispose(engine: AsyncEngine, close: bool = True) -> None:
        """Учитывает завершение настоящего движка через публичный метод класса."""
        await original_dispose(engine, close=close)
        disposed.append(engine)

    def create_engine(*args, **kwargs):
        """Сохраняет настоящий движок для проверки завершения."""
        engine = original_engine(*args, **kwargs)
        resources["engine"] = engine
        return engine

    def create_dispatcher(settings, sessions):
        """Оставляет настоящие middleware и Redis без повторной привязки роутеров."""
        dispatcher = original_dispatcher(settings, sessions, routers=[])
        dispatcher.storage.close = AsyncMock(wraps=dispatcher.storage.close)
        resources["dispatcher"] = dispatcher

        async def polling(client, **kwargs) -> None:
            """Вместо Telegram проверяет настоящее хранилище состояния."""
            assert client is bot
            assert kwargs["close_bot_session"] is False
            assert await dispatcher.storage.redis.ping()
            key = StorageKey(bot_id=123456, chat_id=9000000001, user_id=9000000001)
            try:
                await dispatcher.storage.set_data(key, {"locale": "en"})
                assert await dispatcher.storage.get_data(key) == {"locale": "en"}
            finally:
                await dispatcher.storage.set_data(key, {})
            if polling_fails:
                raise RuntimeError("synthetic polling failure")

        dispatcher.start_polling = AsyncMock(side_effect=polling)
        return dispatcher

    monkeypatch.setattr(app, "create_async_engine", create_engine)
    monkeypatch.setattr(app, "create_dispatcher", create_dispatcher)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    if polling_fails:
        with pytest.raises(RuntimeError, match="synthetic polling failure"):
            await app.main(config)
    else:
        await app.main(config)

    bot.get_me.assert_awaited_once()
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    bot.set_my_commands.assert_awaited_once()
    bot.session.close.assert_awaited_once()
    assert bot.http_session.closed
    resources["dispatcher"].start_polling.assert_awaited_once()
    resources["dispatcher"].storage.close.assert_awaited_once()
    assert disposed == [resources["engine"]]
