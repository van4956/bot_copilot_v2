"""Запуск бота без побочных действий при импорте модуля."""

import asyncio
import logging
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.fsm.strategy import FSMStrategy
from aiogram.utils.i18n import FSMI18nMiddleware, I18n
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from common.comands import get_commands
from config_data.config import (
    ROOT,
    Config,
    load_config,
    validate_test_database,
    validate_wordli_url,
)
from database.models import Base
from database.orm_users import orm_get_locale
from middlewares import counter, db, locale, throttle

logger = logging.getLogger(__name__)


class GracefulDispatcher(Dispatcher):
    """Дожидается обработчиков перед закрытием зависимостей FSM и приложения."""

    def __init__(
        self, *args: Any, shutdown_timeout: float = 30.0, **kwargs: Any
    ) -> None:
        """Настраивает ограниченное ожидание незавершённых обновлений."""
        super().__init__(*args, **kwargs)
        self.shutdown_timeout = shutdown_timeout
        self._active_update_tasks: set[asyncio.Task[Any]] = set()

    async def feed_update(self, bot: Bot, update: types.Update, **kwargs: Any) -> Any:
        """Учитывает публичную обработку обновления без внутренних полей aiogram."""
        task = asyncio.current_task()
        if task is not None:
            self._active_update_tasks.add(task)
        try:
            return await super().feed_update(bot, update, **kwargs)
        finally:
            if task is not None:
                self._active_update_tasks.discard(task)

    async def drain_updates(self) -> None:
        """Ждёт активные задачи, затем отменяет и завершает просроченные."""
        # Задачи, уже созданные polling, должны успеть войти в feed_update.
        await asyncio.sleep(0)
        current = asyncio.current_task()
        tasks = {
            task
            for task in self._active_update_tasks
            if task is not current and not task.done()
        }
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=max(0.0, self.shutdown_timeout))
        if pending:
            logger.warning(
                "Время завершения обработчиков истекло; отменяется задач: %s",
                len(pending),
            )
            for task in pending:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def emit_shutdown(self, *args: Any, **kwargs: Any) -> None:
        """Завершает обновления раньше штатного закрытия FSM-хранилища."""
        await self.drain_updates()
        await super().emit_shutdown(*args, **kwargs)


def create_bot(config: Config) -> Bot:
    """Создаёт клиент без запросов к Telegram."""
    wordli_requested = config.test_mode or config.wordli_enabled is True
    wordli_url = (
        validate_wordli_url(
            config.wordli_webapp_url, test_mode=config.test_mode
        )
        if wordli_requested
        else ""
    )
    if not config.test_mode and config.wordli_enabled is True and not wordli_url:
        raise ValueError("При WORDLI_ENABLED=true нужен WORDLI_WEBAPP_URL")
    token = config.tg_bot.token_test if config.test_mode else config.tg_bot.token
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    for name in (
        "owner",
        "admin_list",
        "home_group",
        "work_group",
        "api_gpt",
        "api_weather",
        "api_currency",
    ):
        setattr(bot, name, getattr(config.tg_bot, name))
    bot.test_mode = config.test_mode
    bot.notify_service_chat = config.notify_service_chat
    bot.webapp_base_url = config.webapp_base_url
    bot.wordli_webapp_url = wordli_url
    bot.wordli_enabled_verified = bool(wordli_url and wordli_requested)
    custom_emoji_enabled = bool(bot.wordli_webapp_url and config.test_mode)
    bot.wordli_custom_emoji_id = (
        config.wordli_custom_emoji_id if custom_emoji_enabled else ""
    )
    bot.wordli_custom_emoji_fallback = (
        (config.wordli_custom_emoji_fallback or "🧩")
        if custom_emoji_enabled and config.wordli_custom_emoji_id else ""
    )
    return bot


