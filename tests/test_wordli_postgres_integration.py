"""Проверки Wordli только в собственной схеме локальной review PostgreSQL."""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config_data.config import validate_test_database
from prototypes.wordli.engine import GameError, LocalGameEngine
from prototypes.wordli.production_schema import (
    WordliSchemaError,
    assert_runtime_permissions,
    assert_schema,
    create_schema,
)
from prototypes.wordli.storage import WordliStorage, profiles_table, requests_table

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Отдельный PostgreSQL Wordli не включён",
    ),
]
SCHEMA_PATTERN = re.compile(r"review_wordli_[0-9a-f]{32}\Z")
USER = 9_100_000_777
NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
WORDS = ["БИЛЕТ", "КОМАР", "КОШКА", "ЛАМПА", "ПАРОМ", "ТАЧКА"]


@pytest_asyncio.fixture
async def database() -> AsyncIterator[AsyncEngine]:
    """Создаёт и удаляет только собственную схему в copilot_review_test."""
    url = os.environ["TEST_DATABASE_URL"]
    validate_test_database(url)
    address = make_url(url)
    assert address.drivername == "postgresql+asyncpg"
    assert address.database == "copilot_review_test"
    assert address.username == "bot_app"
    assert os.environ.get("APP_ENV") == "test"
    schema = "review_wordli_" + uuid4().hex
    assert SCHEMA_PATTERN.fullmatch(schema)
    control = create_async_engine(url)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    public_query = text(
        "select c.oid, c.relname from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind in ('r','p') order by c.oid"
    )
    before = None
    try:
        async with control.begin() as connection:
            assert await connection.scalar(text("select current_user")) == "bot_app"
            before = list((await connection.execute(public_query)).all())
            await connection.execute(text(f'create schema "{schema}" authorization bot_app'))
        async with engine.connect() as connection:
            assert await connection.scalar(text("select current_schema()")) == schema
        yield engine
    finally:
        await engine.dispose()
        try:
            async with control.begin() as connection:
                owner = await connection.scalar(
                    text(
                        "select pg_get_userbyid(nspowner) from pg_namespace "
                        "where nspname = :name"
                    ),
                    {"name": schema},
                )
                if owner is not None:
                    assert owner == "bot_app" and SCHEMA_PATTERN.fullmatch(schema)
                    await connection.execute(text(f'drop schema "{schema}" cascade'))
                if before is not None:
                    assert list((await connection.execute(public_query)).all()) == before
        finally:
            await control.dispose()


@pytest.fixture
def rules(tmp_path: Path) -> LocalGameEngine:
    """Использует маленький искусственный словарь вместо рабочих ответов."""
    path = tmp_path / "words.json"
    path.write_text(
        json.dumps({
            "schema_version": 1, "dictionary_id": "postgres-fixture-v1",
            "normalization": "Ё=Е", "words": WORDS, "answers": WORDS,
        }, ensure_ascii=False), encoding="utf-8",
    )
    return LocalGameEngine(path)


async def _answer(database: AsyncEngine, state: dict) -> str:
    """Читает ответ только из выделенной тестовой схемы."""
    async with database.connect() as connection:
        history = await connection.scalar(
            select(profiles_table.c.history).where(profiles_table.c.user_id == USER)
        )
    return next(
        record["answer"] for record in history
        if record["day"] == state["day"] and record["index"] == state["game"]["index"]
    )


async def test_postgres_schema_migration_is_idempotent_and_keeps_other_data(database) -> None:
    """Одноразовая миграция не изменяет посторонние таблицы и записи."""
    with pytest.raises(WordliSchemaError, match="Не создана"):
        await assert_schema(database)
    async with database.begin() as connection:
        await connection.execute(text("create table unrelated (value integer not null)"))
        await connection.execute(text("insert into unrelated values (42)"))
    await create_schema(database)
    await create_schema(database)
    await assert_schema(database)
    async with database.connect() as connection:
        assert await connection.scalar(text("select value from unrelated")) == 42


async def test_postgres_concurrent_bootstrap_and_uuid_are_atomic(database, rules) -> None:
    """Два процесса видят одну выдачу, повтор UUID записывает один ход."""
    await create_schema(database)
    first = WordliStorage(database, rules, clock=lambda: NOW)
    second = WordliStorage(database, rules, clock=lambda: NOW)
    a, b = await asyncio.gather(first.bootstrap(USER), second.bootstrap(USER))
    assert a == b
    identifier = str(uuid4())
    arguments = (
        USER, a["day"], 0, await _answer(database, a), a["revision"], identifier,
    )
    a, b = await asyncio.gather(first.submit(*arguments), second.submit(*arguments))
    assert a == b and a["stats"]["wins"] == 1
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(requests_table)) == 1
        assert await connection.scalar(select(func.count()).select_from(profiles_table)) == 1


