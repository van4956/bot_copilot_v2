"""Рабочий HTTP-сервер КВОРДЛИ без polling, автосоздания БД и демоданных."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

import asyncpg
from aiogram import Bot
from aiohttp import web

from prototypes.wordli.definitions import StaticDefinitionCatalog
from prototypes.wordli.telegram_auth import MAX_TELEGRAM_USER_ID

from .engine import QuordleEngine
from .server import create_app, initialise_connection, notify_players
from .storage import Storage

DATABASE = "copilot_quordle"
COLUMNS = {
    "user_id": "bigint", "display_name": "text", "locale": "text",
    "game_data": "jsonb", "revision": "bigint", "rating": "integer",
    "played": "integer", "reminders_enabled": "boolean",
    "timezone_name": "text", "reminded_on": "date",
}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProductionConfig:
    """Хранит явно заданные настройки рабочего приложения."""

    origin: str
    webapp_url: str
    database_url: str = field(repr=False)
    token: str = field(repr=False)
    expected_id: int
    expected_username: str
    reminders_enabled: bool
    port: int


def _origin(raw: str) -> str:
    """Принимает только постоянный HTTPS-origin без пути и реквизитов."""
    u = urlsplit(raw)
    host = u.hostname or ""
    if (
        u.scheme != "https" or u.path not in {"", "/"} or u.query or u.fragment
        or u.username is not None or u.password is not None
        or u.port not in {None, 443} or not host or "." not in host
        or host == "trycloudflare.com" or host.endswith(".trycloudflare.com")
        or not re.fullmatch(r"[a-z0-9.-]+", host)
        or any(not part or part.startswith("-") or part.endswith("-")
               for part in host.split("."))
        or not re.search(r"[a-z]", host.split(".")[-1])
        or raw.strip() != raw
    ):
        raise ValueError("КВОРДЛИ требует постоянный публичный HTTPS-origin.")
    return f"https://{host}"


def _database_url(dsn: str) -> str:
    """Проверяет выделенную рабочую БД и запрет параметров подмены DSN."""
    try:
        u = urlsplit(dsn)
        query = (
            parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
            if u.query else []
        )
        valid = (
            u.scheme in {"postgres", "postgresql"} and bool(u.hostname)
            and bool(u.username) and bool(u.password)
            and u.username not in {"postgres", "quordle_test"}
            and u.path == f"/{DATABASE}" and not u.fragment
            and (u.port is None or 0 < u.port <= 65535)
            and (not query or (
                len(query) == 1 and query[0][0] == "sslmode"
                and query[0][1] in {
                    "disable", "allow", "prefer", "require",
                    "verify-ca", "verify-full",
                }
            ))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Нужен DSN отдельной рабочей БД КВОРДЛИ.")
    return dsn


def load_config(environ: Mapping[str, str] | None = None) -> ProductionConfig:
    """Читает окружение контейнера, не загружая файлы или тестовые значения."""
    env = os.environ if environ is None else environ
    if env.get("APP_ENV") != "production":
        raise ValueError("Нужен APP_ENV=production.")
    origin = _origin(env.get("QUORDLE_PUBLIC_ORIGIN", ""))
    link = env.get("QUORDLE_WEBAPP_URL", origin + "/quordle/")
    if not link.startswith(origin + "/") or not re.fullmatch(
        r"(?:/[a-zA-Z0-9_-]+)+/?", link[len(origin):]
    ):
        raise ValueError("URL игры должен вести в подкаталог её origin.")
    token = env.get("BOT_TOKEN", "")
    identifier = env.get("QUORDLE_EXPECTED_BOT_ID", "")
    username = env.get("QUORDLE_EXPECTED_BOT_USERNAME", "").removeprefix("@")
    if (
        not re.fullmatch(r"[1-9][0-9]{0,15}", identifier)
        or int(identifier) > MAX_TELEGRAM_USER_ID
        or not re.fullmatch(re.escape(identifier) + r":[A-Za-z0-9_-]+", token)
        or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username)
        or username.lower() == "tg_template_test_bot"
    ):
        raise ValueError("Не подтверждены реквизиты основного Telegram-бота.")
    reminders = env.get("QUORDLE_REMINDERS_ENABLED", "false").lower()
    port = env.get("QUORDLE_PORT", "8795")
    if reminders not in {"true", "false"}:
        raise ValueError("QUORDLE_REMINDERS_ENABLED требует true или false.")
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port) or int(port) > 65535:
        raise ValueError("Неверный порт рабочего HTTP-сервера.")
    return ProductionConfig(
        origin=origin, webapp_url=link.rstrip("/") + "/",
        database_url=_database_url(env.get("QUORDLE_DATABASE_URL", "")),
        token=token, expected_id=int(identifier), expected_username=username,
        reminders_enabled=reminders == "true", port=int(port),
    )


async def check_database(pool: asyncpg.Pool) -> None:
    """Только читает каталоги PostgreSQL и проверяет рабочие права и схему."""
    async with pool.acquire() as conn:
        if await conn.fetchval("select current_database()") != DATABASE:
            raise RuntimeError("Подключена другая БД вместо copilot_quordle.")
        if not await conn.fetchval(
            """
            select not (rolsuper or rolcreatedb or rolcreaterole or rolreplication
                or rolbypassrls or has_database_privilege(current_database(), 'CREATE')
                or has_schema_privilege('public', 'CREATE'))
            from pg_catalog.pg_roles where rolname = current_user
            """
        ):
            raise RuntimeError("Игровая роль имеет избыточные права.")
        rows = await conn.fetch(
            """
            select column_name, data_type from information_schema.columns
            where table_schema = 'public' and table_name = 'quordle_players'
            """
        )
        if {r["column_name"]: r["data_type"] for r in rows} != COLUMNS:
            raise RuntimeError("Схема КВОРДЛИ не совпадает с миграцией.")
        if not await conn.fetchval(
            """
            select has_table_privilege('public.quordle_players', 'SELECT')
                and has_table_privilege('public.quordle_players', 'INSERT')
                and has_table_privilege('public.quordle_players', 'UPDATE')
                and not has_table_privilege('public.quordle_players', 'DELETE')
                and not has_table_privilege('public.quordle_players', 'TRUNCATE')
                and exists (
                    select 1 from pg_catalog.pg_constraint
                    where conrelid = 'public.quordle_players'::regclass
                        and contype = 'p'
                        and pg_get_constraintdef(oid) = 'PRIMARY KEY (user_id)'
                ) and exists (
                    select 1 from pg_catalog.pg_index
                    where indrelid = 'public.quordle_players'::regclass
                        and indexrelid = to_regclass('public.quordle_rating_idx')
                        and indisvalid and indisready
                )
            """
        ):
            raise RuntimeError("Не подтверждены права или индексы КВОРДЛИ.")


async def verify_bot(bot: Bot, config: ProductionConfig) -> None:
    """Сверяет настоящий ID и username бота до открытия HTTP-порта."""
    identity = await bot.get_me(request_timeout=10)
    if (
        not identity.is_bot or identity.id != config.expected_id
        or (identity.username or "").casefold() != config.expected_username.casefold()
    ):
        raise RuntimeError("Telegram вернул другую личность бота.")


async def create_production_app(config: ProductionConfig) -> web.Application:
    """Готовит API и управляет ресурсами на жизненном цикле aiohttp."""
    pool = asyncpg.create_pool(
        dsn=config.database_url, init=initialise_connection,
        server_settings={"search_path": "pg_catalog,public,pg_temp"},
        min_size=1, max_size=5, timeout=8, command_timeout=15,
    )
    storage = Storage(pool, QuordleEngine())
    app = create_app(
        storage, StaticDefinitionCatalog(), origin=config.origin,
        token=config.token, production=True,
    )

    async def resources(_: web.Application) -> AsyncIterator[None]:
        """Не допускает запуск с неподтверждённой БД либо Telegram-ботом."""
        async with pool, Bot(token=config.token) as bot:
            await check_database(pool)
            await verify_bot(bot, config)
            worker = None
            try:
                if config.reminders_enabled:
                    worker = asyncio.create_task(
                        notify_players(storage, bot, config.webapp_url)
                    )
                yield
            finally:
                if worker:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker

    app.cleanup_ctx.append(resources)
    return app


def main() -> int:
    """Запускает только рабочий API, не выводя реквизиты при ошибках."""
    try:
        config = load_config()
        web.run_app(
            create_production_app(config), host="0.0.0.0", port=config.port,
            print=None, access_log=None,
        )
    except Exception as error:
        LOGGER.error("КВОРДЛИ не запущена: %s.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
