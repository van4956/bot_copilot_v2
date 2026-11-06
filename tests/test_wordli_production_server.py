"""Офлайн-проверки рабочего сервера игры без настоящих секретов и служб."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from prototypes.wordli import production_server
from prototypes.wordli.production_server import (
    create_production_app,
    database_ready,
    load_production_config,
    main,
)
from prototypes.wordli.production_schema import WordliSchemaError
from prototypes.wordli.storage import WordliStorage
from prototypes.wordli.telegram_server import create_app

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
TOKEN = "876543:PRODUCTION_SYNTHETIC_NOT_REAL"
TEST_TOKEN = "123456:TEST_SYNTHETIC_NOT_REAL"
ORIGIN = "https://wordli-production.example"
USER_ID = 900001
OTHER_ID = 900002
DATABASE_URL = "postgresql+asyncpg://wordli_app:synthetic_password@db/copilot"


def environment(**changes: str) -> dict[str, str]:
    """Возвращает только вымышленные рабочие настройки."""
    return {
        "APP_ENV": "production",
        "WORDLI_PUBLIC_ORIGIN": ORIGIN,
        "WORDLI_DATABASE_URL": DATABASE_URL,
        "WORDLI_BOT_TOKEN": TOKEN,
        "WORDLI_EXPECTED_BOT_ID": "876543",
        "WORDLI_ALLOWED_USER_IDS": str(USER_ID),
        **changes,
    }


def signed_auth(user_id: int, token: str = TOKEN) -> str:
    """Подписывает синтетический аккаунт одним из двух тестовых ключей."""
    values = {
        "auth_date": str(int(NOW.timestamp())),
        "user": json.dumps({"id": user_id}),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**values, "hash": digest})


async def request(
    app: web.Application,
    path: str = "/api/state",
    *,
    user_id: int = USER_ID,
    token: str | None = TOKEN,
    method: str = "POST",
    headers: Mapping[str, str] | None = None,
) -> web.Response:
    """Вызывает HTTP-обработчик без сети и без настоящего Telegram."""
    body = b"{}"
    request_headers = {
        "Host": "wordli-production.example",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        **(headers or {}),
    }
    if token is not None:
        request_headers["X-Telegram-Init-Data"] = signed_auth(user_id, token)
    stream = Mock()
    stream.readany = AsyncMock(side_effect=[body, b""])
    transport = Mock()
    transport.get_extra_info.return_value = ("127.0.0.1", 8770)
    incoming = make_mocked_request(
        method, path, headers=request_headers, transport=transport,
        payload=stream, app=app,
    )
    route = await app.router.resolve(incoming)
    return await route.handler(incoming)


def storage() -> SimpleNamespace:
    """Создаёт хранилище-заглушку с приватным ответом в истории."""
    return SimpleNamespace(
        bootstrap=AsyncMock(return_value={
            "history": [{"answer": "КОШКА"}],
            "game": {"answer": None, "outcome": None},
            "revision": 1,
        }),
    )


def test_production_settings_are_explicit_and_hide_secrets() -> None:
    """Чужой BOT_TOKEN не заменяет специально заданный токен игрового API."""
    config = load_production_config(environment(BOT_TOKEN=TEST_TOKEN))
    assert config.bot_token == TOKEN
    assert config.expected_bot_id == 876543
    assert config.public_origin == ORIGIN
    assert config.database_url == DATABASE_URL
    assert config.access == "restricted"
    assert config.allowed_user_ids == frozenset({USER_ID})
    assert TOKEN not in repr(config)
    assert "synthetic_password" not in repr(config)
    assert str(USER_ID) not in repr(config)


@pytest.mark.parametrize(
    "changes",
    [
        {"APP_ENV": "test"}, {"APP_ENV": ""},
        {"WORDLI_BOT_TOKEN": ""}, {"WORDLI_BOT_TOKEN": TEST_TOKEN},
        {"WORDLI_EXPECTED_BOT_ID": ""}, {"WORDLI_EXPECTED_BOT_ID": "0"},
        {"WORDLI_EXPECTED_BOT_ID": "876543 "},
        {"WORDLI_PUBLIC_ORIGIN": "http://wordli-production.example"},
        {"WORDLI_PUBLIC_ORIGIN": "https://wordli-production.example/path"},
        {"WORDLI_PUBLIC_ORIGIN": "https://wordli-production.example:0"},
        {"WORDLI_PUBLIC_ORIGIN": "https://wordli-production.example\x00"},
        {"WORDLI_PUBLIC_ORIGIN": "https://unit.trycloudflare.com"},
        {"WORDLI_PUBLIC_ORIGIN": "https://UNIT.TRYCLOUDFLARE.COM."},
        {"WORDLI_PUBLIC_ORIGIN": "https://trycloudflare.com"},
        {"WORDLI_PUBLIC_ORIGIN": "https://user:synthetic@wordli.example"},
        {"WORDLI_DATABASE_URL": "sqlite+aiosqlite:///:memory:"},
        {"WORDLI_DATABASE_URL": "postgresql://wordli_app:synthetic@db/copilot"},
        {"WORDLI_DATABASE_URL": "postgresql+asyncpg://wordli_app:pw@db/copilot_review_test"},
        {"WORDLI_DATABASE_URL": "postgresql+asyncpg://postgres:pw@db/copilot"},
        {"WORDLI_DATABASE_URL": "postgresql+asyncpg://wordli_app@db/copilot"},
        {"WORDLI_DATABASE_URL": "postgresql+asyncpg://wordli_app:pw@/copilot"},
        {"WORDLI_DATABASE_URL": "postgresql+asyncpg://wordli_app:pw@db:0/copilot"},
        {"WORDLI_DATABASE_URL": "not a database URL"},
        {"WORDLI_ACCESS": ""}, {"WORDLI_ACCESS": "everyone"},
        {"WORDLI_ALLOWED_USER_IDS": ""}, {"WORDLI_ALLOWED_USER_IDS": "0"},
        {"WORDLI_ALLOWED_USER_IDS": "-1"}, {"WORDLI_ALLOWED_USER_IDS": "true"},
        {"WORDLI_ALLOWED_USER_IDS": f"{USER_ID},"},
        {"WORDLI_ALLOWED_USER_IDS": str(1 << 52)},
    ],
)
def test_unsafe_production_settings_fail_closed(changes: dict[str, str]) -> None:
    """Небезопасная конфигурация отклоняется без соединения с БД или Telegram."""
    with pytest.raises(ValueError) as error:
        load_production_config(environment(**changes))
    assert TOKEN not in str(error.value)
    assert "synthetic_password" not in str(error.value)


def test_settings_read_only_explicit_file_and_environment_wins(tmp_path: Path) -> None:
    """Файлы окружения бота не читаются автоматически."""
    path = tmp_path / "production.env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in environment().items()),
        encoding="utf-8",
    )
    config = load_production_config({"WORDLI_ACCESS": "all"}, path)
    assert config.access == "all"
    assert not config.allowed_user_ids
    with pytest.raises(ValueError, match="APP_ENV"):
        load_production_config({})
    with pytest.raises(ValueError, match="не найден"):
        load_production_config({}, tmp_path / "missing.env")


def test_test_default_cannot_be_made_public_accidentally() -> None:
    """Публичный доступ требует явного production и проверки готовности БД."""
    with pytest.raises(ValueError, match="список пользователей"):
        create_app(storage(), TOKEN, ORIGIN, None, expected_bot_id=876543)
    with pytest.raises(ValueError, match="проверки доступности БД"):
        create_app(
            storage(), TOKEN, ORIGIN, None, expected_bot_id=876543,
            mode="production",
        )
    with pytest.raises(ValueError, match="режим"):
        create_app(storage(), TOKEN, ORIGIN, {USER_ID}, mode="unexpected")


async def test_production_restricted_and_all_access_still_require_main_bot_signature() -> None:
    """Публичный режим допускает только правильно подписанный Telegram ID."""
    for allowed in [{USER_ID}, None]:
        backend = storage()
        app = create_app(
            backend, TOKEN, ORIGIN, allowed, clock=lambda: NOW,
            expected_bot_id=876543, mode="production",
            readiness_check=AsyncMock(return_value=True),
        )
        accepted = await request(app)
        assert accepted.status == 200
        assert "history" not in json.loads(accepted.text)
        other = await request(app, user_id=OTHER_ID)
        assert other.status == (403 if allowed is not None else 200)
        for token in [None, TEST_TOKEN]:
            rejected = await request(app, token=token)
            assert rejected.status == 401
            assert "тестового" not in rejected.text
            assert TOKEN not in rejected.text


@pytest.mark.parametrize("ready,expected_status", [(True, 200), (False, 503)])
async def test_production_health_checks_database(ready: bool, expected_status: int) -> None:
    """Готовность отражает БД, не выдавая ложный test_only или секреты."""
    callback = AsyncMock(return_value=ready)
    app = create_app(
        storage(), TOKEN, ORIGIN, {USER_ID}, expected_bot_id=876543,
        mode="production", readiness_check=callback,
    )
    response = await request(app, "/api/health", method="GET", token=None)
    assert response.status == expected_status
    assert json.loads(response.text) == {
        "ok": ready, "test_only": False, "database": ready, "mode": "production",
    }
    callback.assert_awaited_once_with()


async def test_production_health_does_not_leak_database_error() -> None:
    """Исключение драйвера не раскрывает адрес БД и пароль через health."""
    app = create_app(
        storage(), TOKEN, ORIGIN, {USER_ID}, expected_bot_id=876543,
        mode="production", readiness_check=AsyncMock(side_effect=RuntimeError(DATABASE_URL)),
    )
    response = await request(app, "/api/health", method="GET")
    assert response.status == 503
    assert "synthetic_password" not in response.text


async def test_database_readiness_uses_only_read_query(tmp_path: Path) -> None:
    """Проверка не создаёт игровые таблицы и не выполняет DDL."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'health.sqlite3'}")
    statements = []

    def statement(connection, cursor, sql, parameters, context, many) -> None:
        """Запоминает SQL только отдельной временной БД."""
        statements.append(sql.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", statement)
    try:
        assert await database_ready(engine) is True
        assert statements == ["select 1"]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("schema_exists", [True, False])
async def test_production_startup_checks_schema_without_creating_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schema_exists: bool,
) -> None:
    """Запуск проверяет ранее созданные таблицы и закрывает БД при отказе."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'startup.sqlite3'}")
    config = load_production_config(environment())
    monkeypatch.setattr(production_server, "create_async_engine", lambda *a, **k: engine)
    check = AsyncMock(side_effect=None if schema_exists else ValueError("missing schema"))
    monkeypatch.setattr(production_server, "assert_schema", check)
    permissions = AsyncMock()
    monkeypatch.setattr(production_server, "assert_runtime_permissions", permissions)
    init_schema = AsyncMock(side_effect=AssertionError("runtime DDL forbidden"))
    monkeypatch.setattr(WordliStorage, "init_schema", init_schema)
    original_dispose = type(engine).dispose
    dispose = AsyncMock()

    async def dispose_engine(target, close=True) -> None:
        """Закрывает настоящий временный пул с учётом вызова."""
        await dispose(target)
        await original_dispose(target, close=close)

    monkeypatch.setattr(type(engine), "dispose", dispose_engine)
    app = create_production_app(config)
    app.freeze()
    if schema_exists:
        await app.startup()
        await app.cleanup()
    else:
        with pytest.raises(ValueError, match="missing schema"):
            await app.startup()
    check.assert_awaited_once_with(engine)
    dispose.assert_awaited_once_with(engine)
    if schema_exists:
        permissions.assert_awaited_once_with(engine)
    else:
        permissions.assert_not_awaited()
    init_schema.assert_not_awaited()


def test_production_cli_defaults_to_loopback_and_has_no_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI запускает только HTTP-приложение и по умолчанию не слушает внешнюю сеть."""
    config = load_production_config(environment())
    load = Mock(return_value=config)
    app = object()
    monkeypatch.setattr(production_server, "load_production_config", load)
    monkeypatch.setattr(production_server, "create_production_app", Mock(return_value=app))
    run = Mock()
    monkeypatch.setattr(production_server.web, "run_app", run)
    monkeypatch.setattr(production_server.os.sys, "argv", ["wordli-production"])
    main()
    load.assert_called_once_with(env_file=None)
    run.assert_called_once_with(app, host="127.0.0.1", port=8770, access_log=None, print=None)


