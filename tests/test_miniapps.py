"""Локальные проверки мини-приложений без Telegram и базы данных."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError

from database import orm_games
from handlers import miniapp


@pytest.fixture(autouse=True)
def plain_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Исключает зависимость тестов от активной локали."""
    monkeypatch.setattr(miniapp, "_", lambda text: text)


def _message(raw_data: str, username: str | None = "player") -> SimpleNamespace:
    """Создаёт сообщение без сетевого клиента."""
    return SimpleNamespace(
        web_app_data=SimpleNamespace(data=raw_data),
        from_user=SimpleNamespace(id=123, username=username),
        answer=AsyncMock(),
    )


@pytest.mark.parametrize("game,score", [
    ("snake", 0), ("snake", 2220), ("platform", 0), ("platform", 400)
])
def test_results_are_saved(
    monkeypatch: pytest.MonkeyPatch, game: str, score: int
) -> None:
    """Обе игры сохраняют допустимые результаты."""
    save = AsyncMock()
    monkeypatch.setattr(miniapp, "orm_add_game", save)
    message = _message(json.dumps({
        "action": "game_end", "game": game, "score": score
    }))
    session = object()

    asyncio.run(miniapp.handle_web_app_data(message, session))

    save.assert_awaited_once_with(session, {
        "game_name": game,
        "user_id": 123,
        "user_name": "player",
        "score": score,
    })
    message.answer.assert_awaited_once()


def test_result_without_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отсутствующий username заменяется устойчивым идентификатором."""
    save = AsyncMock()
    monkeypatch.setattr(miniapp, "orm_add_game", save)
    message = _message(
        '{"action":"game_end","game":"snake","score":10}', None
    )

    asyncio.run(miniapp.handle_web_app_data(message, object()))

    assert save.await_args.args[1]["user_name"] == "id_123"


@pytest.mark.parametrize("raw_data", [
    "not json",
    "[]",
    "null",
    '{"action":"game_start","game":"snake","score":0}',
    '{"action":"game_end","game":"unknown","score":0}',
    '{"action":"game_end","game":[],"score":0}',
    '{"action":"game_end","game":"snake"}',
    '{"action":"game_end","game":"snake","score":true}',
    '{"action":"game_end","game":"snake","score":"10"}',
    '{"action":"game_end","game":"snake","score":-10}',
    '{"action":"game_end","game":"snake","score":1}',
    '{"action":"game_end","game":"snake","score":2230}',
    '{"action":"game_end","game":"platform","score":410}',
    '{"action":"game_end","game":"platform","score":10.5}',
])
def test_invalid_results_are_rejected(
    monkeypatch: pytest.MonkeyPatch, raw_data: str
) -> None:
    """Некорректные результаты не достигают ORM."""
    save = AsyncMock()
    monkeypatch.setattr(miniapp, "orm_add_game", save)
    message = _message(raw_data)

    asyncio.run(miniapp.handle_web_app_data(message, object()))

    save.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        "Не удалось принять результат игры."
    )


@pytest.mark.parametrize("category,slugs", [
    ("games", ["platform_game", "snake_game"]),
    ("services", ["pizza_calculator", "random_generator"]),
])
def test_reply_buttons_use_configured_test_url(
    category: str, slugs: list[str]
) -> None:
    """WebApp запускаются обычными кнопками по отдельному тестовому адресу."""
    bot = SimpleNamespace(webapp_base_url="https://test.example/miniapps/")

    markup = miniapp._webapp_keyboard(bot, category)

    assert isinstance(markup, ReplyKeyboardMarkup)
    assert [button.web_app.url for button in markup.keyboard[0]] == [
        f"https://test.example/miniapps/{slug}/" for slug in slugs
    ]
    assert markup.keyboard[1][0].text == "/mini"
    assert markup.keyboard[2][0].text == "Назад на главную ↩️"


@pytest.mark.parametrize("base_url", [
    "", "http://test.example", "https://", "https://user:pass@test.example",
    "https://test.example/?token=x", "https://test.example/#fragment",
])
def test_missing_or_invalid_webapp_url_is_disabled(base_url: str) -> None:
    """Ошибочная настройка не подменяется боевым адресом."""
    bot = SimpleNamespace(webapp_base_url=base_url)

    assert miniapp._webapp_keyboard(bot, "games") is None


def test_explicit_webapp_url_overrides_bot_attribute() -> None:
    """Настройка диспетчера имеет приоритет перед атрибутом бота."""
    bot = SimpleNamespace(webapp_base_url="https://old.example")

    markup = miniapp._webapp_keyboard(bot, "games", "https://test.example")

    assert markup.keyboard[0][0].web_app.url.startswith("https://test.example/")
    assert miniapp._webapp_keyboard(bot, "games", "") is None


def test_category_callback_sends_reply_keyboard() -> None:
    """Переход из inline-меню показывает обычную клавиатуру."""
    callback = Mock(spec=CallbackQuery)
    callback.bot = SimpleNamespace(webapp_base_url="https://test.example")
    callback.answer = AsyncMock()
    callback.message = Mock(spec=Message)
    callback.message.answer = AsyncMock()

    asyncio.run(miniapp.cmd_callback_game(callback))

    callback.answer.assert_awaited_once()
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)


def test_category_without_config_is_explained() -> None:
    """Ненастроенный тестовый адрес не открывает боевые страницы."""
    callback = Mock(spec=CallbackQuery)
    callback.bot = SimpleNamespace(webapp_base_url="")
    callback.answer = AsyncMock()
    callback.message = Mock(spec=Message)
    callback.message.answer = AsyncMock()

    asyncio.run(miniapp.cmd_callback_game(callback))

    callback.message.answer.assert_awaited_once_with(
        "Мини-приложения не настроены для этого бота."
    )


def test_failed_game_commit_rolls_back() -> None:
    """Ошибка записи не оставляет сессию в сломанной транзакции."""
    session = SimpleNamespace(
        add=Mock(), commit=AsyncMock(side_effect=SQLAlchemyError("test")),
        rollback=AsyncMock(),
    )
    data = {"user_id": 123, "user_name": "player", "game_name": "snake", "score": 0}

    with pytest.raises(SQLAlchemyError):
        asyncio.run(orm_games.orm_add_game(session, data))

    session.rollback.assert_awaited_once()


def test_error_handler_accepts_aiogram_error_event() -> None:
    """Обработчик использует одно событие ошибки aiogram 3."""
    message = SimpleNamespace(answer=AsyncMock())
    event = SimpleNamespace(
        exception=RuntimeError("test"),
        update=SimpleNamespace(message=message, callback_query=None),
    )

    assert asyncio.run(miniapp.error_handler(event)) is True
    message.answer.assert_awaited_once()
