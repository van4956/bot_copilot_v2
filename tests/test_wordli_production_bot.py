"""Явное включение рабочего игрового меню без Telegram и настоящей БД."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import Message

import app
from common import keyboard
from config_data import config as settings
from handlers import navigation

TOKEN = "123456:synthetic_production_token_not_a_credential"
URL = "https://wordli-production.example"


@pytest.fixture
def production_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Изолирует настройки от всех файлов и переменных пользователя."""
    values = {
        "APP_ENV": "production",
        "BOT_TOKEN": TOKEN,
        "DB_POST": "postgresql+asyncpg://synthetic@unused.invalid/unused",
    }
    monkeypatch.setattr(settings.os, "environ", values)
    monkeypatch.setattr(settings, "dotenv_values", Mock(return_value={}))
    return values


def _bot(
    monkeypatch: pytest.MonkeyPatch, enabled: bool = True, url: str = URL
) -> SimpleNamespace:
    """Создаёт рабочий клиент без открытия сетевой сессии."""
    config = settings.Config(
        tg_bot=settings.TgBot(token=TOKEN, token_test=""),
        db=settings.DataBase(), redis=settings.Redis(), mode="production",
        wordli_enabled=enabled, wordli_webapp_url=url,
        wordli_custom_emoji_id="1234567", wordli_custom_emoji_fallback="🧩",
    )
    monkeypatch.setattr(app, "Bot", Mock(return_value=SimpleNamespace()))
    return app.create_bot(config)


def _message() -> Message:
    """Создаёт синтетическое приглашение, отправленное ботом."""
    return Message.model_validate(
        {
            "message_id": 10, "date": datetime.now(timezone.utc),
            "chat": {"id": 9000000001, "type": "private"},
            "from": {"id": 123456, "is_bot": True, "first_name": "Synthetic"},
            "text": "В какую игру сыграем?",
        }
    )


def test_production_url_without_flag_is_not_activated(
    production_environment: dict[str, str],
) -> None:
    """Один адрес не включает игру в рабочем боте."""
    production_environment["WORDLI_WEBAPP_URL"] = URL
    config = settings.load_config()
    assert config.wordli_enabled is False
    assert config.wordli_webapp_url == ""


@pytest.mark.parametrize("flag", ["true", "TRUE", "1"])
def test_production_requires_explicit_flag_and_https_origin(
    production_environment: dict[str, str], flag: str,
) -> None:
    """Рабочий адрес принимается только вместе с явным флагом."""
    production_environment.update(WORDLI_ENABLED=flag, WORDLI_WEBAPP_URL=URL)
    config = settings.load_config()
    assert config.wordli_enabled is True
    assert config.wordli_webapp_url == URL
    assert config.test_mode is False
    assert config.tg_bot.token == TOKEN
    assert config.tg_bot.token_test == ""


@pytest.mark.parametrize("flag", ["false", "FALSE", "0"])
def test_disabled_production_ignores_url(
    production_environment: dict[str, str], flag: str,
) -> None:
    """Выключенная игра не требует и не использует адрес."""
    production_environment.update(
        WORDLI_ENABLED=flag, WORDLI_WEBAPP_URL="not-an-url",
    )
    config = settings.load_config()
    assert config.wordli_enabled is False
    assert config.wordli_webapp_url == ""


@pytest.mark.parametrize("flag", ["yes", "enabled", "", "2"])
def test_invalid_production_flag_is_rejected(
    production_environment: dict[str, str], flag: str,
) -> None:
    """Опечатка не превращается в неявное включение игры."""
    production_environment.update(WORDLI_ENABLED=flag, WORDLI_WEBAPP_URL=URL)
    with pytest.raises(ValueError):
        settings.load_config()


def test_enabled_production_requires_nonempty_url(
    production_environment: dict[str, str],
) -> None:
    """Включение без адреса останавливает запуск до создания клиента."""
    production_environment["WORDLI_ENABLED"] = "true"
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        settings.load_config()


