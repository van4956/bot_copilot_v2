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

    def test_only_words_label_is_renamed(self) -> None:
        """Матрица кнопок остальных функций остаётся прежней."""
        source = SendMessage(
            chat_id=10, text="Готово",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[
                KeyboardButton(text="Weather 🌊"), KeyboardButton(text="Games 🕹"),
            ]]),
        )
        markup = self.menu.adapt(source).reply_markup
        self.assertEqual(
            [button.text for button in markup.keyboard[0]], ["Weather 🌊", "Words 🧩"]
        )
        self.assertEqual(source.reply_markup.keyboard[0][1].text, "Games 🕹")

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
