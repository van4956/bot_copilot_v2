"""Офлайн-проверки тестовой Mini App с временной SQLite и подписанным ID."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from prototypes.wordli.engine import GameError, LocalGameEngine
from prototypes.wordli.storage import WordliStorage, profiles_table
from prototypes.wordli.telegram_server import (
    MAX_BODY_BYTES,
    create_app,
    main,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TOKEN = "123456:UNIT_TEST_NOT_A_REAL_TOKEN"
ORIGIN = "https://wordli-unit.example"
USER_ID = 789012345
OTHER_ID = USER_ID + 1


@dataclass
class Clock:
    """Управляет только временем отдельного тестового приложения."""

    value: datetime = NOW

    def now(self) -> datetime:
        """Возвращает установленный тестовый момент."""
        return self.value


@dataclass
class AppEnvironment:
    """Содержит только временную БД и вымышленную тестовую конфигурацию."""

    app: web.Application
    storage: WordliStorage
    engine: LocalGameEngine
    database: AsyncEngine
    clock: Clock
    dictionary_path: Path


def signed_auth(user_id: int = USER_ID) -> str:
    """Подписывает вымышленного пользователя без настоящего токена."""
    values = {
        "auth_date": str(int(NOW.timestamp())),
        "user": json.dumps({"id": user_id, "first_name": "Unit test"}),
        "query_id": "UNIT_TEST_QUERY",
    }
    check = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**values, "hash": digest})


@pytest.fixture
async def app_environment(tmp_path: Path) -> AsyncIterator[AppEnvironment]:
    """Создаёт отдельные таблицы игры только во временном файле SQLite."""
    words = sorted(["КОШКА", "КОМАР", "ТАЧКА", "ПАРОМ", "БИЛЕТ", "ЛАМПА"])
    path = tmp_path / "dictionary.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "telegram-http-test",
                "normalization": "Ё=Е",
                "words": words,
                "answers": words,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = LocalGameEngine(path)
    database = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wordli.sqlite3'}"
    )
    clock = Clock()
    storage = WordliStorage(database, engine, clock=clock.now)
    await storage.init_schema()
    app = create_app(
        storage,
        TOKEN,
        ORIGIN,
        {USER_ID, OTHER_ID},
        clock=clock.now,
        expected_bot_id=123456,
    )
    try:
        yield AppEnvironment(app, storage, engine, database, clock, path)
    finally:
        await database.dispose()


async def request(
    app: web.Application,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    raw: bytes | None = None,
    authenticated: bool = True,
    user_id: int = USER_ID,
) -> web.Response:
    """Вызывает реальный aiohttp-обработчик без отключения запрета сети."""
    request_headers = CIMultiDict(headers or {})
    request_headers.setdefault("Host", "wordli-unit.example")
    if method == "POST":
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("Content-Length", str(len(body)))
        if authenticated:
            request_headers.setdefault(
                "X-Telegram-Init-Data", signed_auth(user_id)
            )
    else:
        body = b""
    transport = Mock()
    transport.get_extra_info.return_value = ("127.0.0.1", 8770)
    stream = Mock()
    stream.readany = AsyncMock(side_effect=[body, b""])
    mocked = make_mocked_request(
        method,
        path,
        headers=request_headers,
        transport=transport,
        payload=stream,
        app=app,
    )
    route = await app.router.resolve(mocked)
    return await route.handler(mocked)


def result(response: web.Response) -> dict[str, Any]:
    """Разбирает JSON только из полученного тестового ответа."""
    return json.loads(response.text or "{}")


async def state(
    app: web.Application, user_id: int = USER_ID
) -> dict[str, Any]:
    """Получает подтверждённое серверное состояние заданного пользователя."""
    response = await request(app, "POST", "/api/state", user_id=user_id)
    assert response.status == 200
    return result(response)


def action_payload(current: dict[str, Any]) -> dict[str, Any]:
    """Готовит номер версии и идентификатор единственного действия."""
    return {
        "day": current["day"],
        "index": current["game"]["index"],
        "revision": current["revision"],
        "request_id": str(uuid4()),
    }


async def assigned_answer(
    environment: AppEnvironment, user_id: int = USER_ID
) -> str:
    """Читает ответ только из внутренней временной БД теста."""
    async with environment.database.connect() as connection:
        history = await connection.scalar(
            select(profiles_table.c.history).where(
                profiles_table.c.user_id == user_id
            )
        )
    return history[-1]["answer"]


async def test_health_is_minimal_and_never_exposes_identity(
    app_environment: AppEnvironment,
) -> None:
    """Оставляет открытым только минимальный признак тестового сервера."""
    response = await request(app_environment.app, "GET", "/api/health")
    assert response.status == 200
    assert result(response) == {
        "ok": True,
        "test_only": True,
        "database": True,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in response.headers
    assert USER_ID.__str__() not in (response.text or "")


async def test_authenticated_bootstrap_hides_history_and_answer(
    app_environment: AppEnvironment,
) -> None:
    """Отдаёт только своё поле, серверную версию и реальную статистику."""
    current = await state(app_environment.app)
    assert current["storage"] == "server"
    assert current["server_time"] == "2026-11-05T12:00:00Z"
    assert type(current["revision"]) is int
    assert "history" not in current
    assert current["game"]["answer"] is None
    assert current["game"]["rows"] == []
    assert current["stats"]["played"] == 0


@pytest.mark.parametrize("endpoint", ["state", "guess", "next", "draft"])
async def test_all_api_endpoints_require_signed_telegram_identity(
    app_environment: AppEnvironment, endpoint: str
) -> None:
    """Не обращается к прогрессу без подтверждённых Telegram-данных."""
    response = await request(
        app_environment.app,
        "POST",
        f"/api/{endpoint}",
        authenticated=False,
    )
    assert response.status == 401
    assert result(response)["error"]["code"] == "authentication_failed"


async def test_invalid_signature_and_disallowed_user_fail_closed(
    app_environment: AppEnvironment,
) -> None:
    """Отклоняет подмену подписи и пользователя вне тестового списка."""
    forged = await request(
        app_environment.app,
        "POST",
        "/api/state",
        headers={"X-Telegram-Init-Data": signed_auth() + "forged"},
    )
    assert forged.status == 401
    outsider = await request(
        app_environment.app, "POST", "/api/state", user_id=999999999
    )
    assert outsider.status == 403
    assert result(outsider)["error"]["code"] == "access_denied"


@pytest.mark.parametrize(
    "field",
    ["history", "stats", "user_id", "answer", "used_words", "dictionary_id"],
)
async def test_state_never_accepts_client_owned_progress_or_identity(
    app_environment: AppEnvironment, field: str
) -> None:
    """Не импортирует локальные результаты и чужой ID из JSON браузера."""
    response = await request(
        app_environment.app, "POST", "/api/state", {field: "forged"}
    )
    assert response.status == 400
    assert result(response)["error"]["code"] == "invalid_request"


async def test_guess_idempotency_reload_and_user_isolation(
    app_environment: AppEnvironment,
) -> None:
    """Повтор ответа не удваивает победу и не меняет чужую статистику."""
    app = app_environment.app
    current = await state(app)
    payload = action_payload(current)
    payload["guess"] = await assigned_answer(app_environment)
    first = await request(app, "POST", "/api/guess", payload)
    second = await request(app, "POST", "/api/guess", payload)
    assert first.status == second.status == 200
    assert result(first) == result(second)
    won = result(first)
    assert won["game"]["outcome"] == "win"
    assert won["stats"]["wins"] == won["stats"]["played"] == 1
    assert (await state(app))["stats"]["wins"] == 1
    assert (await state(app, OTHER_ID))["stats"]["played"] == 0
    assert "history" not in won


async def test_next_game_requires_explicit_action_and_is_idempotent(
    app_environment: AppEnvironment,
) -> None:
    """Не создаёт дополнительную партию при обновлении или повторе запроса."""
    app = app_environment.app
    current = await state(app)
    payload = action_payload(current)
    payload["guess"] = await assigned_answer(app_environment)
    won = result(await request(app, "POST", "/api/guess", payload))
    next_payload = action_payload(won)
    first = await request(app, "POST", "/api/next", next_payload)
    second = await request(app, "POST", "/api/next", next_payload)
    assert first.status == second.status == 200
    assert result(first) == result(second)
    next_state = result(first)
    assert next_state["game"]["index"] == 1
    assert next_state["game"]["answer"] is None
    assert next_state["stats"]["wins"] == 1
    assert (await state(app))["game"]["index"] == 1


async def test_draft_is_persisted_and_revision_conflict_returns_fresh_state(
    app_environment: AppEnvironment,
) -> None:
    """Сохраняет черновик и защищает его от устаревшего второго устройства."""
    app = app_environment.app
    current = await state(app)
    payload = {
        "day": current["day"],
        "index": 0,
        "revision": current["revision"],
        "draft": "КОШ",
    }
    first = await request(app, "POST", "/api/draft", payload)
    assert first.status == 200
    assert result(first)["game"]["draft"] == "КОШ"
    conflict = await request(
        app, "POST", "/api/draft", {**payload, "draft": "ТАЧ"}
    )
    assert conflict.status == 409
    response = result(conflict)
    assert response["error"]["code"] == "version_conflict"
    assert response["state"]["game"]["draft"] == "КОШ"
    assert response["state"]["storage"] == "server"
    assert "history" not in response["state"]
    assert (await state(app))["game"]["draft"] == "КОШ"


@pytest.mark.parametrize("endpoint", ["state", "guess", "next", "draft"])
@pytest.mark.parametrize(
    "field", ["history", "answer", "used_words", "dictionary_id", "player_id"]
)
async def test_api_never_imports_word_assignments_from_browser(
    app_environment: AppEnvironment, endpoint: str, field: str
) -> None:
    """Все маршруты отвергают подмену персональной выдачи и истории."""
    app = app_environment.app
    initial = await state(app)
    payload: dict[str, Any] = {}
    if endpoint != "state":
        payload = action_payload(initial)
        if endpoint == "guess":
            payload["guess"] = "КОШКА"
        elif endpoint == "draft":
            payload.pop("request_id")
            payload["draft"] = "КО"
    payload[field] = "forged-assignment"
    response = await request(app, "POST", f"/api/{endpoint}", payload)
    assert response.status == 400
    assert result(response)["error"]["code"] == "invalid_request"
    assert await state(app) == initial


async def test_loss_and_expiry_reserve_personal_words_without_extra_losses(
    app_environment: AppEnvironment,
) -> None:
    """Поражение и незавершённая просрочка навсегда исключают свои слова."""
    app = app_environment.app
    current = await state(app)
    first_answer = await assigned_answer(app_environment)
    words = ["КОШКА", "КОМАР", "ТАЧКА", "ПАРОМ", "БИЛЕТ", "ЛАМПА"]
    wrong = next(word for word in words if word != first_answer)
    assert first_answer not in json.dumps(current, ensure_ascii=False)
    for attempt in range(6):
        payload = {**action_payload(current), "guess": wrong}
        response = await request(app, "POST", "/api/guess", payload)
        assert response.status == 200
        current = result(response)
        assert "history" not in current
        if attempt < 5:
            assert first_answer not in json.dumps(current, ensure_ascii=False)
    assert current["game"]["outcome"] == "loss"
    assert current["stats"]["played"] == 1
    opened = await request(app, "POST", "/api/next", action_payload(current))
    assert opened.status == 200
    unfinished = result(opened)
    second_answer = await assigned_answer(app_environment)
    assert second_answer != first_answer
    assert second_answer not in json.dumps(unfinished, ensure_ascii=False)
    app_environment.clock.value = NOW + timedelta(hours=13)
    tomorrow = await state(app)
    third_answer = await assigned_answer(app_environment)
    assert third_answer not in {first_answer, second_answer}
    assert tomorrow["stats"]["played"] == 1
    assert tomorrow["stats"]["wins"] == 0
    assert tomorrow["game"]["guesses"] == []
    assert third_answer not in json.dumps(tomorrow, ensure_ascii=False)
    stale = await request(
        app,
        "POST",
        "/api/guess",
        {**action_payload(unfinished), "guess": second_answer},
    )
    assert stale.status == 409
    recovered = result(stale)
    assert recovered["error"]["code"] == "version_conflict"
    assert "history" not in recovered["state"]
    assert third_answer not in json.dumps(recovered, ensure_ascii=False)
    assert await state(app) == tomorrow


async def test_exhausted_dictionary_keeps_statistics_and_rejects_replays(
    app_environment: AppEnvironment,
) -> None:
    """Исчерпание всех ответов не зацикливает выдачу и не скрывает статистику."""
    app = app_environment.app
    current = await state(app)
    answers: set[str] = set()
    for number in range(6):
        answer = await assigned_answer(app_environment)
        assert answer not in answers
        answers.add(answer)
        response = await request(
            app,
            "POST",
            "/api/guess",
            {**action_payload(current), "guess": answer},
        )
        assert response.status == 200
        current = result(response)
        if number < 5:
            opened = await request(
                app, "POST", "/api/next", action_payload(current)
            )
            assert opened.status == 200
            current = result(opened)
    assert current["stats"]["played"] == current["stats"]["wins"] == 6
    unavailable = await request(
        app, "POST", "/api/next", action_payload(current)
    )
    assert unavailable.status == 409
    assert result(unavailable)["error"]["code"] == "no_more_words"
    assert (await state(app))["stats"]["wins"] == 6
    app_environment.clock.value = NOW + timedelta(hours=13)
    exhausted = await state(app)
    assert exhausted["game"]["status"] == "exhausted"
    assert exhausted["game"]["answer"] is None
    assert exhausted["availability"] == {"remaining": 0, "exhausted": True}
    assert exhausted["stats"]["wins"] == exhausted["stats"]["played"] == 6
    assert "history" not in exhausted
    assert await state(app) == exhausted
    for endpoint, details in [
        ("guess", {"guess": "КОШКА"}),
        ("draft", {"draft": "КО"}),
        ("next", {}),
    ]:
        payload = {**action_payload(exhausted), **details}
        if endpoint == "draft":
            payload.pop("request_id")
        rejected = await request(app, "POST", f"/api/{endpoint}", payload)
        assert rejected.status == 409
        assert result(rejected)["error"]["code"] == "no_more_words"
    assert await state(app) == exhausted


async def test_dictionary_upgrade_preserves_answers_and_recovers_stale_device(
    app_environment: AppEnvironment,
) -> None:
    """Расширение словаря не меняет победу, активный ответ или черновик."""
    app = app_environment.app
    initial = await state(app)
    first_answer = await assigned_answer(app_environment)
    winning_payload = {**action_payload(initial), "guess": first_answer}
    won = result(await request(app, "POST", "/api/guess", winning_payload))
    active = result(
        await request(app, "POST", "/api/next", action_payload(won))
    )
    second_answer = await assigned_answer(app_environment)
    draft_payload = {
        "day": active["day"], "index": active["game"]["index"],
        "revision": active["revision"], "draft": "КО",
    }
    saved = result(await request(app, "POST", "/api/draft", draft_payload))
    path = app_environment.dictionary_path
    original = json.loads(path.read_text(encoding="utf-8"))
    archive = path.parent / "archive"
    archive.mkdir()
    (archive / f"{original['dictionary_id']}.json").write_bytes(path.read_bytes())
    expanded = {
        **original, "dictionary_id": "telegram-http-expanded",
        "words": sorted([*original["words"], "АРБУЗ"]),
        "answers": sorted([*original["answers"], "АРБУЗ"]),
    }
    updated_path = path.parent / "expanded.json"
    updated_path.write_text(json.dumps(expanded), encoding="utf-8")
    rules = LocalGameEngine(updated_path)
    upgraded_storage = WordliStorage(
        app_environment.database, rules, clock=app_environment.clock.now
    )
    upgraded_app = create_app(
        upgraded_storage, TOKEN, ORIGIN, {USER_ID, OTHER_ID},
        clock=app_environment.clock.now, expected_bot_id=123456,
    )
    stale = await request(
        upgraded_app, "POST", "/api/draft",
        {**draft_payload, "revision": saved["revision"], "draft": "ТА"},
    )
    assert stale.status == 409
    recovered = result(stale)["state"]
    assert recovered["revision"] > saved["revision"]
    assert recovered["game"]["draft"] == "КО"
    assert recovered["game"]["rows"] == []
    assert recovered["game"]["answer"] is None
    assert second_answer not in json.dumps(recovered, ensure_ascii=False)
    assert recovered["dictionary"]["id"] == rules.dictionary_id
    assert recovered["stats"]["wins"] == recovered["stats"]["played"] == 1
    assert "history" not in recovered
    assert await assigned_answer(app_environment) == second_answer
    retried = await request(upgraded_app, "POST", "/api/guess", winning_payload)
    assert retried.status == 200
    assert result(retried)["revision"] == won["revision"]
    assert await state(upgraded_app) == recovered


@pytest.mark.parametrize(
    "origin", ["null", "https://other.example", ORIGIN + "/"]
)
async def test_foreign_or_opaque_origin_is_rejected(
    app_environment: AppEnvironment, origin: str
) -> None:
    """Не разрешает межсайтовый доступ, включая непрозрачный Origin."""
    response = await request(
        app_environment.app,
        "POST",
        "/api/state",
        headers={"Origin": origin},
    )
    assert response.status == 403
    assert result(response)["error"]["code"] == "invalid_origin"
    assert "Access-Control-Allow-Origin" not in response.headers


async def test_public_origin_and_exact_loopback_hosts_are_accepted(
    app_environment: AppEnvironment,
) -> None:
    """Проверяет публичный origin и локальный порт без доверия любому Host."""
    for host in ["wordli-unit.example", "127.0.0.1:8770", "localhost:8770"]:
        response = await request(
            app_environment.app,
            "POST",
            "/api/state",
            headers={"Origin": ORIGIN, "Host": host},
        )
        assert response.status == 200
    for host in ["evil.example", "127.0.0.1:9999", "localhost"]:
        response = await request(
            app_environment.app, "GET", "/api/health", headers={"Host": host}
        )
        assert response.status == 403


async def test_duplicate_host_origin_or_auth_headers_are_rejected(
    app_environment: AppEnvironment,
) -> None:
    """Не выбирает произвольное значение из повторных служебных заголовков."""
    for name, value, expected in [
        ("Host", "wordli-unit.example", 403),
        ("Origin", ORIGIN, 403),
        ("X-Telegram-Init-Data", signed_auth(), 401),
    ]:
        headers = CIMultiDict([(name, value), (name, value)])
        response = await request(
            app_environment.app, "POST", "/api/state", headers=headers
        )
        assert response.status == expected


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/telegram_server.py",
        "/telegram_auth.py",
        "/storage.py",
        "/data/ru5.json",
        "/data/archive/ru5-opencorpora-wordfreq-v1-165050f1e2b8.json",
        "/../.env",
        "/%2e%2e/.env",
        "/README.md",
    ],
)
async def test_only_static_allowlist_is_served(
    app_environment: AppEnvironment, path: str
) -> None:
    """Не выдаёт исходники, настройки, каталог словаря и обходы пути."""
    response = await request(app_environment.app, "GET", path)
    assert response.status == 404


async def test_telegram_scripts_are_injected_only_in_authenticated_server(
    app_environment: AppEnvironment,
) -> None:
    """Не меняет локальный HTML и подключает SDK только в тестовой Mini App."""
    response = await request(app_environment.app, "GET", "/")
    assert response.status == 200
    html = response.text or ""
    assert "https://telegram.org/js/telegram-web-app.js?64" in html
    assert "window.WORDLI_MODE='telegram'" in html
    assert html.index("telegram_client.js") < html.index("prototype.js")
    original = (
        Path(__file__).resolve().parents[1]
        / "prototypes/wordli/index.html"
    ).read_text(encoding="utf-8")
    assert "telegram-web-app.js" not in original
    assert "WORDLI_MODE" not in original
    for path in ["/styles.css", "/prototype.js", "/telegram_client.js"]:
        asset = await request(app_environment.app, "GET", path)
        assert asset.status == 200
        assert asset.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("method", ["OPTIONS", "PUT", "DELETE"])
async def test_preflight_and_other_methods_do_not_enable_cors(
    app_environment: AppEnvironment, method: str
) -> None:
    """Не включает CORS через OPTIONS и посторонние HTTP-методы."""
    response = await request(app_environment.app, method, "/api/state")
    assert response.status == 405
    assert "Access-Control-Allow-Origin" not in response.headers


async def test_body_size_content_type_and_json_are_checked(
    app_environment: AppEnvironment,
) -> None:
    """Не принимает большой, неоднозначный или не-JSON запрос."""
    app = app_environment.app
    oversized = await request(app, "POST", "/api/state", raw=b"a" * 32769)
    assert oversized.status == 413
    raw_limited = await request(
        app,
        "POST",
        "/api/state",
        raw=b"a" * (MAX_BODY_BYTES + 1),
        headers={"Content-Length": "1"},
    )
    assert raw_limited.status == 413
    wrong_type = await request(
        app, "POST", "/api/state", headers={"Content-Type": "text/plain"}
    )
    assert wrong_type.status == 415
    for raw in [b"broken", b"null", b"[]", b'{"x":1,"x":2}']:
        bad = await request(app, "POST", "/api/state", raw=raw)
        assert bad.status == 400


async def test_generic_exception_does_not_reveal_details(
    app_environment: AppEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не отдаёт внутренние исключения и секретные значения в ответе."""
    monkeypatch.setattr(
        app_environment.storage,
        "bootstrap",
        AsyncMock(side_effect=RuntimeError("SECRET_TEST_DIAGNOSTIC")),
    )
    response = await request(app_environment.app, "POST", "/api/state")
    assert response.status == 500
    assert result(response)["error"]["code"] == "internal_error"
    assert "SECRET_TEST_DIAGNOSTIC" not in (response.text or "")


