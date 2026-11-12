"""Смена языка проходит через настоящий диспетчер и изолированную БД в памяти."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage, SendPhoto
from aiogram.methods.base import TelegramMethod
from aiogram.types import Message, Update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import create_bot, create_dispatcher
from config_data.config import Config, DataBase, Redis, TgBot
from database.models import Base
from database.orm_users import orm_add_user, orm_get_locale
from handlers import donate, other, private, start
from middlewares.throttle import ThrottleMiddleware

Harness = tuple[
    Dispatcher,
    Bot,
    async_sessionmaker[AsyncSession],
    list[TelegramMethod],
]


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    """Подменяет весь транспорт, не используя файлы, секреты или серверную БД."""
    config = Config(
        tg_bot=TgBot(token="", token_test="123456:OFFLINE_I18N_TEST"),
        db=DataBase(),
        redis=Redis(),
        database_url="sqlite+aiosqlite:///:memory:",
    )
    engine = create_async_engine(config.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    bot = create_bot(config)
    requests: list[TelegramMethod] = []

    async def send(
        active_bot: Bot,
        method: TelegramMethod,
        **kwargs: object,
    ) -> Message | bool:
        """Возвращает синтетический результат вместо запроса Telegram."""
        requests.append(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message.model_validate(
                {
                    "message_id": len(requests),
                    "date": datetime.now(timezone.utc),
                    "chat": {"id": int(method.chat_id), "type": "private"},
                    "text": getattr(method, "text", None),
                    "caption": getattr(method, "caption", None),
                }
            )
        return True

    monkeypatch.setattr(Bot, "__call__", send)
    dispatcher = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        dispatcher = create_dispatcher(
            config,
            sessions,
            routers=[
                start.start_router,
                private.private_router,
                donate.donate_router,
                other.other_router,
            ],
        )
        for middleware in dispatcher.update.outer_middleware:
            if isinstance(middleware, ThrottleMiddleware):
                middleware.ttl = 0
        yield dispatcher, bot, sessions, requests
    finally:
        if dispatcher is not None:
            await dispatcher.emit_shutdown()
            for router in dispatcher.sub_routers:
                router._parent_router = None
            dispatcher.sub_routers.clear()
        await bot.session.close()
        await engine.dispose()


async def _register(
    sessions: async_sessionmaker[AsyncSession],
    user_id: int,
    locale: str,
) -> None:
    """Регистрирует только синтетического участника в SQLite в памяти."""
    async with sessions() as session:
        await orm_add_user(
            session,
            {
                "user_id": user_id,
                "user_name": "synthetic",
                "full_name": "Synthetic User",
                "locale": locale,
            },
        )


def _actor(user_id: int, language: str = "ru") -> dict:
    """Создаёт синтетического участника с независимым языком Telegram."""
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": "Synthetic",
        "language_code": language,
    }


def _callback(user_id: int, target: str, number: int) -> Update:
    """Создаёт callback с языка уже открытой панели."""
    return Update.model_validate(
        {
            "update_id": number,
            "callback_query": {
                "id": f"synthetic-{number}",
                "chat_instance": "offline",
                "data": f"locale_{target}",
                "from": _actor(user_id),
                "message": {
                    "message_id": number,
                    "date": datetime.now(timezone.utc),
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": 123456, "is_bot": True, "first_name": "Bot"},
                    "text": "Language menu",
                },
            },
        }
    )


def _command(user_id: int, command: str, number: int, language: str = "ru") -> Update:
    """Создаёт обычную команду после смены или потери кэша языка."""
    return Update.model_validate(
        {
            "update_id": number,
            "message": {
                "message_id": number,
                "date": datetime.now(timezone.utc),
                "chat": {"id": user_id, "type": "private"},
                "from": _actor(user_id, language),
                "text": command,
                "entities": [
                    {"type": "bot_command", "offset": 0, "length": len(command)}
                ],
            },
        }
    )


def _game_button(request: SendMessage) -> str:
    """Извлекает последнюю кнопку общей главной панели."""
    return request.reply_markup.keyboard[1][1].text


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_support_from_info_deletes_only_clicked_card_in_selected_language(
    harness: Harness,
    locale: str,
) -> None:
    """Реальный диспетчер заменяет карточку info и сохраняет выбранный язык."""
    dispatcher, bot, sessions, requests = harness
    user_id = 303
    await _register(sessions, user_id, locale)
    # Язык Telegram специально противоположен сохранённому выбору пользователя.
    telegram_language = "en" if locale == "ru" else "ru"
    await dispatcher.feed_update(
        bot,
        _command(user_id, "/info", 700, language=telegram_language),
    )
    state = dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)
    clicked_id = (await state.get_data())["last_message_id"]
    await state.update_data(last_message_id=9999)
    callback = Update.model_validate(
        {
            "update_id": 701,
            "callback_query": {
                "id": "offline-support-transition",
                "chat_instance": "offline",
                "data": "donate",
                "from": _actor(user_id, telegram_language),
                "message": {
                    "message_id": clicked_id,
                    "date": datetime.now(timezone.utc),
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": 123456, "is_bot": True, "first_name": "Bot"},
                    "text": "Info panel",
                },
            },
        }
    )
    previous = len(requests)
    await dispatcher.feed_update(bot, callback)
    transition = requests[previous:]
    deleted = [call for call in transition if isinstance(call, DeleteMessage)]
    assert len(deleted) == 1
    assert deleted[0].message_id == clicked_id and deleted[0].chat_id == user_id
    messages = [call for call in transition if isinstance(call, SendMessage)]
    assert len(messages) == 1
    assert messages[0].text == (
        "Поддержать автора донатом"
        if locale == "ru"
        else "Support the author with a donation"
    )
    buttons = [
        button.text
        for row in messages[0].reply_markup.inline_keyboard
        for button in row
    ]
    assert buttons == [
        "10 ⭐️",
        "50 ⭐️",
        "100 ⭐️",
        "другое" if locale == "ru" else "other",
        "Назад на главную ↩️" if locale == "ru" else "Back to main ↩️",
    ]
    assert (
        len([call for call in transition if isinstance(call, AnswerCallbackQuery)]) == 1
    )
    assert await state.get_state() == donate.Donate.donate_input.state
    assert (await state.get_data())["locale"] == locale
    async with sessions() as session:
        assert await orm_get_locale(session, user_id) == locale


@pytest.mark.parametrize("mode", ["test", "verified_production", "disabled"])
async def test_switch_ru_en_ru_changes_current_callback_and_next_action(
    harness: Harness,
    mode: str,
) -> None:
    """Подтверждение, меню, БД и следующий апдейт получают один выбранный язык."""
    dispatcher, bot, sessions, requests = harness
    bot.test_mode = mode == "test"
    bot.wordli_enabled_verified = mode == "verified_production"
    bot.wordli_webapp_url = "" if mode == "disabled" else "https://example.invalid/game"
    await _register(sessions, 101, "ru")
    state = dispatcher.fsm.get_context(bot=bot, chat_id=101, user_id=101)
    await state.set_data({"locale": "ru"})
    for number, target in [(10, "en"), (20, "ru")]:
        previous = len(requests)
        await dispatcher.feed_update(bot, _callback(101, target, number))
        calls = requests[previous:]
        popup = next(call for call in calls if isinstance(call, AnswerCallbackQuery))
        header = next(call for call in calls if isinstance(call, SendMessage))
        assert popup.text == (
            "Selected: 🇺🇸 English" if target == "en" else "Выбран: 🇷🇺 Русский язык"
        )
        assert header.text == (
            "Current language \n\n 🇺🇸 English"
            if target == "en"
            else "Текущий язык \n\n 🇷🇺 Русский"
        )
        expected_button = (
            ("Words 🧩" if target == "en" else "Слова 🧩")
            if mode == "disabled"
            else ("Games 🕹" if target == "en" else "Игры 🕹")
        )
        assert _game_button(header) == expected_button
        assert (await state.get_data())["locale"] == target
        async with sessions() as session:
            assert await orm_get_locale(session, 101) == target
        await dispatcher.feed_update(bot, _command(101, "/main", number + 1))
        main = next(
            call for call in reversed(requests) if isinstance(call, SendMessage)
        )
        assert main.text == ("Main panel" if target == "en" else "Главная панель")
        assert _game_button(main) == expected_button


async def test_users_keep_independent_locales_and_restore_from_database(
    harness: Harness,
) -> None:
    """Смена языка одного участника не меняет другого и переживает потерю FSM."""
    dispatcher, bot, sessions, requests = harness
    bot.wordli_webapp_url = "https://example.invalid/game"
    await _register(sessions, 101, "ru")
    await _register(sessions, 202, "ru")
    first = dispatcher.fsm.get_context(bot=bot, chat_id=101, user_id=101)
    second = dispatcher.fsm.get_context(bot=bot, chat_id=202, user_id=202)
    await first.set_data({"locale": "ru"})
    await second.set_data({"locale": "ru"})
    await dispatcher.feed_update(bot, _callback(101, "en", 10))
    assert (await second.get_data())["locale"] == "ru"
    await first.clear()
    await dispatcher.feed_update(bot, _command(202, "/main", 11))
    await dispatcher.feed_update(bot, _command(101, "/main", 12))
    main = [call for call in requests if isinstance(call, SendMessage)][-2:]
    assert [call.text for call in main] == ["Главная панель", "Main panel"]
    assert [_game_button(call) for call in main] == ["Игры 🕹", "Games 🕹"]
    assert (await first.get_data())["locale"] == "en"
    async with sessions() as session:
        assert await orm_get_locale(session, 101) == "en"
        assert await orm_get_locale(session, 202) == "ru"


@pytest.mark.parametrize("target", ["de", "", "en_extra", "../ru"])
async def test_invalid_language_does_not_write_or_replace_keyboard(
    harness: Harness,
    target: str,
) -> None:
    """Поддельный callback не меняет сохранённый язык или главное меню."""
    dispatcher, bot, sessions, requests = harness
    await _register(sessions, 101, "ru")
    state = dispatcher.fsm.get_context(bot=bot, chat_id=101, user_id=101)
    await state.set_data({"locale": "ru", "preserved": 42})
    await dispatcher.feed_update(bot, _callback(101, target, 10))
    assert len(requests) == 1
    assert isinstance(requests[0], AnswerCallbackQuery)
    assert requests[0].text == "Язык не поддерживается."
    assert requests[0].show_alert is True
    assert await state.get_data() == {"locale": "ru", "preserved": 42}
    async with sessions() as session:
        assert await orm_get_locale(session, 101) == "ru"


async def test_start_corrects_pinned_context_without_overwriting_saved_locale(
    harness: Harness,
) -> None:
    """Даже старый RU-кэш не меняет выбранный EN и первый ответ /start."""
    dispatcher, bot, sessions, requests = harness
    bot.wordli_webapp_url = "https://example.invalid/game"
    await _register(sessions, 101, "en")
    state = dispatcher.fsm.get_context(bot=bot, chat_id=101, user_id=101)
    await state.set_state("Synthetic:old")
    await state.set_data({"locale": "ru", "old_value": 42})
    await dispatcher.feed_update(bot, _command(101, "/start", 10, language="ru"))
    main = next(call for call in requests if isinstance(call, SendMessage))
    assert main.text == "Bot activated!"
    assert _game_button(main) == "Games 🕹"
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": "en"}
    async with sessions() as session:
        assert await orm_get_locale(session, 101) == "en"
    assert not any(isinstance(call, SendPhoto) for call in requests)


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_first_start_uses_supported_telegram_language(
    harness: Harness,
    language: str,
) -> None:
    """Первое приветствие и новая регистрация используют один язык."""
    dispatcher, bot, sessions, requests = harness
    bot.wordli_webapp_url = "https://example.invalid/game"
    await dispatcher.feed_update(bot, _command(101, "/start", 10, language))
    main = next(call for call in requests if isinstance(call, SendMessage))
    assert main.text == ("Bot activated!" if language == "en" else "Бот активирован!")
    assert _game_button(main) == ("Games 🕹" if language == "en" else "Игры 🕹")
    photo = next(call for call in requests if isinstance(call, SendPhoto))
    assert photo.caption.startswith("Hello" if language == "en" else "Привет")
    async with sessions() as session:
        assert await orm_get_locale(session, 101) == language


async def test_status_error_is_localized_and_does_not_expose_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сведения об ошибке процесса не попадают в пользовательский текст."""

    def fail() -> None:
        """Имитирует отказ системного счётчика."""
        raise RuntimeError("PRIVATE_PROCESS_DETAIL")

    monkeypatch.setattr(other.psutil, "Process", fail)
    monkeypatch.setattr(other, "_", lambda text: text)
    text = await other._status_text()
    assert "Не удалось получить данные." in text
    assert "PRIVATE_PROCESS_DETAIL" not in text


