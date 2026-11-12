"""Проверки Wordli только в собственной схеме локальной review PostgreSQL."""

import asyncio
import hashlib
import hmac
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
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
from prototypes.wordli.storage import (
    WordliStorage,
    profiles_table,
    requests_table,
)
from prototypes.wordli.telegram_server import create_app

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
            await connection.execute(
                text(f'create schema "{schema}" authorization bot_app')
            )
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
                    assert (
                        list((await connection.execute(public_query)).all()) == before
                    )
        finally:
            await control.dispose()


@pytest.fixture
def rules(tmp_path: Path) -> LocalGameEngine:
    """Использует маленький искусственный словарь вместо рабочих ответов."""
    path = tmp_path / "words.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "postgres-fixture-v1",
                "normalization": "Ё=Е",
                "words": WORDS,
                "answers": WORDS,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return LocalGameEngine(path)


async def _answer(database: AsyncEngine, state: dict) -> str:
    """Читает ответ только из выделенной тестовой схемы."""
    async with database.connect() as connection:
        history = await connection.scalar(
            select(profiles_table.c.history).where(profiles_table.c.user_id == USER)
        )
    return next(
        record["answer"]
        for record in history
        if record["day"] == state["day"] and record["index"] == state["game"]["index"]
    )


async def test_postgres_schema_migration_is_idempotent_and_keeps_other_data(
    database,
) -> None:
    """Одноразовая миграция не изменяет посторонние таблицы и записи."""
    with pytest.raises(WordliSchemaError, match="Не создана"):
        await assert_schema(database)
    async with database.begin() as connection:
        await connection.execute(
            text("create table unrelated (value integer not null)")
        )
        await connection.execute(text("insert into unrelated values (42)"))
    await create_schema(database)
    await create_schema(database)
    await assert_schema(database)
    async with database.connect() as connection:
        assert await connection.scalar(text("select value from unrelated")) == 42


async def test_postgres_settings_and_sparse_draft_survive_new_storage(
    database, rules
) -> None:
    """Позиции букв и добровольные настройки восстанавливаются новым процессом."""
    await create_schema(database)
    first = WordliStorage(database, rules, clock=lambda: NOW)
    state = await first.bootstrap(USER)
    await first.settings(
        USER,
        display_name="Тестовый игрок",
        timezone_name="Asia/Yerevan",
        reminder_enabled=True,
    )
    saved = await first.set_draft(USER, state["day"], 0, state["revision"], " А К")
    second = WordliStorage(database, rules, clock=lambda: NOW)
    assert (await second.bootstrap(USER))["game"]["draft"] == " А К"
    assert (await second.bootstrap(USER))["revision"] == saved["revision"]
    assert await second.settings(USER) == {
        "timezone": "Asia/Yerevan",
        "reminder_enabled": True,
    }


async def test_postgres_only_one_worker_claims_daily_reminder(database, rules) -> None:
    """Две независимые очереди PostgreSQL не отправляют одно напоминание дважды."""
    await create_schema(database)
    now = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
    first = WordliStorage(
        database, rules, clock=lambda: now, random_seconds=lambda: 600
    )
    second = WordliStorage(
        database, rules, clock=lambda: now, random_seconds=lambda: 600
    )
    await first.settings(USER, timezone_name="Etc/GMT-4", reminder_enabled=True)
    a, b = await asyncio.gather(
        first.claim_due_reminders(now), second.claim_due_reminders(now)
    )
    assert len(a) + len(b) == 1
    claim = (a + b)[0]
    await first.finish_reminder(USER, claim["claim_token"], "sent")
    assert await second.claim_due_reminders(now) == []
    async with database.connect() as connection:
        assert await connection.scalar(select(profiles_table.c.history)) == []


