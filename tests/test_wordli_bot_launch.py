"""Тестовый вход Wordli без Telegram, секретов и подключений к БД."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from aiogram.types import Message, ReplyKeyboardRemove

import app
from config_data import config as settings
from handlers import navigation

FAKE_TOKEN = "123456:synthetic_test_token_not_a_real_credential"
TEST_URL = "https://wordli-test.example/game/"


@pytest.fixture
def settings_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Заменяет окружение и файл настроек искусственными значениями."""
    values = {"APP_ENV": "test", "BOT_TOKEN_TEST": FAKE_TOKEN}
    monkeypatch.setattr(settings.os, "environ", values)
    monkeypatch.setattr(settings, "dotenv_values", Mock(return_value={}))
    return values


def test_wordli_is_disabled_by_default(
    settings_environment: dict[str, str],
) -> None:
    """Обычный тестовый запуск сохраняет прежнюю заглушку."""
    assert settings.load_config().wordli_webapp_url == ""


def test_wordli_accepts_separate_https_url(
    settings_environment: dict[str, str],
) -> None:
    """Адрес игры отделён от настроек прежних мини-приложений."""
    settings_environment["WORDLI_WEBAPP_URL"] = TEST_URL
    config = settings.load_config()
    assert config.wordli_webapp_url == TEST_URL
    assert config.webapp_base_url == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://wordli-test.example/",
        "https:///game/",
        "https://user@wordli-test.example/",
        "https://user:secret@wordli-test.example/",
        "https://wordli-test.example/?mode=test",
        "https://wordli-test.example/?",
        "https://wordli-test.example/#fragment",
        "https://wordli-test.example/#",
        "https://wordli-test.example:bad-port/",
        "https://wordli-test.example:70000/",
        "https://wordli-test.example\\other/",
        " https://wordli-test.example/",
        "https://wordli-test.example/game path/",
        settings.PRODUCTION_WEBAPP_URL + "/wordli/",
    ],
)
def test_wordli_rejects_unsafe_test_url(
    settings_environment: dict[str, str], url: str
) -> None:
    """Небезопасный адрес не активирует тестовый вход."""
    settings_environment["WORDLI_WEBAPP_URL"] = url
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        settings.load_config()


@pytest.mark.parametrize("url", [TEST_URL, "not-a-valid-url"])
def test_production_ignores_wordli_setting(
    settings_environment: dict[str, str], url: str
) -> None:
    """Даже заданный адрес не включает игру в боевой конфигурации."""
    settings_environment.update(
        APP_ENV="production",
        BOT_TOKEN=FAKE_TOKEN,
        DB_POST="postgresql+asyncpg://synthetic@unused.invalid/unused",
        WORDLI_WEBAPP_URL=url,
    )
    config = settings.load_config()
    assert config.test_mode is False
    assert config.wordli_webapp_url == ""


@pytest.mark.parametrize(
    "mode,expected", [("test", TEST_URL), ("production", "")]
)
def test_bot_receives_url_only_in_test_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    """Создание клиента не открывает сеть и не включает боевой вход."""
    config = settings.Config(
        tg_bot=settings.TgBot(token=FAKE_TOKEN, token_test=FAKE_TOKEN),
        db=settings.DataBase(),
        redis=settings.Redis(),
        mode=mode,
        wordli_webapp_url=TEST_URL,
    )
    fake_bot = SimpleNamespace()
    constructor = Mock(return_value=fake_bot)
    monkeypatch.setattr(app, "Bot", constructor)
    result = app.create_bot(config)
    assert result is fake_bot
    assert result.wordli_webapp_url == expected
    assert result.test_mode is (mode == "test")