async def test_switch_language_survives_command_menu_api_failure(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой обновления команд не откатывает язык, подтверждение или меню игр."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SetMyCommands

    dispatcher, bot, sessions, requests = harness
    bot.wordli_webapp_url = "https://example.invalid/game"
    await _register(sessions, 101, "ru")
    state = dispatcher.fsm.get_context(bot=bot, chat_id=101, user_id=101)
    await state.set_data({"locale": "ru"})
    original_send = Bot.__call__
    failed = []

    async def fail_commands(
        active_bot: Bot, method: TelegramMethod, **kwargs: object
    ) -> Message | bool:
        """Возвращает ошибку только для меню команд без настоящего Telegram."""
        if isinstance(method, SetMyCommands):
            failed.append(method)
            raise TelegramBadRequest(method=method, message="PRIVATE_API_DETAIL")
        return await original_send(active_bot, method, **kwargs)

    monkeypatch.setattr(Bot, "__call__", fail_commands)
    await dispatcher.feed_update(bot, _callback(101, "en", 10))
    main = next(call for call in requests if isinstance(call, SendMessage))
    assert main.text == "Current language \n\n 🇺🇸 English"
    assert _game_button(main) == "Games 🕹"
    assert (await state.get_data())["locale"] == "en"
    async with sessions() as session:
        assert await orm_get_locale(session, 101) == "en"
    assert len(failed) == 1
    assert failed[0].scope.chat_id == 101
    assert "PRIVATE_API_DETAIL" not in caplog.text


async def test_concurrent_language_contexts_do_not_mix_in_public_responses(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Переплетённые запросы двух участников не меняют язык друг друга."""
    import asyncio

    dispatcher, bot, sessions, requests = harness
    await _register(sessions, 101, "en")
    await _register(sessions, 202, "ru")
    for user_id, locale in [(101, "en"), (202, "ru")]:
        state = dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)
        await state.set_data({"locale": locale})
    original_send = Bot.__call__
    both_waiting = asyncio.Event()
    arrivals = []

    async def interleave(
        active_bot: Bot, method: TelegramMethod, **kwargs: object
    ) -> Message | bool:
        """Синхронизирует первые ответы, заставляя обработчики продолжиться вперемешку."""
        if isinstance(method, SendMessage) and method.text in {
            "Information",
            "Информация",
        }:
            arrivals.append(method.chat_id)
            if len(arrivals) == 2:
                both_waiting.set()
            await asyncio.wait_for(both_waiting.wait(), timeout=3)
        return await original_send(active_bot, method, **kwargs)

    monkeypatch.setattr(Bot, "__call__", interleave)
    await asyncio.gather(
        dispatcher.feed_update(bot, _command(101, "/info", 11)),
        dispatcher.feed_update(bot, _command(202, "/info", 12)),
    )
    for user_id, expected in [(101, "Bot status"), (202, "Состояние бота")]:
        texts = [
            call.text
            for call in requests
            if isinstance(call, SendMessage) and call.chat_id == user_id
        ]
        assert expected in texts[-1]