@pytest.mark.parametrize("arguments", [["--host", "not-an-ip"], ["--port", "0"]])
def test_invalid_bind_stops_before_application_creation(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    """Неверный bind не открывает пул БД и сервер."""
    create = Mock(side_effect=AssertionError("must not create"))
    monkeypatch.setattr(production_server, "create_production_app", create)
    monkeypatch.setattr(production_server.os.sys, "argv", ["wordli-production", *arguments])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    create.assert_not_called()


async def test_missing_real_schema_is_not_created_by_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Настоящая пустая временная БД остаётся пустой после отказа сервера."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.sqlite3'}")
    monkeypatch.setattr(production_server, "create_async_engine", lambda *a, **k: engine)
    init_schema = AsyncMock(side_effect=AssertionError("runtime DDL forbidden"))
    monkeypatch.setattr(WordliStorage, "init_schema", init_schema)
    app = create_production_app(load_production_config(environment()))
    app.freeze()
    with pytest.raises(WordliSchemaError, match="Не создана таблица"):
        await app.startup()
    init_schema.assert_not_awaited()
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("select count(*) from sqlite_master where type='table'")
        ) == 0
    await engine.dispose()


async def test_runtime_permissions_failure_prevents_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Несовместимые права не превращаются в запущенный рабочий API."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'permissions.sqlite3'}")
    monkeypatch.setattr(production_server, "create_async_engine", lambda *a, **k: engine)
    schema = AsyncMock()
    permissions = AsyncMock(side_effect=WordliSchemaError("Недопустимые права"))
    monkeypatch.setattr(production_server, "assert_schema", schema)
    monkeypatch.setattr(production_server, "assert_runtime_permissions", permissions)
    app = create_production_app(load_production_config(environment()))
    app.freeze()
    with pytest.raises(WordliSchemaError, match="Недопустимые права"):
        await app.startup()
    schema.assert_awaited_once_with(engine)
    permissions.assert_awaited_once_with(engine)


def test_nonproduction_cli_cannot_start_http_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тестовое окружение не запускает рабочий HTTP даже со схожими именами ключей."""
    run = Mock()
    monkeypatch.setattr(production_server.web, "run_app", run)
    monkeypatch.setattr(production_server.os.sys, "argv", ["wordli-production"])
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    run.assert_not_called()
