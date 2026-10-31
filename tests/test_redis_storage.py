"""Офлайн-проверки Redis FSM без подключения к настоящему серверу."""

import fakeredis
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from fakeredis.aioredis import FakeRedis


async def test_redis_fsm_survives_client_recreation():
    """Локаль и состояние сохраняются при создании нового клиента."""
    server = fakeredis.FakeServer()
    first = RedisStorage(
        FakeRedis(server=server),
        key_builder=DefaultKeyBuilder(prefix="copilot-review-test"),
    )
    second = RedisStorage(
        FakeRedis(server=server),
        key_builder=DefaultKeyBuilder(prefix="copilot-review-test"),
    )
    key = StorageKey(bot_id=123456, chat_id=9000000001, user_id=9000000001)
    try:
        await first.set_data(key, {"locale": "en"})
        await first.set_state(key, "Currency:amount")
        assert await second.get_data(key) == {"locale": "en"}
        assert await second.get_state(key) == "Currency:amount"
    finally:
        await first.close()
        await second.close()


async def test_redis_namespace_does_not_touch_other_keys():
    """Очистка состояния тестового пользователя не удаляет чужие ключи."""
    client = FakeRedis()
    storage = RedisStorage(
        client, key_builder=DefaultKeyBuilder(prefix="copilot-review-test")
    )
    key = StorageKey(bot_id=123456, chat_id=9000000001, user_id=9000000001)
    try:
        await client.set("another-project:state", "untouched")
        await storage.set_data(key, {"locale": "ru"})
        await storage.set_data(key, {})
        assert await client.get("another-project:state") == b"untouched"
    finally:
        await storage.close()