@pytest.mark.parametrize(
    "url",
    [
        "http://wordli-production.example", "https:///",
        "https://user@wordli-production.example",
        "https://user:secret@wordli-production.example",
        "https://wordli-production.example?mode=test",
        "https://wordli-production.example/#fragment",
        "https://wordli-production.example:bad",
        "https://wordli-production.example:70000",
        "https://wordli-production.example/game/",
        "https://wordli-production.example\\path",
        " https://wordli-production.example",
        settings.PRODUCTION_WEBAPP_URL + "/wordli/",
    ],
)
def test_unsafe_production_url_is_rejected(
    production_environment: dict[str, str], url: str,
) -> None:
    """Production принимает отдельный HTTPS origin без пути и секретов."""
    production_environment.update(WORDLI_ENABLED="true", WORDLI_WEBAPP_URL=url)
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        settings.load_config()


@pytest.mark.parametrize("test_mode", [False, True])
@pytest.mark.parametrize("port", [0, -1, 65536])
def test_url_rejects_invalid_port_in_both_modes(test_mode: bool, port: int) -> None:
    """Нулевой и выходящий за диапазон порт не становятся WebApp URL."""
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        settings.validate_wordli_url(f"{URL}:{port}", test_mode=test_mode)


@pytest.mark.parametrize("test_mode", [False, True])
@pytest.mark.parametrize("port", [1, 443, 65535])
def test_url_accepts_valid_port_boundaries(test_mode: bool, port: int) -> None:
    """Допустимые границы порта сохраняют адрес без молчаливого изменения."""
    url = f"{URL}:{port}"
    assert settings.validate_wordli_url(url, test_mode=test_mode) == url


@pytest.mark.parametrize("test_mode", [False, True])
@pytest.mark.parametrize("character", [chr(code) for code in [*range(32), 127]])
def test_url_rejects_control_characters(test_mode: bool, character: str) -> None:
    """C0 и DEL отклоняются даже при предварительной нормализации urlsplit."""
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        settings.validate_wordli_url(URL + character, test_mode=test_mode)


@pytest.mark.parametrize(
    "url",
    [
        "https://unit.trycloudflare.com",
        "https://UNIT.TRYCLOUDFLARE.COM.",
        "https://trycloudflare.com",
    ],
)
def test_quick_tunnel_is_test_only(url: str) -> None:
    """Временный туннель сохраняется для тестов, но не для рабочего меню."""
    assert settings.validate_wordli_url(url, test_mode=True) == url
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        settings.validate_wordli_url(url, test_mode=False)


def test_test_mode_keeps_existing_url_activation(
    production_environment: dict[str, str],
) -> None:
    """Тестовый URL работает по прежним правилам без рабочего флага."""
    production_environment.update(
        APP_ENV="test", BOT_TOKEN_TEST=TOKEN, WORDLI_ENABLED="false",
        WORDLI_WEBAPP_URL="https://wordli-test.example/game/",
    )
    config = settings.load_config()
    assert config.wordli_enabled is False
    assert config.wordli_webapp_url == "https://wordli-test.example/game/"
    assert config.tg_bot.token == ""
    assert config.tg_bot.token_test == TOKEN


