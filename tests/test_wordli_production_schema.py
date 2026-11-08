"""Проверки миграции Wordli только во временной SQLite и заглушках."""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from prototypes.wordli.production_schema import (
    WordliSchemaError,
    assert_runtime_permissions,
    assert_schema,
    create_schema,
    main,
)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Предоставляет отдельную пустую временную БД без настроек проекта."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema.sqlite3'}")
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_runtime_schema_check_does_not_create_missing_tables(database) -> None:
    """Неподготовленная схема не исправляется игровым процессом."""
    statements = []
    event.listen(
        database.sync_engine,
        "before_cursor_execute",
        lambda _, __, statement, *args: statements.append(statement.lower()),
    )
    with pytest.raises(WordliSchemaError, match="wordli_profiles"):
        await assert_schema(database)
    assert not any(statement.lstrip().startswith("create") for statement in statements)
    async with database.connect() as connection:
        assert await connection.run_sync(lambda conn: inspect(conn).get_table_names()) == []


async def test_explicit_migration_is_idempotent_and_preserves_other_tables(database) -> None:
    """Создаются только игровые таблицы, данные других функций не меняются."""
    async with database.begin() as connection:
        await connection.exec_driver_sql("create table other_data (value integer not null)")
        await connection.exec_driver_sql("insert into other_data values (42)")
    await create_schema(database)
    async with database.begin() as connection:
        await connection.exec_driver_sql(
            "insert into wordli_profiles values (900001, '[]', 7, 'synthetic-v1')"
        )
    await create_schema(database)
    await assert_schema(database)
    async with database.connect() as connection:
        names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(names) == {
            "other_data", "wordli_profiles", "wordli_requests", "wordli_settings"
        }
        assert (await connection.exec_driver_sql("select value from other_data")).scalar() == 42
        assert (
            await connection.exec_driver_sql("select revision from wordli_profiles")
        ).scalar() == 7


@pytest.mark.parametrize(
    "definition",
    [
        "user_id bigint primary key, history json not null, revision bigint not null",
        "user_id integer primary key, history json not null, revision bigint not null, dictionary_id varchar(128) not null",
        "user_id bigint primary key, history json, revision bigint not null, dictionary_id varchar(128) not null",
        "user_id bigint not null, history json not null, revision bigint not null, dictionary_id varchar(128) not null",
    ],
)
async def test_incompatible_existing_profile_is_not_altered(database, definition: str) -> None:
    """Ошибочные колонки, типы, nullable и ключи требуют решения оператора."""
    async with database.begin() as connection:
        await connection.exec_driver_sql(f"create table wordli_profiles ({definition})")
    with pytest.raises(WordliSchemaError):
        await create_schema(database)
    async with database.connect() as connection:
        assert await connection.run_sync(lambda conn: inspect(conn).get_table_names()) == [
            "wordli_profiles"
        ]


async def test_missing_request_foreign_key_is_rejected(database) -> None:
    """Повреждённая связь журнала не признаётся рабочей схемой."""
    await create_schema(database)
    async with database.begin() as connection:
        await connection.exec_driver_sql("drop table wordli_requests")
        await connection.exec_driver_sql(
            "create table wordli_requests (user_id bigint not null, "
            "request_id varchar(36) not null, fingerprint varchar(64) not null, "
            "response json not null, primary key (user_id, request_id))"
        )
    with pytest.raises(WordliSchemaError, match="внешние ключи"):
        await assert_schema(database)


async def test_runtime_permissions_reject_sqlite(database) -> None:
    """Рабочая проверка прав не позволяет подставить тестовый SQLite."""
    with pytest.raises(WordliSchemaError, match="PostgreSQL"):
        await assert_runtime_permissions(database)


def _permissions_engine(role: tuple, scalar_values: list) -> SimpleNamespace:
    """Создаёт заглушку PostgreSQL без сети и настоящих адресов."""
    connection = AsyncMock()
    connection.execute.return_value = SimpleNamespace(one=lambda: role)
    connection.scalar.side_effect = scalar_values
    context = AsyncMock()
    context.__aenter__.return_value = connection
    return SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), connect=lambda: context
    )


@pytest.mark.parametrize("index", range(5))
async def test_runtime_rejects_each_administrative_role_flag(index: int) -> None:
    """Суперправа, создание БД/ролей, репликация и обход RLS недопустимы."""
    role = [False] * 5
    role[index] = True
    with pytest.raises(WordliSchemaError, match="административные"):
        await assert_runtime_permissions(_permissions_engine(tuple(role), []))


@pytest.mark.parametrize(
    "values,match",
    [
        ([True], "DDL"),
        ([False, False], "права таблицы"),
        ([False, True, False], "права таблицы"),
        ([False, True, True, False], "права таблицы"),
        ([False, True, True, True, True], "посторонние"),
    ],
)
async def test_runtime_rejects_excess_or_missing_privileges(values: list, match: str) -> None:
    """Разрешены только чтение и запись игровых таблиц."""
    with pytest.raises(WordliSchemaError, match=match):
        await assert_runtime_permissions(_permissions_engine((False,) * 5, values))


async def test_runtime_accepts_only_limited_wordli_permissions() -> None:
    """Минимальные права Wordli достаточны для запуска."""
    await assert_runtime_permissions(
        _permissions_engine((False,) * 5, [False, True, True, True, False])
    )


@pytest.mark.parametrize("url", ["", "invalid", "sqlite:///test.db", "postgresql+asyncpg:///"])
def test_cli_rejects_ambiguous_or_non_postgres_target(monkeypatch, url: str) -> None:
    """CLI не выбирает БД автоматически и не подключается при ошибке URL."""
    monkeypatch.setattr("sys.argv", ["production_schema"])
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WORDLI_MIGRATION_DATABASE_URL", url)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2


def test_cli_refuses_non_production_mode(monkeypatch) -> None:
    """Оператор должен отдельно выбрать рабочий режим миграции."""
    monkeypatch.setattr("sys.argv", ["production_schema", "--apply"])
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
