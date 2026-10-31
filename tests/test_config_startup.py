"""Проверки конфигурации и жизненного цикла без сети и боевых настроек."""

import asyncio
import importlib
import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.storage.memory import MemoryStorage

from config_data import config as settings

FAKE_TOKEN = "123456:synthetic_test_token_not_a_real_credential"


@pytest.fixture
def fake_environment(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Mock]:
    """Изолирует настройки от настоящего окружения и секретных файлов."""
    values = {}
    dotenv = Mock(return_value={})
    monkeypatch.setattr(settings.os, "environ", values)
    monkeypatch.setattr(settings, "dotenv_values", dotenv)
    return values, dotenv


def _config() -> settings.Config:
    """Создаёт безопасную конфигурацию без адресов внешних сервисов."""
    return settings.Config(
        tg_bot=settings.TgBot(token="", token_test=FAKE_TOKEN),
        db=settings.DataBase(db_lite="sqlite+aiosqlite:///:memory:"),
        redis=settings.Redis(),
        mode="test",
        database_url="sqlite+aiosqlite:///:memory:",
        create_schema=True,
        expected_test_bot_id=123456,
    )


def _app():
    """Импортирует приложение после включения изоляции настроек."""
    return importlib.import_module("app")


def test_import_has_no_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch, fake_environment
) -> None:
    """Импорт не читает конфиг и не создаёт клиентов или подключений."""
    import aiogram
    import aiohttp
    import sqlalchemy.ext.asyncio as sqlalchemy_async

    def forbidden(*args, **kwargs):
        """Останавливает любую попытку инициализации при импорте."""
        raise AssertionError("Runtime operation during import")

    monkeypatch.setattr(settings, "load_config", forbidden)
    monkeypatch.setattr(aiogram, "Bot", forbidden)
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(sqlalchemy_async, "create_async_engine", forbidden)
    spec = importlib.util.spec_from_file_location(
        "copilot_import_safety_probe", settings.ROOT / "app.py"
    )
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert callable(module.main)
    fake_environment[1].assert_not_called()


def test_default_mode_reads_only_test_settings(fake_environment) -> None:
    """По умолчанию выбирается тестовый файл, а не настоящий боевой."""
    environment, dotenv = fake_environment
    environment["BOT_TOKEN_TEST"] = FAKE_TOKEN
    config = settings.load_config()

    dotenv.assert_called_once_with(str(settings.ROOT / ".env.test.local"))
    assert config.test_mode
    assert config.tg_bot.token == ""
    assert config.tg_bot.token_test == FAKE_TOKEN
    assert config.drop_pending_updates is False


def test_test_mode_does_not_fall_back_to_normal_token(fake_environment) -> None:
    """Обычный токен не используется вместо отсутствующего тестового."""
    environment, _ = fake_environment
    environment["BOT_TOKEN"] = "987654:another_synthetic_token"

    with pytest.raises(ValueError, match="BOT_TOKEN_TEST"):
        settings.load_config()


def test_test_mode_discards_service_chat_destinations(fake_environment) -> None:
    """Тесты не отправляют уведомления в группы из окружения."""
    environment, _ = fake_environment
    environment.update(
        BOT_TOKEN_TEST=FAKE_TOKEN, HOME_GROUP="-123", WORK_GROUP="-456",
        NOTIFY_SERVICE_CHAT="true",
        DB_POST="postgresql+asyncpg://synthetic@public.invalid/ordinary_db",
    )
    config = settings.load_config()

    assert config.tg_bot.home_group == []
    assert config.tg_bot.work_group == []
    assert config.notify_service_chat is False
    assert "public.invalid" not in config.database_url


def test_configuration_repr_hides_secrets(fake_environment) -> None:
    """Строковое представление конфигурации не раскрывает токен."""
    environment, _ = fake_environment
    environment["BOT_TOKEN_TEST"] = FAKE_TOKEN

    assert FAKE_TOKEN not in repr(settings.load_config())


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:///:memory:",
    "postgresql+asyncpg://bot_app:synthetic@localhost/copilot_review_test",
    "postgresql+asyncpg://bot_app:synthetic@127.0.0.1/copilot_review_test",
    "postgresql+asyncpg://bot_app:synthetic@db-test/copilot_review_test",
])
def test_allowed_test_database(url: str) -> None:
    """Разрешены только отдельная локальная БД и память."""
    settings.validate_test_database(url)


def test_sqlite_must_stay_inside_test_directory() -> None:
    """Файловая SQLite разрешена только в специальном тестовом каталоге."""
    allowed = settings.ROOT / ".test-data" / "synthetic.sqlite3"
    settings.validate_test_database(f"sqlite+aiosqlite:///{allowed}")


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://bot_app:synthetic@db/bot_db",
    "postgresql+asyncpg://bot_app:synthetic@public.invalid/copilot_review_test",
    "postgresql+asyncpg://bot_app:synthetic@localhost/bot_db",
    "sqlite+aiosqlite:///database/sqlite.db",
    "sqlite+aiosqlite:///.test-data/../database/sqlite.db",
    "sqlite+aiosqlite:///.test-data-other/synthetic.sqlite3",
])
def test_production_or_unrelated_database_is_rejected(url: str) -> None:
    """Боевой адрес и выход из тестового каталога блокируются."""
    with pytest.raises(ValueError):
        settings.validate_test_database(url)