def test_production_bot_has_verified_game_flag_and_no_custom_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Клиент получает рабочий адрес, но не тестовые настройки эмодзи."""
    bot = _bot(monkeypatch)
    assert bot.test_mode is False
    assert bot.wordli_enabled_verified is True
    assert bot.wordli_webapp_url == URL
    assert bot.wordli_custom_emoji_id == ""
    assert bot.wordli_custom_emoji_fallback == ""
    assert keyboard.wordli_enabled(bot) is True


@pytest.mark.parametrize("url", ["", "http://wordli-production.example"])
def test_manual_config_is_validated_before_bot_creation(
    monkeypatch: pytest.MonkeyPatch, url: str,
) -> None:
    """Ручной Config тоже не обходит проверку рабочего адреса."""
    config = settings.Config(
        tg_bot=settings.TgBot(token=TOKEN, token_test=""),
        db=settings.DataBase(), redis=settings.Redis(), mode="production",
        wordli_enabled=True, wordli_webapp_url=url,
    )
    constructor = Mock()
    monkeypatch.setattr(app, "Bot", constructor)
    with pytest.raises(ValueError, match="WORDLI_WEBAPP_URL"):
        app.create_bot(config)
    constructor.assert_not_called()


def test_url_and_unverified_flag_cannot_enable_production_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL или похожий атрибут не заменяют подтверждённый флаг клиента."""
    monkeypatch.setattr(keyboard, "_", lambda text: text)
    bot = SimpleNamespace(
        test_mode=False, wordli_webapp_url=URL, wordli_enabled=True,
    )
    assert keyboard.wordli_enabled(bot) is False
    assert keyboard.start_keyboard(bot).keyboard[1][1].text == "Слова 🧩"


async def test_enabled_production_opens_games_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Рабочее меню использует тот же WebApp и прежний возврат."""
    monkeypatch.setattr(keyboard, "_", lambda text: text)
    monkeypatch.setattr(navigation, "_", lambda text: text)
    bot = _bot(monkeypatch)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": "ru"}),
        set_state=AsyncMock(), set_data=AsyncMock(),
    )
    message = SimpleNamespace(answer=AsyncMock())
    assert keyboard.start_keyboard(bot).keyboard[1][1].text == "Игры 🕹"
    assert await navigation._words_button(
        SimpleNamespace(text="Игры 🕹"), bot
    ) is True
    await navigation.open_words(message, state, bot)
    assert message.answer.await_count == 2
    invitation = message.answer.await_args_list[1]
    assert invitation.args == ("В какую игру сыграем?",)
    buttons = invitation.kwargs["reply_markup"].inline_keyboard
    assert [len(row) for row in buttons] == [2, 1]
    assert buttons[0][0].text == "5БУКВ"
    assert buttons[0][0].web_app.url == URL
    assert buttons[0][1].callback_data == "wordli_dimensions_stub"
    assert buttons[1][0].callback_data == "wordli_back_to_main"


async def test_enabled_production_stub_and_back_use_same_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Заглушка и возврат доступны после явного рабочего включения."""
    monkeypatch.setattr(navigation, "_", lambda text: text)
    bot = _bot(monkeypatch)
    callback = SimpleNamespace(message=_message(), answer=AsyncMock())
    await navigation.dimensions_stub(callback, bot)
    callback.answer.assert_awaited_once_with(
        "Игра в разработке", show_alert=True,
    )
    callback.answer.reset_mock()
    deleted = AsyncMock()
    cancelled = AsyncMock()
    monkeypatch.setattr(Message, "delete", deleted)
    monkeypatch.setattr(navigation.private, "cancel_cmd", cancelled)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": "ru"}),
        set_state=AsyncMock(), set_data=AsyncMock(),
    )
    await navigation.back_from_wordli(callback, state, bot)
    callback.answer.assert_awaited_once_with()
    deleted.assert_awaited_once_with()
    cancelled.assert_awaited_once_with(callback.message, state)


async def test_disabled_production_rejects_new_game_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отключение рабочего флага возвращает прежнее поведение меню."""
    monkeypatch.setattr(navigation, "_", lambda text: text)
    bot = _bot(monkeypatch, enabled=False)
    assert bot.wordli_webapp_url == ""
    assert bot.wordli_enabled_verified is False
    assert await navigation._words_button(
        SimpleNamespace(text="Игры 🕹"), bot
    ) is False
    callback = SimpleNamespace(message=_message(), answer=AsyncMock())
    await navigation.dimensions_stub(callback, bot)
    callback.answer.assert_awaited_once_with(
        "Мини-приложения временно недоступны", show_alert=True,
    )