async def test_conflict_recovery_failure_keeps_safe_original_error(
    app_environment: AppEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не подменяет конфликт ложным состоянием при недоступной истории."""
    monkeypatch.setattr(
        app_environment.storage,
        "set_draft",
        AsyncMock(side_effect=GameError("version_conflict", "Конфликт", 409)),
    )
    monkeypatch.setattr(
        app_environment.storage,
        "bootstrap",
        AsyncMock(side_effect=GameError("dictionary_changed", "Словарь", 409)),
    )
    response = await request(
        app_environment.app,
        "POST",
        "/api/draft",
        {"day": "2026-11-05", "index": 0, "draft": "", "revision": 0},
    )
    assert response.status == 409
    assert "state" not in result(response)


@pytest.mark.parametrize(
    "origin",
    [
        "http://wordli-unit.example",
        "https://wordli-unit.example/path",
        "https://user:secret@wordli-unit.example",
        "https://wordli-unit.example?x=1",
        "https://wordli-unit.example#x",
        " https://wordli-unit.example",
        "https://wordli-unit.example:wrong",
        "https://wordli-unit.example:0",
        "https://wordli-unit.example:65536",
        "https://wordli-unit.example\x00",
        "https://wordli-unit.example\x7f",
    ],
)
async def test_application_rejects_ambiguous_public_origin(
    app_environment: AppEnvironment, origin: str
) -> None:
    """Не запускает сервер с посторонним origin или секретами в URL."""
    with pytest.raises(ValueError, match="HTTPS origin"):
        create_app(
            app_environment.storage,
            TOKEN,
            origin,
            {USER_ID},
            expected_bot_id=123456,
        )


async def test_expected_bot_and_nonempty_acl_are_mandatory(
    app_environment: AppEnvironment,
) -> None:
    """Нельзя случайно запустить любой токен или открыть игру всем."""
    for expected_id in [None, True, 1, 654321]:
        with pytest.raises(ValueError, match="тестового бота"):
            create_app(
                app_environment.storage,
                TOKEN,
                ORIGIN,
                {USER_ID},
                expected_bot_id=expected_id,
            )
    for allowed in [set(), {True}, {0}, {-1}, {1 << 52}]:
        with pytest.raises(ValueError, match="список пользователей"):
            create_app(
                app_environment.storage,
                TOKEN,
                ORIGIN,
                allowed,
                expected_bot_id=123456,
            )


def test_cli_refuses_production_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не читает даже конфигурацию при попытке боевого режима запуска."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        "sys.argv", ["telegram_server", "--public-origin", ORIGIN]
    )
    forbidden = Mock(side_effect=AssertionError("Settings must stay unread"))
    monkeypatch.setattr(
        "prototypes.wordli.telegram_server.load_config", forbidden
    )
    with pytest.raises(SystemExit):
        main()
    forbidden.assert_not_called()