@pytest.mark.parametrize("url", [
    "redis://localhost:6379/15",
    "redis://127.0.0.1:6385/15",
    "redis://redis-test:6379/15",
])
def test_allowed_test_redis(fake_environment, url: str) -> None:
    """Тестовое состояние использует отдельную базу Redis."""
    environment, _ = fake_environment
    environment.update(BOT_TOKEN_TEST=FAKE_TOKEN, TEST_REDIS_URL=url)

    config = settings.load_config()

    assert config.redis.url == url
    assert config.redis.prefix == "copilot-review-test"


@pytest.mark.parametrize("url", [
    "redis://public.invalid:6379/15",
    "redis://localhost:6379/0",
    "redis://redis:6379/15",
    "https://localhost/15",
])
def test_unsafe_test_redis_is_rejected(fake_environment, url: str) -> None:
    """Посторонний Redis, обычная база и неправильный протокол запрещены."""
    environment, _ = fake_environment
    environment.update(BOT_TOKEN_TEST=FAKE_TOKEN, TEST_REDIS_URL=url)

    with pytest.raises(ValueError):
        settings.load_config()


def test_file_mode_conflict_is_rejected(fake_environment) -> None:
    """Явно боевой файл нельзя молча использовать в тестовом режиме."""
    environment, dotenv = fake_environment
    environment.update(APP_ENV="test", BOT_TOKEN_TEST=FAKE_TOKEN)
    dotenv.return_value = {"APP_ENV": "production"}

    with pytest.raises(ValueError, match="APP_ENV"):
        settings.load_config()


def test_production_webapps_are_not_used_by_tests(fake_environment) -> None:
    """Тестовый бот не открывает боевой GitHub Pages."""
    environment, _ = fake_environment
    environment.update(
        BOT_TOKEN_TEST=FAKE_TOKEN,
        WEBAPP_BASE_URL=settings.PRODUCTION_WEBAPP_URL,
    )

    with pytest.raises(ValueError):
        settings.load_config()


def test_create_bot_has_expected_attributes_without_network(
    fake_environment,
) -> None:
    """Создание клиента не обращается к Telegram и выбирает тестовый режим."""
    app = _app()
    bot = app.create_bot(_config())
    try:
        assert bot.test_mode is True
        assert bot.token == FAKE_TOKEN
        assert bot.home_group == []
        assert bot.webapp_base_url == ""
    finally:
        asyncio.run(bot.session.close())


def test_dispatcher_can_be_created_without_handlers_or_network(
    fake_environment,
) -> None:
    """Пустой диспетчер создаётся без открытия БД и Telegram."""
    app = _app()
    dispatcher = app.create_dispatcher(_config(), Mock(), routers=[])
    try:
        assert isinstance(dispatcher.storage, MemoryStorage)
        assert dispatcher.sub_routers == []
    finally:
        asyncio.run(dispatcher.storage.close())


def test_service_chat_is_optional(fake_environment) -> None:
    """Отсутствующая служебная группа не мешает приложению."""
    app = _app()
    bot = SimpleNamespace(send_message=AsyncMock())
    config = _config()
    config.notify_service_chat = True

    asyncio.run(app.notify_service(bot, config, "synthetic"))

    bot.send_message.assert_not_awaited()


def test_service_notification_failure_is_nonfatal(fake_environment) -> None:
    """Ошибка необязательного уведомления не прерывает запуск."""
    app = _app()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline")))
    config = _config()
    config.notify_service_chat = True
    config.tg_bot.home_group = [-123]

    asyncio.run(app.notify_service(bot, config, "synthetic"))

    bot.send_message.assert_awaited_once()


def test_bot_session_closes_if_engine_creation_fails(
    monkeypatch: pytest.MonkeyPatch, fake_environment
) -> None:
    """Самая ранняя ошибка не оставляет клиент Telegram незакрытым."""
    app = _app()
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(app, "create_bot", lambda config: bot)
    monkeypatch.setattr(
        app, "create_async_engine", Mock(side_effect=RuntimeError("engine"))
    )

    with pytest.raises(RuntimeError, match="engine"):
        asyncio.run(app.main(_config()))

    bot.session.close.assert_awaited_once()


def test_all_resources_close_if_schema_check_fails(
    monkeypatch: pytest.MonkeyPatch, fake_environment
) -> None:
    """Ошибка ранней работы с БД закрывает хранилище, движок и клиента."""
    app = _app()
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    context = SimpleNamespace()
    database = AsyncMock()
    database.__aenter__.side_effect = RuntimeError("schema")
    engine = SimpleNamespace(begin=lambda: database, dispose=AsyncMock())
    storage = SimpleNamespace(close=AsyncMock())
    dispatcher = SimpleNamespace(storage=storage)
    monkeypatch.setattr(app, "create_bot", lambda config: bot)
    monkeypatch.setattr(app, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(app, "async_sessionmaker", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        app, "create_dispatcher", lambda *args, **kwargs: dispatcher
    )

    with pytest.raises(RuntimeError, match="schema"):
        asyncio.run(app.main(_config()))

    storage.close.assert_awaited_once()
    engine.dispose.assert_awaited_once()
    bot.session.close.assert_awaited_once()
