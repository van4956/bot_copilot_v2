"""Командное меню, заглушка слов и отключение входов мини-приложений."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import CallbackQuery, Message, Update
from aiogram.utils.i18n import I18n
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import create_bot, create_dispatcher
from common import keyboard
from common.comands import admin_command, private_command
from config_data.config import ROOT, Config, DataBase, Redis, TgBot
from database.models import Base
from handlers import miniapp, other
from middlewares.throttle import ThrottleMiddleware

USER_ID = 9000000001


@pytest_asyncio.fixture
async def ui_dispatcher() -> AsyncIterator[tuple[Dispatcher, Bot]]:
    """Собирает настоящий диспетчер только с SQLite в памяти."""
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_TEST_ONLY"),
        db=DataBase(),
        redis=Redis(),
        database_url="sqlite+aiosqlite:///:memory:",
    )
    engine = create_async_engine(config.database_url)
    bot = create_bot(config)
    dispatcher = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        dispatcher = create_dispatcher(config, async_sessionmaker(engine))
        for middleware in dispatcher.update.outer_middleware:
            if isinstance(middleware, ThrottleMiddleware):
                middleware.ttl = 0
        yield dispatcher, bot
    finally:
        if dispatcher is not None:
            await dispatcher.emit_shutdown()
            for router in dispatcher.sub_routers:
                router._parent_router = None
            dispatcher.sub_routers.clear()
        await engine.dispose()
        await bot.session.close()


def _message(**fields) -> dict:
    """Создаёт поля личного сообщения без настоящего пользователя."""
    return {
        "message_id": 1,
        "date": datetime.now(timezone.utc),
        "chat": {"id": USER_ID, "type": "private"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Synthetic"},
        **fields,
    }


@pytest.mark.parametrize("commands", [private_command, admin_command])
def test_menu_has_book_instead_of_mini(commands: list) -> None:
    """Оба меню содержат прежние команды и новый вход cookbook."""
    assert [(item.command, item.description) for item in commands] == [
        ("main", "main pannel"),
        ("book", "cookbook"),
        ("info", "settings"),
    ]


@pytest.mark.parametrize("language,word", [("ru", "Слова 🧩"), ("en", "Words 🧩")])
def test_words_replace_book_button(language: str, word: str) -> None:
    """Основная клавиатура сохраняет расположение кнопок в двух языках."""
    i18n = I18n(path=str(ROOT / "locales"), domain="bot_00_template")
    with i18n.context(), i18n.use_locale(language):
        markup = keyboard.start_keyboard()
    assert [len(row) for row in markup.keyboard] == [2, 2]
    assert markup.keyboard[1][1].text == word


@pytest.mark.parametrize(
    "language,text,expected",
    [
        ("ru", "Слова 🧩", "Данная функция в разработке"),
        ("en", "Words 🧩", "This feature is under development"),
    ],
)
async def test_words_button_opens_only_placeholder(
    ui_dispatcher: tuple[Dispatcher, Bot],
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    text: str,
    expected: str,
) -> None:
    """Кнопка завершает старый диалог и показывает только заглушку."""
    dispatcher, bot = ui_dispatcher
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
    await state.set_state("Currency:amount")
    await state.set_data({"locale": language, "user_value": "12"})

    await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "message": _message(text=text),
            }
        ),
    )

    assert answer.await_count == 1
    assert answer.await_args.args[0] == expected
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": language}


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_text_amount_uses_registered_currency_handler(
    ui_dispatcher: tuple[Dispatcher, Bot],
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    """Текст проходит полный путь middleware и подтверждает введённую сумму."""
    dispatcher, bot = ui_dispatcher
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
    await state.set_state("Currency:amount")
    await state.set_data({"locale": language, "user_value": "1"})

    await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "message": _message(text="12.50"),
            }
        ),
    )

    assert await state.get_state() == "Currency:base_currency"
    assert (await state.get_data())["currency_amount"] == "12.50"
    assert (await state.get_data())["locale"] == language
    assert "12.50" in answer.await_args.args[0]
    assert answer.await_args.kwargs["reply_markup"].inline_keyboard


async def test_mini_command_and_routers_are_disabled(
    ui_dispatcher: tuple[Dispatcher, Bot],
) -> None:
    """Команда не открывает раздел, его сохранённые роутеры не подключены."""
    dispatcher, bot = ui_dispatcher
    assert miniapp.miniapp_router not in dispatcher.sub_routers
    assert miniapp.webapp_data_router not in dispatcher.sub_routers
    result = await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "message": _message(
                    text="/mini",
                    entities=[
                        {
                            "type": "bot_command",
                            "offset": 0,
                            "length": 5,
                        }
                    ],
                ),
            }
        ),
    )
    assert result is UNHANDLED


@pytest.mark.parametrize(
    "data",
    [
        "service_miniapp",
        "game_miniapp",
        "back_to_mini",
        "mini_back_to_main",
    ],
)
async def test_old_miniapp_buttons_are_acknowledged_but_not_opened(
    ui_dispatcher: tuple[Dispatcher, Bot],
    monkeypatch: pytest.MonkeyPatch,
    data: str,
) -> None:
    """Старые кнопки не запускают WebApp и не меняют незавершённый диалог."""
    dispatcher, bot = ui_dispatcher
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
    await state.set_state("Currency:amount")
    await state.set_data({"locale": "ru", "user_value": "12"})

    await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "synthetic",
                    "chat_instance": "review",
                    "data": data,
                    "from": _message()["from"],
                    "message": _message(text="Old menu"),
                },
            }
        ),
    )

    answer.assert_awaited_once_with(
        "Мини-приложения временно недоступны", show_alert=True
    )
    assert await state.get_state() == "Currency:amount"
    assert await state.get_data() == {"locale": "ru", "user_value": "12"}


async def test_old_game_result_does_not_reach_currency_or_database(
    ui_dispatcher: tuple[Dispatcher, Bot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Возврат из старой игры не записывается и не принимается как сумма."""
    dispatcher, bot = ui_dispatcher
    answer, save = AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(miniapp, "orm_add_game", save)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
    await state.set_state("Currency:amount")
    await state.set_data({"locale": "ru", "user_value": "12"})

    await dispatcher.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 1,
                "message": _message(
                    web_app_data={
                        "data": '{"action":"game_end","game":"snake","score":10}',
                        "button_text": "Snake",
                    }
                ),
            }
        ),
    )

    answer.assert_awaited_once_with("Мини-приложения временно недоступны")
    save.assert_not_awaited()
    assert await state.get_state() == "Currency:amount"
    assert await state.get_data() == {"locale": "ru", "user_value": "12"}


@pytest.mark.parametrize("language,word", [("ru", "Слова 🧩"), ("en", "Words 🧩")])
async def test_changing_language_does_not_restore_old_book_button(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    word: str,
) -> None:
    """Смена языка использует тот же новый набор кнопок."""
    from types import SimpleNamespace

    callback = SimpleNamespace(
        data=f"locale_{language}",
        from_user=SimpleNamespace(id=USER_ID),
        message=SimpleNamespace(delete=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(update_data=AsyncMock())
    monkeypatch.setattr(other, "orm_update_locale", AsyncMock())

    await other.update_locale_cmd(callback, object(), state, {})

    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert markup.keyboard[1][1].text == word