@pytest.fixture
def words_state(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Изолирует перевод и создаёт незавершённый диалог."""
    monkeypatch.setattr(navigation, "_", lambda text: text)
    return SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": "ru", "amount": "12"}),
        set_state=AsyncMock(),
        set_data=AsyncMock(),
    )


@pytest.mark.parametrize("from_message", [False, True])
async def test_test_words_launches_inline_webapp(
    words_state: SimpleNamespace, from_message: bool
) -> None:
    """Тестовое меню предлагает готовую игру и отдельную заглушку."""
    bot = SimpleNamespace(test_mode=True, wordli_webapp_url=TEST_URL)
    message = SimpleNamespace(answer=AsyncMock(), bot=bot)
    if from_message:
        await navigation.open_words(message, words_state)
    else:
        await navigation.open_words(message, words_state, bot)
    assert message.answer.await_count == 2
    remove_call, invitation_call = message.answer.await_args_list
    assert remove_call.args == ("🕹",)
    assert remove_call.kwargs["parse_mode"] is None
    assert "entities" not in remove_call.kwargs
    assert "message_effect_id" not in remove_call.kwargs
    removal = remove_call.kwargs["reply_markup"]
    assert isinstance(removal, ReplyKeyboardRemove)
    assert removal.remove_keyboard is True
    assert invitation_call.args == ("В какую игру сыграем?",)
    assert "message_effect_id" not in invitation_call.kwargs
    markup = invitation_call.kwargs["reply_markup"]
    assert [len(row) for row in markup.inline_keyboard] == [2, 1]
    button = markup.inline_keyboard[0][0]
    assert button.text == "5БУКВ"
    assert button.web_app.url == TEST_URL
    assert button.callback_data is None
    dimensions = markup.inline_keyboard[0][1]
    assert dimensions.text == "Битва Измерений"
    assert dimensions.callback_data == "wordli_dimensions_stub"
    assert dimensions.web_app is None
    assert dimensions.url is None
    back = markup.inline_keyboard[1][0]
    assert back.text == "Назад на главную ↩️"
    assert back.callback_data == "wordli_back_to_main"
    assert back.web_app is None
    words_state.set_state.assert_awaited_once_with(None)
    words_state.set_data.assert_awaited_once_with({"locale": "ru"})


def _invitation_message() -> Message:
    """Создаёт только сообщение приглашения, отправленное самим ботом."""
    return Message.model_validate(
        {
            "message_id": 77,
            "date": datetime.now(timezone.utc),
            "chat": {"id": 9000000001, "type": "private"},
            "from": {
                "id": 123456,
                "is_bot": True,
                "first_name": "Synthetic bot",
            },
            "text": "В какую игру сыграем?",
        }
    )


@pytest.mark.parametrize("already_deleted", [False, True])
async def test_wordli_back_deletes_invitation_and_restores_main(
    monkeypatch: pytest.MonkeyPatch,
    words_state: SimpleNamespace,
    already_deleted: bool,
) -> None:
    """Возврат удаляет только приглашение и сохраняет язык диалога."""
    invitation = _invitation_message()
    assert invitation.from_user.is_bot is True
    callback = SimpleNamespace(message=invitation, answer=AsyncMock())
    bot = SimpleNamespace(test_mode=True, wordli_webapp_url=TEST_URL)
    deleted = AsyncMock()
    answered = AsyncMock()
    if already_deleted:
        deleted.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=9000000001, message_id=77),
            message="Bad Request: message to delete not found",
        )

    async def delete_invitation(target: Message) -> None:
        """Запоминает ID единственного удаляемого сообщения."""
        await deleted(target.message_id)

    markup = object()
    monkeypatch.setattr(Message, "delete", delete_invitation)
    monkeypatch.setattr(Message, "answer", answered)
    monkeypatch.setattr(navigation.private, "_", lambda text: text)
    monkeypatch.setattr(
        navigation.keyboard, "start_keyboard", lambda bot=None: markup
    )
    await navigation.back_from_wordli(callback, words_state, bot)
    callback.answer.assert_awaited_once_with()
    deleted.assert_awaited_once_with(77)
    answered.assert_awaited_once_with("Главная панель", reply_markup=markup)
    assert words_state.set_state.await_count == 2
    assert all(
        call.args == (None,)
        for call in words_state.set_state.await_args_list
    )
    words_state.set_data.assert_awaited_once_with({"locale": "ru"})


@pytest.mark.parametrize(
    "test_mode,url", [(False, TEST_URL), (True, ""), (None, TEST_URL)]
)
async def test_wordli_back_is_unavailable_without_test_feature(
    monkeypatch: pytest.MonkeyPatch,
    words_state: SimpleNamespace,
    test_mode: bool | None,
    url: str,
) -> None:
    """Старая кнопка не влияет на основной бот или выключенную игру."""
    invitation = _invitation_message()
    deleted, answered = AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "delete", deleted)
    monkeypatch.setattr(Message, "answer", answered)
    callback = SimpleNamespace(message=invitation, answer=AsyncMock())
    bot = SimpleNamespace(test_mode=test_mode, wordli_webapp_url=url)
    await navigation.back_from_wordli(callback, words_state, bot)
    callback.answer.assert_awaited_once_with(
        "Мини-приложения временно недоступны", show_alert=True
    )
    deleted.assert_not_awaited()
    answered.assert_not_awaited()
    words_state.set_state.assert_not_awaited()
    words_state.set_data.assert_not_awaited()


async def test_wordli_back_handles_missing_invitation(
    words_state: SimpleNamespace,
) -> None:
    """Недоступное сообщение не вызывает обращения к Telegram или FSM."""
    callback = SimpleNamespace(message=None, answer=AsyncMock())
    bot = SimpleNamespace(test_mode=True, wordli_webapp_url=TEST_URL)
    await navigation.back_from_wordli(callback, words_state, bot)
    callback.answer.assert_awaited_once_with(
        "Мини-приложения временно недоступны", show_alert=True
    )
    words_state.set_state.assert_not_awaited()


@pytest.mark.parametrize("from_message", [False, True])
async def test_dimensions_stub_shows_only_alert(
    monkeypatch: pytest.MonkeyPatch, from_message: bool
) -> None:
    """Заглушка показывает уведомление, не меняя сообщение меню."""
    monkeypatch.setattr(navigation, "_", lambda text: text)
    answered, deleted, edited = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answered)
    monkeypatch.setattr(Message, "delete", deleted)
    monkeypatch.setattr(Message, "edit_text", edited)
    bot = SimpleNamespace(test_mode=True, wordli_webapp_url=TEST_URL)
    callback = SimpleNamespace(
        message=_invitation_message(), answer=AsyncMock(), bot=bot
    )
    if from_message:
        await navigation.dimensions_stub(callback)
    else:
        await navigation.dimensions_stub(callback, bot)
    callback.answer.assert_awaited_once_with(
        "Игра в разработке", show_alert=True
    )
    answered.assert_not_awaited()
    deleted.assert_not_awaited()
    edited.assert_not_awaited()


@pytest.mark.parametrize(
    "test_mode,url,missing_message",
    [
        (False, TEST_URL, False),
        (None, TEST_URL, False),
        (True, "", False),
        (True, TEST_URL, True),
    ],
)
async def test_dimensions_stub_is_unavailable_outside_test_menu(
    monkeypatch: pytest.MonkeyPatch,
    test_mode: bool | None,
    url: str,
    missing_message: bool,
) -> None:
    """Старая кнопка не включает тестовую заглушку в боевом режиме."""
    monkeypatch.setattr(navigation, "_", lambda text: text)
    callback = SimpleNamespace(
        message=None if missing_message else _invitation_message(),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(test_mode=test_mode, wordli_webapp_url=url)
    await navigation.dimensions_stub(callback, bot)
    callback.answer.assert_awaited_once_with(
        "Мини-приложения временно недоступны", show_alert=True
    )


async def test_wordli_back_does_not_hide_unexpected_delete_error(
    monkeypatch: pytest.MonkeyPatch, words_state: SimpleNamespace
) -> None:
    """Посторонняя ошибка Telegram не маскируется как успешное удаление."""
    invitation = _invitation_message()
    callback = SimpleNamespace(message=invitation, answer=AsyncMock())
    bot = SimpleNamespace(test_mode=True, wordli_webapp_url=TEST_URL)
    deleted = AsyncMock(
        side_effect=TelegramBadRequest(
            method=DeleteMessage(chat_id=9000000001, message_id=77),
            message="Bad Request: unrelated synthetic failure",
        )
    )
    answered = AsyncMock()
    monkeypatch.setattr(Message, "delete", deleted)
    monkeypatch.setattr(Message, "answer", answered)
    with pytest.raises(TelegramBadRequest, match="unrelated"):
        await navigation.back_from_wordli(callback, words_state, bot)
    callback.answer.assert_awaited_once_with()
    answered.assert_not_awaited()
    words_state.set_state.assert_not_awaited()


@pytest.mark.parametrize(
    "test_mode,url",
    [(True, ""), (False, TEST_URL), (None, TEST_URL)],
)
async def test_unconfigured_or_production_words_keep_stub(
    monkeypatch: pytest.MonkeyPatch,
    words_state: SimpleNamespace,
    test_mode: bool | None,
    url: str,
) -> None:
    """Без тестового флага и адреса ответ полностью прежний."""
    markup = object()
    monkeypatch.setattr(
        navigation.keyboard, "start_keyboard", lambda bot=None: markup
    )
    message = SimpleNamespace(answer=AsyncMock())
    bot = SimpleNamespace(test_mode=test_mode, wordli_webapp_url=url)
    await navigation.open_words(message, words_state, bot)
    message.answer.assert_awaited_once_with(
        "Данная функция в разработке", reply_markup=markup
    )
    words_state.set_state.assert_awaited_once_with(None)
    words_state.set_data.assert_awaited_once_with({"locale": "ru"})


def _emoji_message(**fields: object) -> Message:
    """Создаёт искусственное личное сообщение с одним custom emoji."""
    return Message.model_validate(
        {
            "message_id": 91,
            "date": datetime.now(timezone.utc),
            "chat": {"id": 9000000001, "type": "private"},
            "from": {
                "id": 9000000001,
                "is_bot": False,
                "first_name": "Synthetic owner",
            },
            "text": " 🧩 ",
            "entities": [
                {
                    "type": "custom_emoji",
                    "offset": 1,
                    "length": 2,
                    "custom_emoji_id": "synthetic-emoji-id",
                }
            ],
            **fields,
        }
    )


@pytest.fixture
def emoji_capture_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Перенаправляет тестовую запись в отдельный временный каталог."""
    path = tmp_path / "emoji-capture" / "custom-emoji.json"
    monkeypatch.setattr(navigation, "WORDLI_EMOJI_CAPTURE_PATH", path)
    return path


@pytest.mark.parametrize("role", ["owner", "admin_list"])
async def test_custom_emoji_capture_is_private_silent_and_one_off(
    monkeypatch: pytest.MonkeyPatch, emoji_capture_path: Path, role: str
) -> None:
    """Сохраняются только ID и замена, без ответа и перезаписи."""
    bot = SimpleNamespace(
        test_mode=True, wordli_webapp_url=TEST_URL,
        **{role: [9000000001]},
    )
    message = _emoji_message()
    answered = AsyncMock()
    monkeypatch.setattr(Message, "answer", answered)
    assert navigation._can_capture_wordli_emoji(message, bot) is True
    await navigation.capture_wordli_emoji(message, bot)
    assert json.loads(emoji_capture_path.read_text(encoding="utf-8")) == {
        "custom_emoji_id": "synthetic-emoji-id", "fallback": "🧩"
    }
    before = emoji_capture_path.read_bytes()
    assert navigation._can_capture_wordli_emoji(message, bot) is False
    await navigation.capture_wordli_emoji(message, bot)
    assert emoji_capture_path.read_bytes() == before
    answered.assert_not_awaited()


async def test_custom_emoji_sticker_capture(
    emoji_capture_path: Path,
) -> None:
    """Отдельная отправка custom sticker также даёт ID и замену."""
    message = _emoji_message(
        text=None, entities=None,
        sticker={
            "file_id": "synthetic-file",
            "file_unique_id": "synthetic-unique-file",
            "type": "custom_emoji",
            "width": 100, "height": 100,
            "is_animated": False, "is_video": False,
            "custom_emoji_id": "synthetic-sticker-id", "emoji": "🧩",
        },
    )
    bot = SimpleNamespace(
        test_mode=True, wordli_webapp_url=TEST_URL, owner=[9000000001]
    )
    assert navigation._can_capture_wordli_emoji(message, bot) is True
    await navigation.capture_wordli_emoji(message, bot)
    assert json.loads(emoji_capture_path.read_text(encoding="utf-8")) == {
        "custom_emoji_id": "synthetic-sticker-id", "fallback": "🧩"
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"text": "ordinary text", "entities": None},
        {"text": "🧩", "entities": None},
        {"text": "🧩 ordinary text"},
        {"caption": "not an emoji-only message"},
        {
            "text": "🧩",
            "entities": [
                {"type": "custom_emoji", "offset": 0, "length": 1,
                 "custom_emoji_id": "synthetic-emoji-id"}
            ],
        },
        {
            "text": "🧩🧩",
            "entities": [
                {"type": "custom_emoji", "offset": 0, "length": 2,
                 "custom_emoji_id": "synthetic-first-id"},
                {"type": "custom_emoji", "offset": 2, "length": 2,
                 "custom_emoji_id": "synthetic-second-id"},
            ],
        },
        {"text": "🧩", "entities": [{"type": "bold", "offset": 0,
                                      "length": 2}]},
    ],
)
async def test_ordinary_or_multiple_emoji_messages_are_not_consumed(
    emoji_capture_path: Path, fields: dict[str, object]
) -> None:
    """Фильтр не забирает обычные сообщения или несколько эмодзи."""
    message = _emoji_message(**fields)
    bot = SimpleNamespace(
        test_mode=True, wordli_webapp_url=TEST_URL, owner=[9000000001]
    )
    assert navigation._can_capture_wordli_emoji(message, bot) is False
    await navigation.capture_wordli_emoji(message, bot)
    assert not emoji_capture_path.parent.exists()


@pytest.mark.parametrize(
    "mode,url,owner,user_id,chat_type",
    [
        (False, TEST_URL, [9000000001], 9000000001, "private"),
        (True, "", [9000000001], 9000000001, "private"),
        (True, TEST_URL, [], 9000000001, "private"),
        (True, TEST_URL, [9000000001], 9000000002, "private"),
        (True, TEST_URL, [9000000001], 9000000001, "group"),
    ],
)
async def test_custom_emoji_capture_cannot_write_for_other_bot_or_user(
    emoji_capture_path: Path,
    mode: bool,
    url: str,
    owner: list[int],
    user_id: int,
    chat_type: str,
) -> None:
    """Основной бот, посторонний пользователь и группа не создают файл."""
    message = _emoji_message(
        **{"from": {"id": user_id, "is_bot": False,
                    "first_name": "Synthetic"}},
        chat={"id": 9000000001, "type": chat_type},
    )
    bot = SimpleNamespace(
        test_mode=mode, wordli_webapp_url=url, owner=owner
    )
    assert navigation._can_capture_wordli_emoji(message, bot) is False
    await navigation.capture_wordli_emoji(message, bot)
    assert not emoji_capture_path.parent.exists()
