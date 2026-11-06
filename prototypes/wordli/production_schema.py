"""Проверка и явное создание только таблиц Wordli без данных бота."""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import dotenv_values
from sqlalchemy import BigInteger, JSON, String, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .storage import wordli_metadata


class WordliSchemaError(RuntimeError):
    """Несовместимая схема или лишние полномочия игрового процесса."""


def _check_tables(connection: Connection, *, require_all: bool) -> None:
    """Проверяет существующие таблицы, не исправляя их структуру молча."""
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    for table in wordli_metadata.sorted_tables:
        if table.name not in actual_tables:
            if require_all:
                raise WordliSchemaError(f"Не создана таблица {table.name}")
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(columns) != set(table.columns.keys()):
            raise WordliSchemaError(f"Неверные колонки таблицы {table.name}")
        for expected in table.columns:
            actual = columns[expected.name]
            actual_type = actual["type"]
            if isinstance(expected.type, BigInteger):
                compatible = isinstance(actual_type, BigInteger)
            elif isinstance(expected.type, JSON):
                compatible = isinstance(actual_type, JSON)
            elif isinstance(expected.type, String):
                compatible = (
                    isinstance(actual_type, String)
                    and actual_type.length == expected.type.length
                )
            else:
                compatible = False
            if not compatible or actual["nullable"] != expected.nullable:
                raise WordliSchemaError(
                    f"Неверный тип или nullable: {table.name}.{expected.name}"
                )
        actual_primary = inspector.get_pk_constraint(table.name)["constrained_columns"]
        if actual_primary != list(table.primary_key.columns.keys()):
            raise WordliSchemaError(f"Неверный первичный ключ {table.name}")
        actual_foreign = {
            (
                tuple(key["constrained_columns"]),
                key["referred_table"],
                tuple(key["referred_columns"]),
                key.get("referred_schema"),
            )
            for key in inspector.get_foreign_keys(table.name)
        }
        expected_foreign = {
            (
                tuple(element.parent.name for element in key.elements),
                key.referred_table.name,
                tuple(element.column.name for element in key.elements),
                key.referred_table.schema,
            )
            for key in table.foreign_key_constraints
        }
        if actual_foreign != expected_foreign:
            raise WordliSchemaError(f"Неверные внешние ключи {table.name}")


async def assert_schema(engine: AsyncEngine) -> None:
    """Проверяет готовую схему без DDL и изменения записей."""
    async with engine.connect() as connection:
        await connection.run_sync(_check_tables, require_all=True)


async def create_schema(engine: AsyncEngine) -> None:
    """Явно создаёт недостающие таблицы Wordli, не меняя существующие."""
    async with engine.begin() as connection:
        await connection.run_sync(_check_tables, require_all=False)
        await connection.run_sync(wordli_metadata.create_all)
        await connection.run_sync(_check_tables, require_all=True)


async def assert_runtime_permissions(engine: AsyncEngine) -> None:
    """Не допускает административную роль и доступ к посторонним таблицам."""
    if engine.dialect.name != "postgresql":
        raise WordliSchemaError("Рабочая игра требует PostgreSQL")
    async with engine.connect() as connection:
        role = (
            await connection.execute(
                text(
                    "select rolsuper, rolcreatedb, rolcreaterole, "
                    "rolreplication, rolbypassrls from pg_roles "
                    "where rolname = current_user"
                )
            )
        ).one()
        if any(role):
            raise WordliSchemaError("Игровой пользователь имеет административные права")
        can_create = await connection.scalar(
            text(
                "select has_database_privilege(current_database(), 'CREATE') "
                "or exists (select 1 from pg_namespace "
                "where nspname not like 'pg_%' and nspname <> 'information_schema' "
                "and has_schema_privilege(oid, 'CREATE'))"
            )
        )
        if can_create:
            raise WordliSchemaError("Игровому пользователю запрещён постоянный DDL")
        for table in wordli_metadata.sorted_tables:
            allowed = await connection.scalar(
                text(
                    "select has_table_privilege(:name, 'SELECT') "
                    "and has_table_privilege(:name, 'INSERT') "
                    "and has_table_privilege(:name, 'UPDATE') "
                    "and not has_table_privilege(:name, 'DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"name": table.name},
            )
            if not allowed:
                raise WordliSchemaError(f"Неверные права таблицы {table.name}")
        other_access = await connection.scalar(
            text(
                "select exists (select 1 from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname not like 'pg_%' "
                "and n.nspname <> 'information_schema' "
                "and c.relkind in ('r','p','v','m','f') "
                "and c.oid not in (cast('wordli_profiles' as regclass), "
                "cast('wordli_requests' as regclass)) "
                "and has_table_privilege(c.oid, "
                "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))"
            )
        )
        if other_access:
            raise WordliSchemaError("Игровому пользователю доступны посторонние таблицы")


async def _run(database_url: str, *, apply: bool) -> None:
    """Выполняет выбранный оператором режим и закрывает соединения."""
    engine = create_async_engine(database_url)
    try:
        if apply:
            await create_schema(engine)
        else:
            await assert_schema(engine)
    finally:
        await engine.dispose()


def main() -> None:
    """Не выполняет миграцию без отдельного явного параметра --apply."""
    parser = argparse.ArgumentParser(description="Схема рабочей Wordli")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file")
    args = parser.parse_args()
    values = dict(dotenv_values(args.env_file)) if args.env_file else {}
    values.update(os.environ)
    if values.get("APP_ENV") != "production":
        parser.error("Нужен явный APP_ENV=production")
    database_url = values.get("WORDLI_MIGRATION_DATABASE_URL", "")
    try:
        address = make_url(database_url)
        if (
            address.drivername != "postgresql+asyncpg"
            or not address.username
            or not address.host
            or not address.database
        ):
            raise ValueError("Нужна явная PostgreSQL-база миграции")
    except (ArgumentError, ValueError):
        parser.error("Нужен WORDLI_MIGRATION_DATABASE_URL с драйвером postgresql+asyncpg")
    asyncio.run(_run(database_url, apply=args.apply))
    print("Схема Wordli готова; данные бота не изменены.")


if __name__ == "__main__":
    main()
