"""Сквозная офлайн-проверка маршрутизации через настоящий Dispatcher."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram.types import Update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import create_bot, create_dispatcher
from config_data.config import Config, DataBase, Redis, TgBot
from database.models import Base
from handlers import donate, miniapp, other, private


async def test_global_commands_escape_state_handlers(monkeypatch):
    """Команды меню проходят раньше широких обработчиков текущего FSM."""
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_TEST_ONLY"),
        db=DataBase(),
        redis=Redis(),
        mode="test",
        database_url="sqlite+aiosqlite:///:memory:",
        create_schema=True,
    )
    engine = create_async_engine(config.database_url)
    bot = create_bot(config)
    dp = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        dp = create_dispatcher(config, async_sessionmaker(engine))
        assert dp.sub_routers[0] is donate.payment_router
        state = dp.fsm.get_context(bot=bot, chat_id=9000000001, user_id=9000000001)
        cases = [
            ("/mini", miniapp, "cmd_miniapp"),
            ("/info", other, "process_help_command"),
            ("/main", private, "cancel_cmd"),
        ]
        for number, (command, module, function) in enumerate(cases, 1):
            handler = AsyncMock()
            monkeypatch.setattr(module, function, handler)
            await state.set_state("Currency:amount")
            await state.set_data({"locale": "en", "user_value": "5"})
            update = Update.model_validate(
                {
                    "update_id": number,
                    "message": {
                        "message_id": number,
                        "date": datetime.now(timezone.utc),
                        "chat": {"id": 9000000001, "type": "private"},
                        "from": {
                            "id": 9000000001,
                            "is_bot": False,
                            "first_name": "Synthetic",
                            "language_code": "en",
                        },
                        "text": command,
                        "entities": [
                            {
                                "type": "bot_command",
                                "offset": 0,
                                "length": len(command),
                            }
                        ],
                    },
                }
            )
            # В этом тесте маршрутизации временное ограничение отключено.
            from middlewares.throttle import ThrottleMiddleware

            for middleware in dp.update.outer_middleware:
                if isinstance(middleware, ThrottleMiddleware):
                    middleware.ttl = 0
            await dp.feed_update(bot, update)
            handler.assert_awaited_once()
            assert await state.get_state() is None
            assert await state.get_data() == {"locale": "en"}
    finally:
        if dp is not None:
            await dp.emit_shutdown()
            for router in dp.sub_routers:
                router._parent_router = None
            dp.sub_routers.clear()
        await engine.dispose()
        await bot.session.close()