async def test_postgres_http_settings_draft_and_leaderboard(database, rules) -> None:
    """Подписанный HTTP-клиент работает с настоящей изолированной PostgreSQL."""
    await create_schema(database)
    storage = WordliStorage(database, rules, clock=lambda: NOW)
    token = "123456:POSTGRES_HTTP_OFFLINE_ONLY"
    values = {
        "auth_date": str(int(NOW.timestamp())),
        "user": json.dumps({"id": USER, "first_name": "HTTP тест"}),
    }
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signed = hmac.new(
        secret,
        "\n".join(f"{key}={value}" for key, value in sorted(values.items())).encode(),
        hashlib.sha256,
    ).hexdigest()
    auth = urlencode({**values, "hash": signed})
    app = create_app(
        storage,
        token,
        "https://wordli-http-test.example",
        {USER},
        clock=lambda: NOW,
        expected_bot_id=123456,
    )
    async with TestClient(TestServer(app)) as client:
        headers = {
            "X-Telegram-Init-Data": auth,
            "Origin": "https://wordli-http-test.example",
        }
        response = await client.post(
            "/api/settings",
            json={"timezone": "Asia/Yerevan", "reminder_enabled": True},
            headers=headers,
        )
        assert response.status == 200
        assert (await response.json())["settings"] == {
            "timezone": "Asia/Yerevan",
            "reminder_enabled": True,
        }
        response = await client.post("/api/state", json={}, headers=headers)
        state = await response.json()
        assert response.status == 200 and "history" not in state
        response = await client.post(
            "/api/draft",
            json={
                "day": state["day"],
                "index": 0,
                "revision": state["revision"],
                "draft": " А К",
            },
            headers=headers,
        )
        assert response.status == 200
        assert (await response.json())["game"]["draft"] == " А К"
        response = await client.post("/api/leaderboard", json={}, headers=headers)
        assert response.status == 200
        rating = (await response.json())["leaderboard"]
        assert rating["rows"] == [] and rating["self_eligible"] is False
        assert (await client.post("/api/settings", json={})).status == 401


async def test_postgres_leaderboard_keeps_histories_and_hides_private_fields(
    database,
) -> None:
    """Публичный рейтинг считает сохранённые партии без создания следующего слова."""
    await create_schema(database)
    rules = LocalGameEngine()
    storage = WordliStorage(database, rules, clock=lambda: NOW)
    for user_id, played in [(USER, 5), (USER + 1, 6), (USER + 2, 4)]:
        history = [
            {
                "day": (NOW.date() - timedelta(days=played - index)).isoformat(),
                "index": 0,
                "answer": answer,
                "dictionary_id": rules.dictionary_id,
                "guesses": [answer],
                "draft": "",
            }
            for index, answer in enumerate(rules._answers[:played])
        ]
        async with database.begin() as connection:
            await connection.execute(
                profiles_table.insert().values(
                    user_id=user_id,
                    history=history,
                    revision=1,
                    dictionary_id=rules.dictionary_id,
                )
            )
        await storage.settings(user_id, display_name=f"Тест {played}")
    async with database.connect() as connection:
        before = (
            await connection.execute(
                select(profiles_table).order_by(profiles_table.c.user_id)
            )
        ).all()
    result = await storage.leaderboard(USER)
    assert [row["played"] for row in result["rows"]] == [6, 5]
    assert result["self_eligible"] is True
    assert all(
        set(row) == {"rank", "name", "played", "score", "is_self"}
        for row in result["rows"]
    )
    async with database.connect() as connection:
        assert (
            await connection.execute(
                select(profiles_table).order_by(profiles_table.c.user_id)
            )
        ).all() == before


async def test_postgres_concurrent_bootstrap_and_uuid_are_atomic(
    database, rules
) -> None:
    """Два процесса видят одну выдачу, повтор UUID записывает один ход."""
    await create_schema(database)
    first = WordliStorage(database, rules, clock=lambda: NOW)
    second = WordliStorage(database, rules, clock=lambda: NOW)
    a, b = await asyncio.gather(first.bootstrap(USER), second.bootstrap(USER))
    assert a == b
    identifier = str(uuid4())
    arguments = (
        USER,
        a["day"],
        0,
        await _answer(database, a),
        a["revision"],
        identifier,
    )
    a, b = await asyncio.gather(first.submit(*arguments), second.submit(*arguments))
    assert a == b and a["stats"]["wins"] == 1
    async with database.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(requests_table))
            == 1
        )
        assert (
            await connection.scalar(select(func.count()).select_from(profiles_table))
            == 1
        )


async def test_postgres_draft_conflict_and_restart_preserve_progress(
    database, rules
) -> None:
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


async def test_postgres_expired_words_do_not_repeat_or_create_losses(
    database, rules
) -> None:
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
    database,
    rules,
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
            await connection.execute(
                text(
                    f"create role \"{role}\" login password '{password}' "
                    "nosuperuser nocreatedb nocreaterole noreplication nobypassrls"
                )
            )
            created = True
            await connection.execute(
                text(f'grant connect on database copilot_review_test to "{role}"')
            )
            await connection.execute(
                text(f'grant usage on schema "{schema}" to "{role}"')
            )
            await connection.execute(
                text(
                    f'grant select, insert, update on table "{schema}".wordli_profiles, '
                    f'"{schema}".wordli_requests, "{schema}".wordli_settings to "{role}"'
                )
            )
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
            await connection.execute(
                text(f'grant delete on table "{schema}".wordli_profiles to "{role}"')
            )
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
