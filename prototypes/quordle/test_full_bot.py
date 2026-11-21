"""Проверки тестового меню полного Copilot без вызовов Telegram."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.methods import SendMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.i18n import I18n

from common.keyboard import start_keyboard
from config_data.config import ROOT

from .full_test_bot import TwoGamesMenu

WORDLI = "https://wordli.trycloudflare.com"
QUORDLE = "https://quordle.trycloudflare.com"


class GamesMenuTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет, что адаптер меняет только тестовый раздел игр."""

    def setUp(self) -> None:
        """Готовит адаптер и два адреса без токенов."""
        self.menu = TwoGamesMenu(WORDLI, QUORDLE, {10})

    def test_menu_links_and_back_button(self) -> None:
        """Добавляет соседнюю игру, не изменяя штатную кнопку возврата."""
        wordli_button = InlineKeyboardButton(
            text="5БУКВ", web_app=WebAppInfo(url=WORDLI)
        )
        back = InlineKeyboardButton(text="Назад", callback_data="wordli_back_to_main")
        source = SendMessage(
            chat_id=10, text="В какую игру сыграем?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[wordli_button], [back]]),
        )
        applied = self.menu.adapt(source)
        games, controls = applied.reply_markup.inline_keyboard
        self.assertEqual([button.text for button in games], ["5БУКВ", "КВОРДЛИ"])
        self.assertEqual([button.web_app.url for button in games], [WORDLI, QUORDLE])
        self.assertEqual(controls, [back])
        self.assertEqual(len(source.reply_markup.inline_keyboard[0]), 1)
        other_chat = source.model_copy(update={"chat_id": 20})
        self.assertIs(self.menu.adapt(other_chat), other_chat)

    def test_main_keyboard_is_not_renamed(self) -> None:
        """Адаптер не вмешивается в штатную главную клавиатуру."""
        source = SendMessage(
            chat_id=10, text="Готово",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[
                KeyboardButton(text="Weather 🌊"), KeyboardButton(text="Games 🕹"),
            ]]),
        )
        self.assertIs(self.menu.adapt(source), source)

    def test_games_is_the_default_button_in_all_four_languages(self) -> None:
        """Контракт подписи не зависит от доступности игры."""
        i18n = I18n(path=str(ROOT / "locales"), domain="bot_00_template")
        labels = {
            "ru": "Игры 🕹", "en": "Games 🕹",
            "es": "Juegos 🕹", "uk": "Ігри 🕹",
        }
        enabled = SimpleNamespace(test_mode=True, wordli_webapp_url=WORDLI)
        for locale, label in labels.items():
            with self.subTest(locale=locale):
                with i18n.context(), i18n.use_locale(locale):
                    for bot in (None, enabled):
                        self.assertEqual(start_keyboard(bot).keyboard[1][1].text, label)

    async def test_non_test_bot_request_is_not_changed(self) -> None:
        """Не меняет Telegram-запрос вне проверенного тестового режима."""
        request = AsyncMock(return_value=True)
        bot = SimpleNamespace(test_mode=False)
        message = SendMessage(
            chat_id=10, text="Меню", reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Игры 🕹")]]
            )
        )
        self.assertTrue(await self.menu(request, bot, message))
        request.assert_awaited_once_with(bot, message)
