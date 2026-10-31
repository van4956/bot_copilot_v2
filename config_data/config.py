"""Явные настройки боевого и изолированного тестового запуска."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_WEBAPP_URL = "https://van4956.github.io/bot_copilot_v2"


@dataclass
class TgBot:
    """Настройки Telegram без секретов в строковом представлении."""

    token: str = field(repr=False)
    token_test: str = field(repr=False)
    owner: list[int] = field(default_factory=list)
    admin_list: list[int] = field(default_factory=list)
    home_group: list[int] = field(default_factory=list)
    work_group: list[int] = field(default_factory=list)
    api_gpt: str = field(default="", repr=False)
    api_weather: str = field(default="", repr=False)
    api_currency: str = field(default="", repr=False)


@dataclass
class DataBase:
    """Адреса баз данных."""

    db_lite: str = field(default="", repr=False)
    db_post: str = field(default="", repr=False)


@dataclass
class Redis:
    """Подключение и пространство имён Redis."""

    host: str = "localhost"
    port: int = 6379
    url: str = field(default="", repr=False)
    prefix: str = "fsm"


@dataclass
class Config:
    """Полная конфигурация приложения."""

    tg_bot: TgBot
    db: DataBase
    redis: Redis
    mode: str = "test"
    database_url: str = field(default="", repr=False)
    create_schema: bool = False
    notify_service_chat: bool = False
    drop_pending_updates: bool = False
    webapp_base_url: str = ""
    expected_test_bot_id: int | None = None

    @property
    def test_mode(self) -> bool:
        """Проверяет выбор тестового режима."""
        return self.mode == "test"


def validate_test_database(url: str) -> None:
    """Запрещает заполнение и запуск тестов на посторонней базе."""
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite"):
        if parsed.database == ":memory:":
            return
        path = Path(parsed.database or "").resolve()
        if ROOT / ".test-data" in path.parents:
            return
    elif (
        parsed.drivername == "postgresql+asyncpg"
        and parsed.host in {"127.0.0.1", "localhost", "db-test"}
        and parsed.database == "copilot_review_test"
    ):
        return
    raise ValueError("Тестовая база должна быть отдельной локальной базой review")


def _ids(value: str) -> list[int]:
    """Разбирает список идентификаторов."""
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    """Разбирает явное логическое значение."""
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError("Ожидается true или false")
    return value.lower() in {"true", "1"}


def load_config(path: str | None = None) -> Config:
    """Читает выбранный файл, не меняя окружение и не загружая боевой по умолчанию."""
    mode = os.getenv("APP_ENV", "test").lower()
    if mode not in {"test", "production"}:
        raise ValueError("APP_ENV должен быть test или production")
    filename = path or os.getenv("APP_ENV_FILE")
    filename = filename or str(ROOT / (".env.test.local" if mode == "test" else ".env"))
    values = {
        key: value
        for key, value in dotenv_values(filename).items()
        if value is not None
    }
    if values.get("APP_ENV", mode).lower() != mode:
        raise ValueError("APP_ENV в файле и окружении не совпадает")
    values.update(os.environ)
    test = mode == "test"
    token_name = "BOT_TOKEN_TEST" if test else "BOT_TOKEN"
    token = values.get(token_name, "")
    if not token:
        raise ValueError(f"Не задан {token_name}; бот не запущен")
    database_url = (
        values.get(
            "TEST_DATABASE_URL",
            f"sqlite+aiosqlite:///{ROOT / '.test-data' / 'copilot_review.sqlite3'}",
        )
        if test
        else values.get("DB_POST", "")
    )
    if test:
        validate_test_database(database_url)
    elif not database_url:
        raise ValueError("Не задан DB_POST")
    redis_url = (
        values.get("TEST_REDIS_URL", "") if test else values.get("REDIS_URL", "")
    )
    host = values.get("REDIS_HOST", "redis")
    port = int(values.get("REDIS_PORT", "6379"))
    if not test and not redis_url:
        redis_url = f"redis://{host}:{port}/0"
    if test and redis_url:
        address = urlsplit(redis_url)
        if (
            address.scheme not in {"redis", "rediss"}
            or address.hostname not in {"127.0.0.1", "localhost", "redis-test"}
            or address.path != "/15"
        ):
            raise ValueError("Тестовый Redis должен быть отдельным локальным, база 15")
    webapp = values.get(
        "WEBAPP_BASE_URL", "" if test else PRODUCTION_WEBAPP_URL
    ).rstrip("/")
    if webapp and not webapp.startswith("https://"):
        raise ValueError("Мини-приложения требуют HTTPS")
    if test and webapp.startswith(PRODUCTION_WEBAPP_URL):
        raise ValueError("Тестовый бот не должен открывать боевые мини-приложения")
    expected_id = values.get("EXPECTED_TEST_BOT_ID", "")
    return Config(
        tg_bot=TgBot(
            token="" if test else token,
            token_test=token if test else "",
            owner=_ids(values.get("OWNER", "")),
            admin_list=_ids(values.get("ADMIN_LIST", "")),
            home_group=[] if test else _ids(values.get("HOME_GROUP", "")),
            work_group=[] if test else _ids(values.get("WORK_GROUP", "")),
            api_gpt=values.get("API_GPT", ""),
            api_weather=values.get("API_WEATHER", ""),
            api_currency=values.get("API_CURRENCY", ""),
        ),
        db=DataBase(db_lite=database_url if test else "", db_post=database_url),
        redis=Redis(
            host=host,
            port=port,
            url=redis_url,
            prefix="copilot-review-test" if test else "fsm",
        ),
        mode=mode,
        database_url=database_url,
        create_schema=_bool(
            values.get("AUTO_CREATE_SCHEMA", "true" if test else "false")
        ),
        notify_service_chat=not test
        and _bool(values.get("NOTIFY_SERVICE_CHAT", "true")),
        drop_pending_updates=_bool(values.get("DROP_PENDING_UPDATES", "false")),
        webapp_base_url=webapp,
        expected_test_bot_id=int(expected_id) if expected_id else None,
    )
