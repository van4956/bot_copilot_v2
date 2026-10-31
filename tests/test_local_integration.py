"""Проверки только выделенных PostgreSQL и Redis тестового Compose."""

import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config_data.config import validate_test_database
from database.models import Cookbook, Games, Users
from testing.seed_test_data import seed_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Отдельные PostgreSQL/Redis не запущены",
    ),
]


async def test_postgres_bootstrap_fixtures_and_roles():
    """Тестовая роль работает, но не получает административных полномочий."""
    url = os.environ["TEST_DATABASE_URL"]
    validate_test_database(url)
    await seed_database(url)
    second = await seed_database(url)
    assert second["users_added"] == 0
    assert second["recipes_added"] == 0
    assert second["games_added"] == 0
    engine = create_async_engine(url)
    try:
        sessions = async_sessionmaker(engine)
        async with sessions() as session:
            role = (
                await session.execute(
                    text(
                        "select current_user, rolsuper, rolcreatedb, rolcreaterole "
                        "from pg_roles where rolname = current_user"
                    )
                )
            ).one()
            assert role[0] == "bot_app"
            assert role[1:] == (False, False, False)
            for model in (Users, Cookbook, Games):
                assert (await session.execute(select(model))).first() is not None
    finally:
        await engine.dispose()


async def test_redis_has_own_namespace_and_recovers_locale():
    """Проверяет сохранение FSM без очистки посторонних ключей."""
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

    url = os.environ["TEST_REDIS_URL"]
    assert url.startswith("redis://redis-test:") and url.endswith("/15")
    client = Redis.from_url(url)
    prefix = "copilot-review-test:integration:" + uuid4().hex
    storage = RedisStorage(redis=client, key_builder=DefaultKeyBuilder(prefix=prefix))
    key = StorageKey(bot_id=123456, chat_id=9000000001, user_id=9000000001)
    try:
        assert await client.ping()
        await storage.set_data(key, {"locale": "en"})
        assert (await storage.get_data(key))["locale"] == "en"
        await storage.set_data(key, {})
    finally:
        await storage.close()
