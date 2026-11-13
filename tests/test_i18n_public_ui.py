"""Публичные тексты и кнопки проходят четыре настоящих каталога без сети."""

import re
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.utils.i18n import I18n

from common import keyboard
from common.http import ExternalServiceError
from config_data.config import ROOT
from handlers import (
    admin,
    cookbook,
    currency,
    donate,
    group,
    navigation,
    other,
    private,
)

Ui = tuple[str, I18n, Bot, FSMContext]


@pytest_asyncio.fixture(params=["ru", "en", "es", "uk"])
async def ui(request: pytest.FixtureRequest) -> AsyncIterator[Ui]:
    """Создаёт язык, синтетического бота и состояние только в памяти."""
    language = request.param
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    bot = Bot("123456:OFFLINE_I18N_TEST_ONLY")
    bot.test_mode = True
    bot.wordli_webapp_url = "https://example.invalid/game"
    bot.api_currency = "offline-key"
    bot.edit_message_reply_markup = AsyncMock()
    bot.delete_message = AsyncMock()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=99, user_id=99))
    await state.set_data({"locale": language})
    try:
        with i18n.context(), i18n.use_locale(language):
            yield language, i18n, bot, state
    finally:
        await storage.close()
        await bot.session.close()


def _message(bot: Bot, text: str = "") -> SimpleNamespace:
    """Подменяет отправку сообщений и фото, не читая локальные изображения."""
    sent = SimpleNamespace(message_id=44, react=AsyncMock())
    return SimpleNamespace(
        bot=bot,
        text=text,
        message_id=22,
        chat=SimpleNamespace(id=99, type="private"),
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(return_value=sent),
        answer_photo=AsyncMock(return_value=sent),
        answer_document=AsyncMock(return_value=sent),
        edit_text=AsyncMock(),
        delete=AsyncMock(),
    )


def _callback(bot: Bot, message: object, data: str = "") -> SimpleNamespace:
    """Создаёт безопасное нажатие кнопки без Telegram."""
    return SimpleNamespace(
        bot=bot,
        message=message,
        data=data,
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )


def _flat(markup: object) -> list[str]:
    """Возвращает подписи обычной или inline-клавиатуры."""
    rows = getattr(markup, "inline_keyboard", None) or markup.keyboard
    return [button.text for row in rows for button in row]


def test_main_and_numeric_keyboard_use_actual_catalog(ui: Ui) -> None:
    """Реальные подписи исключают регрессию Words и английские кнопки в RU."""
    language, _, bot, _ = ui
    assert (
        _flat(keyboard.start_keyboard(bot))
        == {
            "ru": ["Погода 🌊", "Валюта 💵", "Котики 🐱", "Игры 🕹"],
            "en": ["Weather 🌊", "Currency 💵", "Cats 🐱", "Games 🕹"],
            "es": ["Tiempo 🌊", "Divisas 💵", "Gatitos 🐱", "Juegos 🕹"],
            "uk": ["Погода 🌊", "Валюта 💵", "Котики 🐱", "Ігри 🕹"],
        }[language]
    )
    markup = keyboard.markup_num()
    assert (
        _flat(markup)[-3:]
        == {
            "ru": ["◀️ Удалить", "Ввод ▶️", "Назад на главную ↩️"],
            "en": ["◀️ Delete", "Enter ▶️", "Back to main ↩️"],
            "es": ["◀️ Borrar", "Introducir ▶️", "Volver al inicio ↩️"],
            "uk": ["◀️ Видалити", "Ввід ▶️", "Назад на головну ↩️"],
        }[language]
    )
    assert [
        button.callback_data for row in markup.inline_keyboard[-2:] for button in row
    ] == [
        "num_del",
        "num_enter",
        "about_back_to_main",
    ]


