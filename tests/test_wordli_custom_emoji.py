"""Необязательный custom emoji Wordli без сети и настоящих настроек."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.utils.i18n import I18n

import app
from common import keyboard
from config_data import config as settings
from handlers import navigation

TEST_URL = "https://wordli-test.example/game/"
EMOJI_ID = "12345678901234567890"
FAKE_TOKEN = "123456:synthetic_test_token_not_a_real_credential"


@pytest.fixture
def custom_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Изолирует чтение конфигурации от настоящих файлов и окружения."""
    values = {"APP_ENV": "test", "BOT_TOKEN_TEST": FAKE_TOKEN}
    monkeypatch.setattr(settings.os, "environ", values)
    monkeypatch.setattr(settings, "dotenv_values", Mock(return_value={}))
    return values


def _bot(**fields: object) -> SimpleNamespace:
    """Создаёт только искусственные атрибуты тестового бота."""
    return SimpleNamespace(
        **{
            "test_mode": True,
            "wordli_webapp_url": TEST_URL,
            "wordli_custom_emoji_id": EMOJI_ID,
            "wordli_custom_emoji_fallback": "🧩",
            **fields,
        }
    )


def test_custom_emoji_configuration_is_off_by_default(
    custom_environment: dict[str, str],
) -> None:
    """Без ID подстановка выключена, даже если задана замена."""
    custom_environment["WORDLI_CUSTOM_EMOJI_FALLBACK"] = "🧩"
    config = settings.load_config()
    assert config.wordli_custom_emoji_id == ""
    assert config.wordli_custom_emoji_fallback == ""


def test_custom_emoji_configuration_has_default_fallback(
    custom_environment: dict[str, str],
) -> None:
    """Числовой ID включает стандартную замену без реальных секретов."""
    custom_environment["WORDLI_CUSTOM_EMOJI_ID"] = EMOJI_ID
    config = settings.load_config()
    assert config.wordli_custom_emoji_id == EMOJI_ID
    assert config.wordli_custom_emoji_fallback == "🧩"


@pytest.mark.parametrize("identifier", ["a", "-1", " 1", "١", "1" * 33])
def test_custom_emoji_configuration_rejects_non_numeric_id(
    custom_environment: dict[str, str], identifier: str
) -> None:
    """ID имеет ровно 1–32 обычные десятичные цифры."""
    custom_environment["WORDLI_CUSTOM_EMOJI_ID"] = identifier
    with pytest.raises(ValueError, match="WORDLI_CUSTOM_EMOJI_ID"):
        settings.load_config()


def test_custom_emoji_configuration_ignores_production_values(
    custom_environment: dict[str, str],
) -> None:
    """Production не активирует даже явно переданные настройки эмодзи."""
    custom_environment.update(
        APP_ENV="production", BOT_TOKEN=FAKE_TOKEN,
        DB_POST="postgresql+asyncpg://synthetic@unused.invalid/unused",
        WORDLI_CUSTOM_EMOJI_ID="invalid-but-ignored",
        WORDLI_CUSTOM_EMOJI_FALLBACK="🧩",
    )
    config = settings.load_config()
    assert config.wordli_custom_emoji_id == ""
    assert config.wordli_custom_emoji_fallback == ""


@pytest.mark.parametrize(
    "mode,url,enabled",
    [("test", TEST_URL, True), ("production", TEST_URL, False),
     ("test", "", False)],
)
def test_bot_attaches_custom_emoji_only_to_enabled_test_game(
    monkeypatch: pytest.MonkeyPatch, mode: str, url: str, enabled: bool
) -> None:
    """Создание клиента не открывает сеть и сохраняет тестовую границу."""
    config = settings.Config(
        tg_bot=settings.TgBot(token=FAKE_TOKEN, token_test=FAKE_TOKEN),
        db=settings.DataBase(), redis=settings.Redis(), mode=mode,
        wordli_webapp_url=url, wordli_custom_emoji_id=EMOJI_ID,
    )
    monkeypatch.setattr(app, "Bot", Mock(return_value=SimpleNamespace()))
    bot = app.create_bot(config)
    assert bot.wordli_custom_emoji_id == (EMOJI_ID if enabled else "")
    assert bot.wordli_custom_emoji_fallback == ("🧩" if enabled else "")


@pytest.mark.parametrize("language,label", [("ru", "Игры"), ("en", "Games")])
def test_keyboard_changes_only_words_to_standard_joystick(
    language: str, label: str
) -> None:
    """Старый custom ID не заменяет согласованный обычный джойстик."""
    i18n = I18n(path=str(settings.ROOT / "locales"), domain="bot_00_template")
    with i18n.context(), i18n.use_locale(language):
        default = keyboard.start_keyboard().model_dump(exclude_none=True)
        markup = keyboard.start_keyboard(bot=_bot())
    actual = json.loads(markup.model_dump_json(exclude_none=True))
    assert [len(row) for row in actual["keyboard"]] == [2, 2]
    assert actual["keyboard"][0] == default["keyboard"][0]
    assert actual["keyboard"][1][0] == default["keyboard"][1][0]
    assert actual["keyboard"][1][1] == {
        "text": f"{label} 🕹"
    }
    assert actual["resize_keyboard"] == default["resize_keyboard"]
    assert (
        actual["input_field_placeholder"] == default["input_field_placeholder"]
    )