def create_dispatcher(
    config: Config,
    sessions: async_sessionmaker[AsyncSession],
    routers: list[Router] | None = None,
) -> Dispatcher:
    """Собирает обработчики и хранилище без сетевых запросов."""
    storage = (
        RedisStorage.from_url(
            config.redis.url,
            key_builder=DefaultKeyBuilder(prefix=config.redis.prefix),
        )
        if config.redis.url
        else MemoryStorage()
    )
    dp = GracefulDispatcher(fsm_strategy=FSMStrategy.USER_IN_CHAT, storage=storage)
    dp.update.outer_middleware(throttle.ThrottleMiddleware())
    dp.update.outer_middleware(counter.CounterMiddleware())
    dp.update.outer_middleware(db.DataBaseSession(session_pool=sessions))
    dp.update.outer_middleware(
        locale.LocaleFromDBMiddleware(workflow_data=dp.workflow_data)
    )
    i18n = I18n(
        path=str(ROOT / "locales"),
        default_locale="ru",
        domain="bot_00_template",
    )
    dp.update.middleware(FSMI18nMiddleware(i18n=i18n))
    if routers is None:
        from handlers import (
            admin,
            cookbook,
            currency,
            donate,
            group,
            # miniapp,  # Мини-приложения временно скрыты, код сохранён.
            navigation,
            other,
            owner,
            private,
            start,
            weather,
        )

        routers = [
            donate.payment_router,
            # miniapp.webapp_data_router,
            navigation.navigation_router,
            start.start_router,
            owner.owner_router,
            admin.admin_router,
            private.private_router,
            donate.donate_router,
            weather.weather_router,
            currency.currency_router,
            cookbook.cookbook_router,
            # miniapp.miniapp_router,
            group.group_router,
            other.other_router,
        ]
    for router in routers:
        dp.include_router(router)
    return dp


async def notify_service(bot: Bot, config: Config, text: str) -> None:
    """Отправляет необязательное уведомление без остановки приложения."""
    if not config.notify_service_chat or not config.tg_bot.home_group:
        return
    try:
        await bot.send_message(config.tg_bot.home_group[0], text)
    except Exception:
        logger.warning("Не удалось отправить служебное уведомление")


async def main(config: Config | None = None) -> None:
    """Запускает выбранную среду и освобождает подключения при любой ошибке."""
    config = config or load_config()
    bot = create_bot(config)
    engine = None
    dp = None
    try:
        if config.test_mode:
            validate_test_database(config.database_url)
            url = make_url(config.database_url)
            if url.drivername.startswith("sqlite") and url.database != ":memory:":
                from pathlib import Path

                Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(config.database_url, echo=False)
        sessions = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        dp = create_dispatcher(config, sessions)
        async with engine.begin() as connection:
            if config.create_schema:
                await connection.run_sync(Base.metadata.create_all)
            else:
                tables = await connection.run_sync(
                    lambda conn: set(inspect(conn).get_table_names())
                )
                if not set(Base.metadata.tables).issubset(tables):
                    raise RuntimeError(
                        "Схема БД не готова: примените проверенную миграцию"
                    )
        async with aiohttp.ClientSession() as http:
            bot.http_session = http
            identity = await bot.get_me()
            if config.test_mode:
                if (identity.username or "").casefold() == "terminatorvan_bot":
                    raise RuntimeError("Отказ запуска тестов на основном боте")
                if (
                    config.expected_test_bot_id
                    and identity.id != config.expected_test_bot_id
                ):
                    raise RuntimeError("ID тестового бота не совпадает")
            await bot.delete_webhook(drop_pending_updates=config.drop_pending_updates)
            for language_code in ("", "ru", "en"):
                await bot.set_my_commands(
                    get_commands(language_code or "ru"),
                    scope=types.BotCommandScopeAllPrivateChats(),
                    language_code=language_code,
                )
            for admin_id in config.tg_bot.admin_list:
                async with sessions() as session:
                    admin_locale = await orm_get_locale(session, admin_id) or "ru"
                await bot.set_my_commands(
                    get_commands(admin_locale),
                    scope=types.BotCommandScopeChat(chat_id=admin_id),
                )
            await notify_service(bot, config, "🤖 Бот запущен")
            try:
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                    polling_timeout=60,
                    close_bot_session=False,
                )
            finally:
                await notify_service(bot, config, "Бот остановлен")
    finally:
        try:
            if dp is not None:
                await dp.storage.close()
        finally:
            try:
                if engine is not None:
                    await engine.dispose()
            finally:
                await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