async def test_currency_full_text_input_flow_uses_one_language(
    ui: Ui,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Калькулятор, подтверждение, результат и меню завершаются на одном языке."""
    language, i18n, bot, state = ui
    message = _message(bot)
    await currency.process_reopen(message, state, {})
    controls = _flat(message.answer.await_args.kwargs["reply_markup"])[-3:]
    assert controls[0] == i18n.gettext("◀️ Удалить")
    message.text = "12.5"
    await currency.process_amout_(message, state)
    assert message.answer.await_args.args[0] == i18n.gettext(
        "Сумма операции: {user_value}\nБазовая валюта: "
    ).format(user_value=Decimal("12.5"))
    await currency.process_base_currency(_callback(bot, message, "USD"), state)
    assert "USD" in message.edit_text.await_args.args[0]
    monkeypatch.setattr(
        currency,
        "get_currency_rate",
        AsyncMock(
            return_value=("12:00", "08-11-2026", Decimal("0.9")),
        ),
    )
    await currency.process_target_currency(_callback(bot, message, "EUR"), state)
    result = message.edit_text.await_args.args[0]
    assert "12.5 USD  ~  11.25 EUR" in result
    assert message.answer.await_args.args[0] == i18n.gettext("Что дальше?")
    assert _flat(message.answer.await_args.kwargs["reply_markup"]) == [
        i18n.gettext("Повторить 💵"),
        i18n.gettext("Назад на главную ↩️"),
    ]
    assert await state.get_state() is None
    assert (await state.get_data())["locale"] == language
    if language == "en":
        assert not re.search("[А-Яа-яЁё]", result)


async def test_currency_input_errors_are_translated(ui: Ui) -> None:
    """Подсказки неверного ввода и старых кнопок не обходят gettext."""
    _, i18n, bot, state = ui
    message = _message(bot, "not a number")
    await state.set_state(currency.Currency.amount)
    await currency.process_amout_(message, state)
    assert message.answer.await_args.args[0] == i18n.gettext(
        "Введите неотрицательное число до 18 символов: цифры и точка."
    )
    callback = _callback(bot, message, "num_1")
    await currency.stale_numeric_keypad(callback)
    assert callback.answer.await_args.args[0] == i18n.gettext(
        "Эта клавиатура устарела. Используйте текущую."
    )


def test_cookbook_formats_author_without_label_or_translation(ui: Ui) -> None:
    """Автор выделяется курсивом без добавленного слова и остаётся исходным."""
    _, i18n, _, _ = ui
    recipe = SimpleNamespace(
        recipe_name="Борщ <special>",
        author="Иван & Jane",
        description="Описание " * 400,
    )
    caption = cookbook.format_recipe_caption(recipe)
    assert "<i>Иван &amp; Jane</i>\n\n" in caption
    assert "Автор:" not in caption and "Author:" not in caption and "Autor:" not in caption
    assert "Борщ &lt;special&gt;" in caption
    visible = unescape(re.sub("<[^>]+>", "", caption))
    assert len(visible.encode("utf-16-le")) // 2 <= 1024
    assert _flat(cookbook._page_keyboard(1, 3))[-1] == i18n.gettext("Назад на главную ↩️")


async def test_games_invitation_and_stub_are_translated(
    ui: Ui,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Название игры сохраняется, а приглашение и уведомление переводятся."""
    language, i18n, bot, state = ui
    message = _message(bot)
    await navigation.open_words(message, state, bot)
    call = message.answer.await_args
    expected = i18n.gettext("В какую игру сыграем?")
    if language != "ru":
        expected += "\n\n(" + i18n.gettext(
            "Из-за особенностей механики игры игра пока доступна только на русском языке"
        ) + ")"
    assert call.args[0] == expected
    assert _flat(call.kwargs["reply_markup"]) == [
        "5БУКВ",
        i18n.gettext("Назад на главную ↩️"),
    ]
    assert [len(row) for row in call.kwargs["reply_markup"].inline_keyboard] == [1, 1]
    real_message = Message.model_validate(
        {
            "message_id": 55,
            "date": 1794144000,
            "chat": {"id": 99, "type": "private"},
        }
    ).as_(bot)
    callback = _callback(bot, real_message)
    await navigation.dimensions_stub(callback, bot)
    callback.answer.assert_awaited_once_with(
        i18n.gettext("Игра в разработке"), show_alert=True
    )


async def test_information_language_terms_and_author_are_translated(
    ui: Ui,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Информация и обе вложенные страницы используют актуальный язык."""
    language, i18n, bot, state = ui
    monkeypatch.setattr(
        other.psutil,
        "Process",
        lambda: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=1024 * 1024),
            cpu_percent=lambda interval: 0.0,
            create_time=lambda: time.time() - 60,
        ),
    )
    message = _message(bot)
    await other.process_help_command(message, {}, state)
    status = message.answer.await_args.kwargs["text"]
    assert {
        "ru": "📊 Состояние бота", "en": "📊 Bot status",
        "es": "📊 Estado del bot", "uk": "📊 Стан бота",
    }[language] in status
    assert _flat(message.answer.await_args.kwargs["reply_markup"]) == [
        i18n.gettext(text)
        for text in [
            "Условия использования",
            "Сменить язык",
            "Об авторе",
            "Поддержать",
            "Назад на главную ↩️",
        ]
    ]
    callback = _callback(bot, message)
    await other.locale_cmd(callback, state)
    assert _flat(message.answer.await_args.kwargs["reply_markup"]) == [
        i18n.gettext(text)
        for text in [
            "🇺🇸 Английский",
            "🇷🇺 Русский",
            "🇪🇸 Испанский",
            "🇺🇦 Украинский",
            "Назад",
            "Назад на главную ↩️",
        ]
    ]
    await other.terms_cmd(callback, state)
    terms = message.answer.await_args.args[0]
    assert terms.startswith({
        "ru": "Условия использования @Terminatorvan_bot:",
        "en": "Terms of use @Terminatorvan_bot:",
        "es": "Condiciones de uso de @Terminatorvan_bot:",
        "uk": "Умови використання @Terminatorvan_bot:",
    }[language])
    if language == "en":
        assert not re.search("[А-Яа-яЁё]", terms)
    await private.author_cmd(callback, {}, state)
    assert message.answer_photo.await_args.kwargs["caption"] == i18n.gettext(
        "... в мире, где машины стремятся к господству, он выбрал судьбу героя, "
        "создавая ботов, как первый шаг к спасению человечества через код и умные алгоритмы."
    )


async def test_cats_error_and_donation_keyboard_use_selected_locale(
    ui: Ui,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Фотографии не переводятся, а ошибки и управление донатом переводятся."""
    _, i18n, bot, state = ui
    message = _message(bot)
    monkeypatch.setattr(private.r, "random", lambda: 0.9)
    monkeypatch.setattr(
        private, "get_json", AsyncMock(side_effect=ExternalServiceError("offline"))
    )
    await private.cat_cmd(message, {})
    message.answer.assert_awaited_once_with(
        i18n.gettext("Здесь должна была быть картинка с котиком :(")
    )
    await donate.cmd_donate(message, state)
    assert _flat(message.answer.await_args.kwargs["reply_markup"]) == [
        "10 ⭐️",
        "50 ⭐️",
        "100 ⭐️",
        i18n.gettext("другое"),
        i18n.gettext("Назад на главную ↩️"),
    ]
    monkeypatch.setattr(donate, "orm_get_invoice", AsyncMock(return_value=None))
    monkeypatch.setattr(
        donate, "validate_invoice", Mock(side_effect=ValueError("invalid"))
    )
    query = SimpleNamespace(
        invoice_payload="offline",
        from_user=SimpleNamespace(id=99),
        currency="XTR",
        total_amount=10,
        answer=AsyncMock(),
    )
    await donate.pre_checkout_query(query, object())
    query.answer.assert_awaited_once_with(
        ok=False,
        error_message=i18n.gettext("Счёт недействителен. Создайте новый донат."),
    )


async def test_technical_identifiers_have_translated_labels(ui: Ui) -> None:
    """Идентификаторы не переводятся, а подписи соответствуют текущему каталогу."""
    language, _, bot, _ = ui
    message = _message(bot)
    await group.get_chat_id_cmd(message)
    assert (
        message.answer.await_args.args[0]
        == {
            "ru": "ID чата: <code>99</code>\nID пользователя: <code>99</code>",
            "en": "Chat ID: <code>99</code>\nUser ID: <code>99</code>",
            "es": "ID del chat: <code>99</code>\nID del usuario: <code>99</code>",
            "uk": "ID чату: <code>99</code>\nID користувача: <code>99</code>",
        }[language]
    )


def test_admin_stays_russian_under_each_public_locale(ui: Ui) -> None:
    """Публичная локаль не переводит административную клавиатуру и подсказки."""
    labels = _flat(admin.get_admin_keyboard())
    assert labels == [
        "Ассортимент 📖", "Добавить 📃", "Синхронизировать 🔄",
        "Информация 👨‍👩‍👦‍👦", "Назад на главную ↩️",
    ]
    assert [len(row) for row in admin.get_admin_keyboard().keyboard] == [3, 1, 1]
    for step in (
        admin.AddProduct.recipe_name,
        admin.AddProduct.author,
        admin.AddProduct.description,
        admin.AddProduct.price,
        admin.AddProduct.image,
    ):
        prompt = admin.get_step_prompt(step.state)
        assert prompt
        assert re.search("[А-Яа-яЁё]", prompt)