@pytest.mark.parametrize(
    "fields",
    [{"test_mode": False}, {"wordli_webapp_url": ""}],
)
def test_default_or_production_keyboard_stays_identical(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, object]
) -> None:
    """Выключенный или боевой режим не меняет прежнюю клавиатуру."""
    monkeypatch.setattr(keyboard, "_", lambda text: text)
    expected = keyboard.start_keyboard().model_dump(exclude_none=True)
    actual = keyboard.start_keyboard(bot=_bot(**fields))
    assert actual.model_dump(exclude_none=True) == expected


@pytest.mark.parametrize(
    "language,label,games_label",
    [("ru", "Слова", "Игры"), ("en", "Words", "Games")],
)
async def test_words_filter_accepts_joystick_and_legacy_test_labels(
    language: str, label: str, games_label: str
) -> None:
    """Новые подписи принимаются только у настроенной тестовой игры."""
    i18n = I18n(path=str(settings.ROOT / "locales"), domain="bot_00_template")
    with i18n.context(), i18n.use_locale(language):
        plain = SimpleNamespace(text=label)
        legacy = SimpleNamespace(text=f"{label} 🧩")
        joystick = SimpleNamespace(text=f"{label} 🕹")
        games = SimpleNamespace(text=f"{games_label} 🕹")
        assert await navigation._words_button(plain, _bot()) is True
        assert await navigation._words_button(legacy, _bot()) is True
        assert await navigation._words_button(joystick, _bot()) is True
        assert await navigation._words_button(games, _bot()) is True
        assert await navigation._words_button(
            games, _bot(test_mode=False)
        ) is False
        assert await navigation._words_button(
            games, _bot(wordli_webapp_url="")
        ) is False
        assert await navigation._words_button(
            joystick, _bot(test_mode=False)
        ) is False
        assert await navigation._words_button(
            plain, _bot(test_mode=False)
        ) is False
        assert await navigation._words_button(
            legacy, _bot(test_mode=False)
        ) is True
        assert await navigation._words_button(
            plain, _bot(wordli_custom_emoji_id="")
        ) is True


@pytest.mark.parametrize("fallback", ["🧩", "🧑‍💻"])
async def test_invitation_ignores_obsolete_custom_emoji_and_effects(
    monkeypatch: pytest.MonkeyPatch, fallback: str
) -> None:
    """Приглашение использует обычный джойстик без сущностей и эффектов."""
    monkeypatch.setattr(navigation, "_", lambda text: text)
    monkeypatch.setattr(
        navigation, "get_i18n", lambda: SimpleNamespace(current_locale="ru"),
    )
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": "ru"}),
        set_state=AsyncMock(), set_data=AsyncMock(),
    )
    await navigation.open_words(
        message, state, _bot(wordli_custom_emoji_fallback=fallback)
    )
    assert message.answer.await_count == 2
    emoji_call = message.answer.await_args_list[0]
    assert emoji_call.args == ("🕹",)
    assert emoji_call.kwargs["parse_mode"] is None
    assert emoji_call.kwargs["reply_markup"].remove_keyboard is True
    assert "entities" not in emoji_call.kwargs
    assert "message_effect_id" not in emoji_call.kwargs
    assert message.answer.await_args_list[1].args == ("В какую игру сыграем?",)
    assert "message_effect_id" not in message.answer.await_args_list[1].kwargs


@pytest.mark.parametrize(
    "language,prompt",
    [
        ("ru", "В какую игру сыграем?"),
        (
            "en", "Which game shall we play?\n\n"
            "(Due to the game's mechanics, it is currently available only in Russian)",
        ),
    ],
)
async def test_games_menu_uses_selected_locale(
    language: str, prompt: str
) -> None:
    """Вопрос переводится, а название готовой игры остаётся прежним."""
    i18n = I18n(path=str(settings.ROOT / "locales"), domain="bot_00_template")
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": language}),
        set_state=AsyncMock(), set_data=AsyncMock(),
    )
    with i18n.context(), i18n.use_locale(language):
        await navigation.open_words(message, state, _bot())
    invitation = message.answer.await_args_list[1]
    assert invitation.args == (prompt,)
    markup = invitation.kwargs["reply_markup"]
    assert [len(row) for row in markup.inline_keyboard] == [1, 1]
    assert markup.inline_keyboard[0][0].text == "5БУКВ"
    assert markup.inline_keyboard[0][0].web_app.url == TEST_URL
    assert markup.inline_keyboard[1][0].callback_data == "wordli_back_to_main"
