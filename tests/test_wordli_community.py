"""Проверки настроек и рейтинга только во временной игровой БД."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from aiohttp.test_utils import make_mocked_request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from prototypes.wordli.engine import GameError, LocalGameEngine
from prototypes.wordli.storage import (
    DEFAULT_TIMEZONE,
    WordliStorage,
    profiles_table,
    settings_table,
)
from prototypes.wordli.telegram_server import create_app

NOW = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
USER_ID = 901001
TOKEN = "123456:COMMUNITY_OFFLINE_TEST"
ORIGIN = "https://wordli-unit.example"


@pytest_asyncio.fixture
async def community(tmp_path: Path) -> AsyncIterator[WordliStorage]:
    """Создаёт изолированную SQLite без env и настоящих токенов."""
    database = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'community-test.sqlite3'}"
    )
    storage = WordliStorage(database, LocalGameEngine(), lambda: NOW)
    await storage.init_schema()
    try:
        yield storage
    finally:
        await database.dispose()


def history(rules: LocalGameEngine, played: int, *, attempts: int = 1) -> list:
    """Готовит завершённые партии с неповторяющимися ответами."""
    words = list(rules._answers)
    records = []
    for index, answer in enumerate(words[:played]):
        guesses = [words[-1]] * (attempts - 1) + [answer]
        records.append(
            {
                "day": (NOW.date() - timedelta(days=played - index)).isoformat(),
                "index": 0,
                "answer": answer,
                "dictionary_id": rules.dictionary_id,
                "guesses": guesses,
                "draft": "",
            }
        )
    return records


async def seed(
    storage: WordliStorage,
    user_id: int,
    played: int,
    *,
    attempts: int = 1,
) -> None:
    """Записывает синтетический профиль без создания активного слова."""
    async with storage._database.begin() as connection:
        await connection.execute(
            profiles_table.insert().values(
                user_id=user_id,
                history=history(storage._rules, played, attempts=attempts),
                revision=1,
                dictionary_id=storage._rules.dictionary_id,
            )
        )
    await storage.settings(user_id, display_name=f"Игрок {user_id}")


def signed_auth(user_id: int, first_name: str, last_name: str = "") -> str:
    """Подписывает фиктивное имя для офлайн-проверок HTTP."""
    values = {
        "auth_date": str(int(NOW.timestamp())),
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": first_name,
                "last_name": last_name,
            }
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**values, "hash": digest})


async def api(
    storage: WordliStorage,
    path: str,
    payload: dict | None = None,
    *,
    auth: str | None = None,
) -> tuple[int, dict]:
    """Вызывает HTTP-обработчик без настоящих сетевых соединений."""
    app = create_app(
        storage,
        TOKEN,
        ORIGIN,
        {USER_ID},
        clock=lambda: NOW,
        expected_bot_id=123456,
    )
    body = json.dumps(payload or {}).encode()
    stream = Mock()
    stream.readany = AsyncMock(side_effect=[body, b""])
    transport = Mock()
    transport.get_extra_info.return_value = ("127.0.0.1", 8770)
    request = make_mocked_request(
        "POST",
        path,
        headers={
            "Host": "wordli-unit.example",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Telegram-Init-Data": auth or signed_auth(USER_ID, "Иван", "Тестовый"),
        },
        payload=stream,
        transport=transport,
        app=app,
    )
    route = await app.router.resolve(request)
    response = await route.handler(request)
    return response.status, json.loads(response.text)


async def test_settings_default_and_persistence(community: WordliStorage) -> None:
    """Напоминание выключено, предпочтения сохраняются без смены партии."""
    assert await community.settings(USER_ID) == {
        "timezone": DEFAULT_TIMEZONE,
        "reminder_enabled": False,
    }
    settings = await community.settings(
        USER_ID,
        timezone_name="Asia/Yerevan",
        reminder_enabled=True,
    )
    restored = WordliStorage(community._database, community._rules, lambda: NOW)
    assert await restored.settings(USER_ID) == settings
    async with community._database.connect() as connection:
        profile = (await connection.execute(select(profiles_table))).mappings().one()
    assert profile["history"] == []
    assert profile["revision"] == 0


@pytest.mark.parametrize("value", ["", "Fake/Nowhere", "/etc/passwd", "../UTC", "UTC "])
async def test_settings_reject_invalid_timezone(
    community: WordliStorage,
    value: str,
) -> None:
    """Посторонние пути и несуществующие пояса не сохраняются."""
    with pytest.raises(GameError, match="часовой пояс"):
        await community.settings(USER_ID, timezone_name=value)


async def test_public_settings_hide_name_and_internal_fields(
    community: WordliStorage,
) -> None:
    """Клиент получает только настройки, без внутреннего расписания."""
    result = await community.settings(USER_ID, display_name="  Иван\n\u202e Тест ")
    assert set(result) == {"timezone", "reminder_enabled"}
    async with community._database.connect() as connection:
        data = await connection.scalar(select(settings_table.c.data))
    assert data["name"] == "Иван Тест"


async def test_leaderboard_empty_and_ten_game_qualification(
    community: WordliStorage,
) -> None:
    """Незавершённые и первые девять игр не создают место в рейтинге."""
    assert (await community.leaderboard(USER_ID))["rows"] == []
    await seed(community, USER_ID, 9)
    await seed(community, USER_ID + 1, 10)
    board = await community.leaderboard(USER_ID)
    assert board["total"] == 1
    assert board["self_eligible"] is False
    assert board["rows"][0]["played"] == 10
    assert board["rows"][0]["is_self"] is False


async def test_leaderboard_dense_rank_and_safe_public_columns(
    community: WordliStorage,
) -> None:
    """Одинаковые очки и число игр дают одинаковое плотное место."""
    await seed(community, USER_ID, 10)
    await seed(community, USER_ID + 1, 10)
    await seed(community, USER_ID + 2, 11)
    await seed(community, USER_ID + 3, 12, attempts=6)
    board = await community.leaderboard(USER_ID)
    assert [row["rank"] for row in board["rows"]] == [1, 2, 2, 3]
    assert board["rows"][0]["played"] == 11
    assert [row["is_self"] for row in board["rows"]] == [False, True, False, False]
    for row in board["rows"]:
        assert set(row) == {"rank", "name", "played", "score", "is_self"}


async def test_leaderboard_does_not_assign_active_words(
    community: WordliStorage,
) -> None:
    """Просмотр таблицы не меняет профили и не резервирует новые ответы."""
    await seed(community, USER_ID, 10)
    async with community._database.connect() as connection:
        before = (await connection.execute(select(profiles_table))).mappings().one()
    await community.leaderboard(USER_ID)
    async with community._database.connect() as connection:
        after = (await connection.execute(select(profiles_table))).mappings().one()
    assert dict(after) == dict(before)


@pytest.mark.parametrize("position", [0, 9, 10, 13, 14])
async def test_leaderboard_window_uses_participants_not_dense_rank(
    community: WordliStorage,
    position: int,
) -> None:
    """Одинаковое место не отменяет отсечку десяти участников."""
    for index in range(15):
        await seed(community, USER_ID + index, 10)
    board = await community.leaderboard(USER_ID + position)
    names = [row["name"] for row in board["rows"]]
    assert len(names) == len(set(names)) == 10
    assert all(row["rank"] == 1 for row in board["rows"])
    if position < 10:
        expected = list(range(10))
        assert board["separator_after"] is None
    else:
        start = position - (4 if position >= 13 else 2)
        expected = list(range(5)) + list(range(start, start + 5))
        assert board["separator_after"] == 5
    assert names == [f"Игрок {USER_ID + index}" for index in expected]
    if position >= 13:
        assert board["rows"][-1]["is_self"] is True


async def test_state_and_settings_use_signed_telegram_name(
    community: WordliStorage,
) -> None:
    """Имя обновляется только из проверенного Telegram-профиля."""
    status, result = await api(community, "/api/state")
    assert status == 200
    assert result["settings"]["reminder_enabled"] is False
    status, result = await api(
        community,
        "/api/settings",
        {"timezone": "Europe/Berlin", "reminder_enabled": True},
        auth=signed_auth(USER_ID, "Новое", "Имя"),
    )
    assert status == 200
    assert result["settings"] == {
        "timezone": "Europe/Berlin",
        "reminder_enabled": True,
    }
    async with community._database.connect() as connection:
        data = await connection.scalar(select(settings_table.c.data))
    assert data["name"] == "Новое Имя"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Подделка"},
        {"user_id": USER_ID + 1},
        {"reminder_enabled": "true"},
        {"reminder_enabled": None},
        {"timezone": None},
        {"timezone": 4},
    ],
)
async def test_settings_api_rejects_untrusted_fields_and_types(
    community: WordliStorage,
    payload: dict,
) -> None:
    """Нельзя подменить имя, владельца или тип значения настройки."""
    status, result = await api(community, "/api/settings", payload)
    assert status == 400
    assert "error" in result


@pytest.mark.parametrize("path", ["/api/settings", "/api/leaderboard"])
async def test_community_api_requires_authentication(
    community: WordliStorage,
    path: str,
) -> None:
    """Новые маршруты не обходят проверку подписи Telegram."""
    status, result = await api(community, path, auth="not_a_valid_signature")
    assert status == 401
    assert result["error"]["code"] == "authentication_failed"


async def test_leaderboard_api_returns_only_public_summary(
    community: WordliStorage,
) -> None:
    """Турнирная таблица не выдаёт истории, ответы и Telegram ID."""
    await seed(community, USER_ID, 10)
    status, result = await api(community, "/api/leaderboard")
    assert status == 200
    assert result["leaderboard"]["rows"][0]["name"] == "Иван Тестовый"
    assert set(result) == {"leaderboard", "server_time", "storage"}
    row = result["leaderboard"]["rows"][0]
    assert not {"user_id", "history", "answer", "guesses"} & row.keys()
