"""Проверяет SQL-миграцию в отдельной схеме локального тестового PostgreSQL."""

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config_data.config import validate_test_database
from database.models import Games, Payments
from database.orm_payments import (
    orm_create_invoice,
    orm_get_payment,
    orm_mark_refunded,
    orm_record_payment,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
    / "001_review_payments.sql"
)
SCHEMA_PATTERN = re.compile(r"review_migration_[0-9a-f]{32}\Z")
TEST_USER_ID = 9_100_000_001

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Отдельный тестовый PostgreSQL не запущен",
    ),
]


async def _public_tables_state(connection: asyncpg.Connection) -> tuple:
    """Читает признаки public-таблиц, которые тест не должен изменять."""
    row = await connection.fetchrow(
        "select to_regclass('public.users')::oid, "
        "to_regclass('public.games')::oid, "
        "(select attnotnull from pg_attribute "
        "where attrelid = to_regclass('public.games') "
        "and attname = 'user_name' and not attisdropped)"
    )
    assert row is not None
    return tuple(row)


@pytest_asyncio.fixture
async def migration_schema() -> AsyncIterator[tuple[asyncpg.Connection, str, str]]:
    """Выделяет и удаляет только собственную схему с уникальным UUID."""
    database_url = os.environ["TEST_DATABASE_URL"]
    validate_test_database(database_url)
    address = make_url(database_url)
    assert address.drivername == "postgresql+asyncpg"
    assert address.database == "copilot_review_test"
    assert address.username == "bot_app"
    assert os.getenv("APP_ENV") == "test"
    schema = f"review_migration_{uuid4().hex}"
    assert SCHEMA_PATTERN.fullmatch(schema)
    connection = await asyncpg.connect(
        host=address.host,
        port=address.port or 5432,
        user=address.username,
        password=address.password,
        database=address.database,
    )
    public_before = None
    try:
        assert await connection.fetchval("select current_user") == "bot_app"
        public_before = await _public_tables_state(connection)
        assert all(value is not None for value in public_before), (
            "Перед тестом нужны public.users/public.games, созданные "
            "синтетическим seed выделенной тестовой БД"
        )
        await connection.execute(f'create schema "{schema}" authorization bot_app')
        # Не включаем public: ошибка поиска games не должна затронуть public.games.
        await connection.execute(f'set search_path to "{schema}"')
        assert await connection.fetchval("select current_schema()") == schema
        yield connection, schema, database_url
    finally:
        try:
            if connection.is_in_transaction():
                await connection.execute("rollback")
            assert SCHEMA_PATTERN.fullmatch(schema)
            owner = await connection.fetchval(
                "select pg_get_userbyid(nspowner) from pg_namespace where nspname = $1",
                schema,
            )
            if owner is not None:
                assert owner == "bot_app"
                await connection.execute(f'drop schema "{schema}" cascade')
            if public_before is not None:
                assert await _public_tables_state(connection) == public_before
        finally:
            await connection.close()


async def _create_legacy_tables(
    connection: asyncpg.Connection, *, include_games: bool = True
) -> None:
    """Создаёт минимальную старую схему и искусственные записи в ней."""
    await connection.execute(
        "create table users ("
        "user_id bigint primary key, user_name varchar(150) not null, "
        "full_name varchar(150) not null, locale varchar(150) not null, "
        "status varchar(150) not null, flag integer not null, "
        "created timestamp not null default now(), "
        "updated timestamp not null default now()); "
        "insert into users "
        "(user_id, user_name, full_name, locale, status, flag) "
        f"values ({TEST_USER_ID}, 'migration_user', 'Тест миграции', "
        "'en', 'member', 1)"
    )
    if include_games:
        await connection.execute(
            "create table games ("
            "game_id serial primary key, game_name varchar(150) not null, "
            "user_id bigint not null, user_name varchar(150) not null, "
            "score integer not null, "
            "created timestamp not null default now(), "
            "updated timestamp not null default now()); "
            "insert into games (game_name, user_id, user_name, score) "
            f"values ('snake', {TEST_USER_ID}, 'migration_user', 42)"
        )