async def test_postgres_draft_conflict_and_restart_preserve_progress(database, rules) -> None:
    """Устаревшее устройство не перезаписывает принятый черновик."""
    await create_schema(database)
    first = WordliStorage(database, rules, clock=lambda: NOW)
    second = WordliStorage(database, rules, clock=lambda: NOW)
    original = await first.bootstrap(USER)
    saved = await first.set_draft(USER, original["day"], 0, original["revision"], "КО")
    with pytest.raises(GameError) as error:
        await second.set_draft(USER, original["day"], 0, original["revision"], "ТА")
    assert error.value.code == "version_conflict"
    restored = await WordliStorage(database, rules, clock=lambda: NOW).bootstrap(USER)
    assert restored == saved and restored["game"]["draft"] == "КО"


async def test_postgres_expired_words_do_not_repeat_or_create_losses(database, rules) -> None:
    """Просрочки резервируют все личные слова без изменения счёта побед."""
    await create_schema(database)
    seen = set()
    for offset in range(len(WORDS)):
        day = NOW + timedelta(days=offset)
        storage = WordliStorage(database, rules, clock=lambda day=day: day)
        state = await storage.bootstrap(USER)
        answer = await _answer(database, state)
        assert answer not in seen
        seen.add(answer)
        assert state["stats"]["played"] == 0 and "history" not in state
    storage = WordliStorage(database, rules, clock=lambda: NOW + timedelta(days=20))
    state = await storage.bootstrap(USER)
    assert state["game"]["status"] == "exhausted"
    assert state["stats"]["played"] == state["stats"]["wins"] == 0


async def test_postgres_rejects_ddl_capable_runtime_account(database) -> None:
    """Даже тестовая bot_app не подходит для рабочего игрового процесса."""
    await create_schema(database)
    with pytest.raises(WordliSchemaError, match="DDL"):
        await assert_runtime_permissions(database)


@pytest.mark.skipif(
    not os.getenv("WORDLI_TEST_ADMIN_DATABASE_URL"),
    reason="Не задан администратор отдельной review PostgreSQL",
)
async def test_postgres_separate_migration_and_restricted_runtime_roles(
    database, rules,
) -> None:
    """Оператор создаёт схему, а отдельная роль получает только игровой DML."""
    admin_url = os.environ["WORDLI_TEST_ADMIN_DATABASE_URL"]
    validate_test_database(admin_url)
    address = make_url(admin_url)
    assert address.database == "copilot_review_test"
    assert address.username == "review_admin"
    administrator = create_async_engine(admin_url)
    role = "wordli_test_" + uuid4().hex
    assert re.fullmatch(r"wordli_test_[0-9a-f]{32}", role)
    password = "SYNTHETIC_REVIEW_ROLE_ONLY_42"
    runtime = None
    created = False
    try:
        await create_schema(database)
        async with database.connect() as connection:
            schema = await connection.scalar(text("select current_schema()"))
        assert SCHEMA_PATTERN.fullmatch(schema)
        async with administrator.begin() as connection:
            await connection.execute(text(
                f'create role "{role}" login password \'{password}\' '
                "nosuperuser nocreatedb nocreaterole noreplication nobypassrls"
            ))
            created = True
            await connection.execute(text(
                f'grant connect on database copilot_review_test to "{role}"'
            ))
            await connection.execute(text(f'grant usage on schema "{schema}" to "{role}"'))
            await connection.execute(text(
                f'grant select, insert, update on table "{schema}".wordli_profiles, '
                f'"{schema}".wordli_requests to "{role}"'
            ))
        runtime = create_async_engine(
            address.set(username=role, password=password),
            connect_args={"server_settings": {"search_path": schema}},
        )
        await assert_schema(runtime)
        await assert_runtime_permissions(runtime)
        storage = WordliStorage(runtime, rules, clock=lambda: NOW)
        state = await storage.bootstrap(USER)
        assert state["stats"]["played"] == 0
        saved = await storage.set_draft(USER, state["day"], 0, state["revision"], "КО")
        assert saved["game"]["draft"] == "КО"
        async with administrator.begin() as connection:
            await connection.execute(text(
                f'grant delete on table "{schema}".wordli_profiles to "{role}"'
            ))
        with pytest.raises(WordliSchemaError, match="Неверные права"):
            await assert_runtime_permissions(runtime)
    finally:
        if runtime is not None:
            await runtime.dispose()
        try:
            if created:
                async with administrator.begin() as connection:
                    assert re.fullmatch(r"wordli_test_[0-9a-f]{32}", role)
                    await connection.execute(text(f'drop owned by "{role}"'))
                    await connection.execute(text(f'drop role "{role}"'))
        finally:
            await administrator.dispose()
