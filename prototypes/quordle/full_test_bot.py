"""Запуск полного Copilot с двумя играми только на тестовом боте."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace
from typing import Any
from unittest.mock import patch

from aiogram import Bot, F
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.filters import Command
from aiogram.methods import SendMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import dotenv_values

import app
from config_data.config import Config, load_config, validate_test_database
from handlers import navigation

from .demo_bot import (
    ENV_FILE,
    USERNAME,
    DemoError,
    _test_url,
    load_test_identity,
    verify_test_bot,
)

WORDS_LABELS = {
    "Игры 🕹": "Слова 🧩",
    "Games 🕹": "Words 🧩",
    "Juegos 🕹": "Palabras 🧩",
    "Ігри 🕹": "Слова 🧩",
}


class TwoGamesMenu(BaseRequestMiddleware):
    """Добавляет КВОРДЛИ в штатную разметку, не заменяя обработчики."""

    def __init__(self, wordli: str, quordle: str, users: set[int]) -> None:
        """Запоминает проверенные адреса и допущенных тестеров."""
        self.wordli = wordli
        self.quordle = quordle
        self.users = users

    def adapt(self, method: SendMessage) -> SendMessage:
        """Меняет название раздела и добавляет соседнюю кнопку."""
        if method.chat_id not in self.users:
            return method
        markup = method.reply_markup
        if isinstance(markup, ReplyKeyboardMarkup):
            if not any(
                button.text in WORDS_LABELS for row in markup.keyboard for button in row
            ):
                return method
            rows = [
                [
                    button.model_copy(update={"text": WORDS_LABELS[button.text]})
                    if button.text in WORDS_LABELS else button
                    for button in row
                ]
                for row in markup.keyboard
            ]
            return method.model_copy(
                update={"reply_markup": markup.model_copy(update={"keyboard": rows})}
            )
        if not isinstance(markup, InlineKeyboardMarkup):
            return method
        rows = markup.inline_keyboard
        if not (
            len(rows) == 2
            and len(rows[0]) == 1
            and rows[0][0].text == "5БУКВ"
            and rows[0][0].web_app
            and rows[0][0].web_app.url.rstrip("/") == self.wordli
            and len(rows[1]) == 1
            and rows[1][0].callback_data == "wordli_back_to_main"
        ):
            return method
        new_button = InlineKeyboardButton(
            text="КВОРДЛИ", web_app=WebAppInfo(url=self.quordle)
        )
        new_markup = markup.model_copy(
            update={"inline_keyboard": [[*rows[0], new_button], rows[1]]}
        )
        return method.model_copy(update={"reply_markup": new_markup})

    async def __call__(self, make_request: Any, bot: Bot, method: Any) -> Any:
        """Преобразует только сообщения, отправленные тестовым экземпляром."""
        if getattr(bot, "test_mode", False) and isinstance(method, SendMessage):
            method = self.adapt(method)
        return await make_request(bot, method)


def load_isolated_config(token: str, expected: int) -> Config:
    """Читает только локальный файл и не заимствует ключи среды."""
    values = dotenv_values(ENV_FILE, interpolate=False)
    # Dotenv штатного Copilot использует окружение как приоритетную подмену.
    # Убираем её при чтении файла, в том числе для отсутствующих ключей API.
    with patch.dict(os.environ, {"APP_ENV": "test"}, clear=True):
        config = load_config(str(ENV_FILE))
    if (
        not config.test_mode
        or config.expected_test_bot_id != expected
        or config.tg_bot.token_test != token
        or config.database_url != values.get("TEST_DATABASE_URL")
    ):
        raise DemoError("Не подтверждена изоляция тестового Copilot.")
    validate_test_database(config.database_url)
    return replace(
        config, notify_service_chat=False, drop_pending_updates=False,
        tg_bot=replace(config.tg_bot, token=""),
    )


async def run(quordle: str, check_only: bool) -> None:
    """Подтверждает имя и запускает неизменённые роутеры Copilot."""
    token, expected, users = load_test_identity()
    config = load_isolated_config(token, expected)
    wordli = _test_url(config.wordli_webapp_url)
    if quordle == wordli:
        raise DemoError("Для игр нужны разные тестовые HTTPS-адреса.")
    async with Bot(token=token) as bot:
        await verify_test_bot(bot, expected)
        if (await bot.get_webhook_info()).url:
            raise DemoError("Есть webhook; тестовый polling не запущен.")
    if check_only:
        print(f"Тестовый бот @{USERNAME} и адреса игр подтверждены.", flush=True)
        return

    from local_testing.testing.local_bootstrap import configure_local_fixtures

    configure_local_fixtures()
    navigation.navigation_router.message.register(
        navigation.open_words, Command("quordle"), F.from_user.id.in_(users)
    )
    menu = TwoGamesMenu(wordli, quordle, users)

    async def install_menu(bot: Bot) -> None:
        """Подключает разметку при старте штатного Dispatcher."""
        if getattr(bot, "test_mode", False) is not True or bot.id != expected:
            raise DemoError("Переопределение разрешено только в локальном тесте.")
        bot.session.middleware(menu)

    navigation.navigation_router.startup.register(install_menu)
    print(f"Тестовый бот @{USERNAME} подтверждён; запуск полного Copilot.", flush=True)
    await app.main(config)


def main() -> int:
    """Не раскрывает реквизиты Telegram или базы в сообщениях ошибок."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTPS-туннель КВОРДЛИ.")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(_test_url(args.url), args.check_only))
    except KeyboardInterrupt:
        return 0
    except DemoError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Тестовый Copilot остановлен: {type(error).__name__}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