async def test_migration_preserves_data_and_supports_payment_orm(
    migration_schema: tuple[asyncpg.Connection, str, str],
) -> None:
    """Применяет исходный SQL от bot_app и проверяет новую схему через ORM."""
    connection, schema, database_url = migration_schema
    await _create_legacy_tables(connection)
    users_before = await connection.fetch("select * from users order by user_id")
    games_before = await connection.fetch("select * from games order by game_id")

    await connection.execute(MIGRATION_PATH.read_text(encoding="utf-8"))

    assert (
        await connection.fetch("select * from users order by user_id") == users_before
    )
    assert (
        await connection.fetch("select * from games order by game_id") == games_before
    )
    assert (
        await connection.fetchval(
            "select is_nullable from information_schema.columns "
            "where table_schema = $1 and table_name = 'games' "
            "and column_name = 'user_name'",
            schema,
        )
        == "YES"
    )
    owners = await connection.fetch(
        "select relname, pg_get_userbyid(relowner) as owner "
        "from pg_class join pg_namespace on pg_namespace.oid = relnamespace "
        "where nspname = $1 and relkind in ('r', 'S')",
        schema,
    )
    assert {row["relname"] for row in owners} >= {
        "users",
        "games",
        "payments",
        "payments_payment_id_seq",
    }
    assert all(row["owner"] == "bot_app" for row in owners)

    engine = create_async_engine(
        database_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    charge_id = f"review-charge-{uuid4().hex}"
    try:
        async with sessions() as session:
            invoice = await orm_create_invoice(session, TEST_USER_ID, 25)
            payload = invoice.invoice_payload
            payment, created = await orm_record_payment(
                session, payload, TEST_USER_ID, "XTR", 25, charge_id
            )
            assert created
            assert payment.status == "paid"
            assert payment.paid_at is not None and payment.paid_at.tzinfo is not None
            _, repeated = await orm_record_payment(
                session, payload, TEST_USER_ID, "XTR", 25, charge_id
            )
            assert not repeated
            await orm_mark_refunded(session, charge_id)
            refunded = await orm_get_payment(session, charge_id)
            assert refunded is not None and refunded.status == "refunded"
            assert refunded.refunded_at is not None
            assert refunded.refunded_at.tzinfo is not None

            session.add(
                Games(game_name="snake", user_id=TEST_USER_ID, user_name=None, score=7)
            )
            await session.commit()

        async with sessions() as session:
            session.add(
                Payments(
                    invoice_payload=payload,
                    user_id=TEST_USER_ID,
                    currency="XTR",
                    total_amount=25,
                    status="pending",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        async with sessions() as session:
            session.add(
                Payments(
                    invoice_payload=f"review-other-{uuid4().hex}",
                    user_id=TEST_USER_ID,
                    currency="XTR",
                    total_amount=25,
                    status="paid",
                    telegram_payment_charge_id=charge_id,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
            assert len(list(await session.scalars(select(Payments)))) == 1
    finally:
        await engine.dispose()


async def test_migration_rolls_back_when_legacy_games_are_missing(
    migration_schema: tuple[asyncpg.Connection, str, str],
) -> None:
    """Проверяет откат созданной payments при ошибке следующего ALTER TABLE."""
    connection, schema, _ = migration_schema
    await _create_legacy_tables(connection, include_games=False)
    users_before = await connection.fetch("select * from users order by user_id")

    with pytest.raises(asyncpg.UndefinedTableError):
        await connection.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
    assert connection.is_in_transaction()
    await connection.execute("rollback")

    assert (
        await connection.fetchval("select to_regclass($1)::oid", f"{schema}.payments")
        is None
    )
    assert (
        await connection.fetch("select * from users order by user_id") == users_before
    )
    assert await connection.fetchval("select current_user") == "bot_app"
