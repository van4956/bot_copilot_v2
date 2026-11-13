"""Локализованные команды и безопасная регистрация без настоящего Telegram."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands
from aiogram.types import User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app
from common.comands import get_commands, sync_user_commands
from common.locales import LANGUAGE_CODES
from config_data.config import Config, DataBase, Redis, TgBot
from database.models import Base
from database.orm_users import orm_add_user


@pytest.mark.parametrize(
    "language,expected",
    [
        ("ru", ["Главная панель", "Кулинарная книга", "Настройки"]),
        ("en", ["Main panel", "Cookbook", "Settings"]),
    ],
)
def test_commands_are_localized_without_active_request_context(
    language: str,
    expected: list[str],
) -> None:
    """Язык меню задаётся явно и не зависит от предыдущего пользователя."""
    commands = get_commands(language)
    assert [command.command for command in commands] == ["main", "book", "info"]
    assert [command.description for command in commands] == expected


def test_command_locale_must_be_supported() -> None:
    """Служебный метод не читает произвольный каталог по входному значению."""
    with pytest.raises(ValueError):
        get_commands("../../private")


async def test_user_command_scope_respects_selected_bot_locale() -> None:
    """Личная область приоритетнее языка интерфейса Telegram пользователя."""
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    await sync_user_commands(bot, 99, "en")
    call = bot.set_my_commands.await_args
    assert call.kwargs["scope"].chat_id == 99
    assert "language_code" not in call.kwargs
    assert call.kwargs["request_timeout"] == 5
    assert [command.description for command in call.args[0]] == [
        "Main panel",
        "Cookbook",
        "Settings",
    ]


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=SetMyCommands(commands=[]), message="SECRET"),
        ClientError("SECRET"),
        asyncio.TimeoutError("SECRET"),
        TimeoutError("SECRET"),
    ],
)
async def test_command_menu_failure_is_nonfatal_and_does_not_log_secret(
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой необязательного меню не отменяет сохранённый язык и новый экран."""
    bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=error))
    await sync_user_commands(bot, 99, "ru")
    bot.set_my_commands.assert_awaited_once()
    assert "Не удалось обновить язык" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("admin_locale", LANGUAGE_CODES)
async def test_startup_registers_language_scopes_and_saved_admin_locale(
    monkeypatch: pytest.MonkeyPatch,
    admin_locale: str,
) -> None:
    """Проверяет запуск с SQLite в памяти и полностью подменённым Telegram."""
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_I18N_ONLY", admin_list=[99]),
        db=DataBase(),
        redis=Redis(),
        database_url="sqlite+aiosqlite:///:memory:",
        create_schema=True,
        expected_test_bot_id=123456,
        notify_service_chat=False,
    )
    database = create_async_engine(config.database_url)
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        await orm_add_user(
            session,
            {
                "user_id": 99,
                "user_name": "offline",
                "full_name": "Synthetic",
                "locale": admin_locale,
            },
        )
    bot = app.create_bot(config)
    bot.get_me = AsyncMock(
        return_value=User(id=123456, is_bot=True, first_name="Offline")
    )
    bot.delete_webhook = AsyncMock(return_value=True)
    bot.set_my_commands = AsyncMock(return_value=True)
    dispatcher_factory = app.create_dispatcher

    def create_dispatcher(settings: Config, pool: object) -> app.GracefulDispatcher:
        """Оставляет настоящий dispatcher, но не запускает polling и роутеры."""
        dispatcher = dispatcher_factory(settings, pool, routers=[])
        dispatcher.start_polling = AsyncMock()
        return dispatcher

    monkeypatch.setattr(app, "create_bot", lambda settings: bot)
    monkeypatch.setattr(app, "create_async_engine", lambda *args, **kwargs: database)
    monkeypatch.setattr(app, "create_dispatcher", create_dispatcher)
    try:
        await app.main(config)
        calls = bot.set_my_commands.await_args_list
        assert len(calls) == 6
        assert [call.kwargs.get("language_code") for call in calls[:5]] == [
            "",
            "ru",
            "en",
            "es",
            "uk",
        ]
        assert [call.kwargs["scope"].type for call in calls[:5]] == [
            "all_private_chats"
        ] * 5
        assert calls[-1].kwargs["scope"].chat_id == 99
        assert calls[-1].args[0] == get_commands(admin_locale)
        assert bot.get_me.await_count == 1
    finally:
        await database.dispose()
        await bot.session.close()


@pytest.mark.parametrize(
    "identity", [(654321, "other_bot"), (123456, "Terminatorvan_bot")]
)
async def test_wrong_test_bot_identity_blocks_telegram_mutations(
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[int, str],
) -> None:
    """Неверная личность останавливает запуск до команд, webhook и polling."""
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_I18N_ONLY"),
        db=DataBase(),
        redis=Redis(),
        database_url="sqlite+aiosqlite:///:memory:",
        create_schema=True,
        expected_test_bot_id=123456,
        notify_service_chat=False,
    )
    database = create_async_engine(config.database_url)
    bot = app.create_bot(config)
    bot.get_me = AsyncMock(
        return_value=User(
            id=identity[0],
            username=identity[1],
            is_bot=True,
            first_name="Offline",
        )
    )
    bot.delete_webhook = AsyncMock()
    bot.set_my_commands = AsyncMock()
    original = app.create_dispatcher
    dispatchers = []

    def build(settings: Config, sessions: object) -> app.GracefulDispatcher:
        """Подменяет только polling изолированного диспетчера."""
        dispatcher = original(settings, sessions, routers=[])
        dispatcher.start_polling = AsyncMock()
        dispatchers.append(dispatcher)
        return dispatcher

    monkeypatch.setattr(app, "create_bot", lambda settings: bot)
    monkeypatch.setattr(app, "create_async_engine", lambda *args, **kwargs: database)
    monkeypatch.setattr(app, "create_dispatcher", build)
    try:
        with pytest.raises(RuntimeError, match="ID тестового|основном боте"):
            await app.main(config)
        bot.delete_webhook.assert_not_awaited()
        bot.set_my_commands.assert_not_awaited()
        dispatchers[0].start_polling.assert_not_awaited()
    finally:
        await database.dispose()
        await bot.session.close()
