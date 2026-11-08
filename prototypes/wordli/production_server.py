"""Рабочий HTTP-сервер игры без polling бота и автоматического изменения БД."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .engine import LocalGameEngine
from .production_schema import assert_runtime_permissions, assert_schema
from .reminders import parse_reminders_flag, reminder_service
from .storage import WordliStorage
from .telegram_auth import MAX_TELEGRAM_USER_ID
from .telegram_server import _origin, create_app


@dataclass(frozen=True, slots=True)
class ProductionConfig:
    """Содержит только необходимые серверу игры рабочие настройки."""

    public_origin: str
    database_url: str = field(repr=False)
    bot_token: str = field(repr=False)
    expected_bot_id: int
    access: str = "restricted"
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset, repr=False)
    reminders_enabled: bool = False


def load_production_config(
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> ProductionConfig:
    """Читает явные рабочие настройки без подстановки файлов тестового бота."""
    values: dict[str, str] = {}
    if env_file is not None:
        path = Path(env_file)
        if not path.is_file():
            raise ValueError("Файл настроек игрового сервера не найден")
        values.update(
            (key, value)
            for key, value in dotenv_values(path).items()
            if value is not None
        )
    values.update(os.environ if environ is None else environ)
    if values.get("APP_ENV") != "production":
        raise ValueError("Рабочий игровой сервер требует APP_ENV=production")
    origin = _origin(values.get("WORDLI_PUBLIC_ORIGIN", ""))
    hostname = (urlsplit(origin).hostname or "").lower().rstrip(".")
    if hostname == "trycloudflare.com" or hostname.endswith(".trycloudflare.com"):
        raise ValueError("Рабочей игре нужен постоянный HTTPS origin")
    token = values.get("WORDLI_BOT_TOKEN", "")
    identifier = values.get("WORDLI_EXPECTED_BOT_ID", "")
    if (
        not re.fullmatch(r"[1-9][0-9]{0,18}", identifier)
        or not re.fullmatch(r"[0-9]+:[^\s:]+", token)
        or int(token.partition(":")[0]) != int(identifier)
    ):
        raise ValueError("Не подтверждён WORDLI_EXPECTED_BOT_ID рабочего бота")
    database_url = values.get("WORDLI_DATABASE_URL", "")
    try:
        database = make_url(database_url)
        valid_database = (
            database.drivername == "postgresql+asyncpg"
            and bool(database.host)
            and bool(database.username)
            and database.username != "postgres"
            and bool(database.password)
            and bool(database.database)
            and database.database != "copilot_review_test"
            and (database.port is None or 1 <= database.port <= 65535)
        )
    except (ArgumentError, TypeError, ValueError):
        valid_database = False
    if not valid_database:
        raise ValueError(
            "WORDLI_DATABASE_URL требует отдельную роль рабочей PostgreSQL через asyncpg"
        )
    access = values.get("WORDLI_ACCESS", "restricted")
    if access not in {"restricted", "all"}:
        raise ValueError("WORDLI_ACCESS должен быть restricted или all")
    allowed: frozenset[int] = frozenset()
    if access == "restricted":
        raw_ids = values.get("WORDLI_ALLOWED_USER_IDS", "")
        parts = [part.strip() for part in raw_ids.split(",")]
        if not parts or any(
            not re.fullmatch(r"[1-9][0-9]{0,15}", part)
            or int(part) > MAX_TELEGRAM_USER_ID
            for part in parts
        ):
            raise ValueError("Нужен непустой WORDLI_ALLOWED_USER_IDS для restricted")
        allowed = frozenset(map(int, parts))
    reminders_enabled = parse_reminders_flag(
        values.get("WORDLI_REMINDERS_ENABLED", "false")
    )
    return ProductionConfig(
        public_origin=origin,
        database_url=database_url,
        bot_token=token,
        expected_bot_id=int(identifier),
        access=access,
        allowed_user_ids=allowed,
        reminders_enabled=reminders_enabled,
    )


async def database_ready(database: AsyncEngine) -> bool:
    """Проверяет доступность БД без вывода адреса подключения и исключений."""
    async def check() -> None:
        """Выполняет только лёгкий запрос чтения."""
        async with database.connect() as connection:
            await connection.execute(text("select 1"))

    try:
        await asyncio.wait_for(check(), timeout=2.0)
    except Exception:
        return False
    return True


def create_production_app(config: ProductionConfig) -> web.Application:
    """Собирает HTTP-приложение; схему проверяет до открытия порта."""
    database = create_async_engine(config.database_url, pool_pre_ping=True)
    storage = WordliStorage(database, LocalGameEngine())

    async def ready() -> bool:
        """Возвращает готовность существующей рабочей БД."""
        return await database_ready(database)

    app = create_app(
        storage,
        config.bot_token,
        config.public_origin,
        None if config.access == "all" else set(config.allowed_user_ids),
        expected_bot_id=config.expected_bot_id,
        mode="production",
        readiness_check=ready,
    )

    async def resources(application: web.Application) -> AsyncIterator[None]:
        """Не создаёт таблицы и закрывает пул даже при отказе запуска."""
        try:
            await assert_schema(database)
            await assert_runtime_permissions(database)
            if config.reminders_enabled:
                async with reminder_service(
                    storage,
                    config.bot_token,
                    allowed_user_ids=(
                        None if config.access == "all" else config.allowed_user_ids
                    ),
                ):
                    yield
            else:
                yield
        finally:
            await database.dispose()

    app.cleanup_ctx.append(resources)
    return app


def main() -> None:
    """Запускает только HTTP API с явно рабочими переменными окружения."""
    parser = argparse.ArgumentParser(description="Рабочий сервер игры 5БУКВ")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()
    try:
        ipaddress.ip_address(args.host)
        if not 1 <= args.port <= 65535:
            raise ValueError("Порт должен быть в пределах 1–65535")
        config = load_production_config(env_file=args.env_file)
    except ValueError as error:
        parser.error(str(error))
    web.run_app(
        create_production_app(config),
        host=args.host,
        port=args.port,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
