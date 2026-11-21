"""Изолированный Telegram-вход для ручной проверки Квордли."""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "local_testing" / ".env.test.local"
USERNAME = "tg_template_test_bot"
TUNNEL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com/?")


class DemoError(Exception):
    """Ошибка с сообщением без секретов и персональных данных."""


def _test_url(value: str) -> str:
    """Разрешает только корень временного тестового HTTPS-туннеля."""
    if not TUNNEL.fullmatch(value):
        raise DemoError("Нужен HTTPS-адрес тестового Quick Tunnel.")
    return value.rstrip("/")


def _dispatcher(
    users: set[int], url: str, wordli_url: str | None
) -> Dispatcher:
    """Собирает личное меню для разрешённых пользователей."""
    dp = Dispatcher(disable_fsm=True)
    dp.message.filter(F.chat.type == "private", F.from_user.id.in_(users))
    dp.callback_query.filter(
        F.message.chat.type == "private", F.from_user.id.in_(users)
    )
    old_game = (
        InlineKeyboardButton(text="5БУКВ", web_app=WebAppInfo(url=wordli_url))
        if wordli_url
        else InlineKeyboardButton(
            text="5БУКВ", callback_data="demo_wordli_off"
        )
    )
    menu = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                old_game,
                InlineKeyboardButton(
                    text="КВОРДЛИ", web_app=WebAppInfo(url=url)
                ),
            ],
        ]
    )

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        """Показывает кнопку входа в тестовый раздел."""
        await message.answer(
            "Тестовый раздел словесных игр.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Игры 🕹")]],
                resize_keyboard=True,
            ),
        )

    @dp.message(Command("quordle"))
    @dp.message(F.text == "Игры 🕹")
    async def games(message: Message) -> None:
        """Отправляет кнопки игр в одном ряду."""
        await message.answer("В какую игру сыграем?", reply_markup=menu)

    @dp.callback_query(F.data == "demo_wordli_off")
    async def wordli_off(callback: CallbackQuery) -> None:
        """Объясняет, почему старую игру не открываем на этом стенде."""
        await callback.answer(
            "«5БУКВ» на этом стенде не запущена.", show_alert=True
        )

    return dp


def _test_settings() -> dict[str, str | None]:
    """Читает только фиксированный файл настроек тестового стенда."""
    if (
        os.getenv("APP_ENV", "test") != "test"
        or ENV_FILE.resolve() != ENV_FILE
    ):
        raise DemoError("Разрешены только локальные настройки тестовой среды.")
    values = dotenv_values(ENV_FILE, interpolate=False)
    if values.get("APP_ENV") != "test":
        raise DemoError("Не найден корректный локальный тестовый файл.")
    return values


def load_test_identity() -> tuple[str, int, set[int]]:
    """Возвращает токен, ожидаемый ID и список допущенных к стенду игроков."""
    values = _test_settings()
    token = values.get("BOT_TOKEN_TEST") or ""
    expected = values.get("EXPECTED_TEST_BOT_ID") or ""
    username = (values.get("EXPECTED_TEST_BOT_USERNAME") or "").lstrip("@")
    if not (
        expected.isascii()
        and expected.isdigit()
        and token.partition(":")[0] == expected
        and username.lower() == USERNAME
    ):
        raise DemoError("Не подтверждены параметры тестового бота.")
    try:
        users = {
            int(item.strip())
            for name in ("OWNER", "ADMIN_LIST")
            for item in (values.get(name) or "").split(",")
            if item.strip()
        }
    except ValueError:
        raise DemoError("Некорректный список пользователей.") from None
    if not users or min(users) <= 0:
        raise DemoError("Нужен непустой список тестовых пользователей.")
    return token, int(expected), users


async def verify_test_bot(bot: Bot, expected_id: int) -> None:
    """Сверяет ID и имя реального бота до запуска сервисов."""
    try:
        identity = await bot.get_me()
    except Exception:
        raise DemoError("Не удалось подтвердить тестового бота.") from None
    if (
        identity.id != expected_id
        or (identity.username or "").lower() != USERNAME
    ):
        raise DemoError("Telegram вернул другого бота, запуск отменён.")


async def run(url: str, wordli_url: str | None = None) -> None:
    """Проверяет личность тестового бота и только затем принимает сообщения."""
    url = _test_url(url)
    token, expected_id, users = load_test_identity()
    if wordli_url is not None:
        wordli_url = _test_url(wordli_url)
        configured = _test_settings().get("WORDLI_WEBAPP_URL") or ""
        if wordli_url != configured.rstrip("/"):
            raise DemoError("Адрес «5БУКВ» не совпадает с тестовым файлом.")
        if wordli_url == url:
            raise DemoError("Для двух игр нужны разные тестовые адреса.")

    async with Bot(token=token) as bot:
        await verify_test_bot(bot, expected_id)
        if (await bot.get_webhook_info()).url:
            raise DemoError("Есть webhook: он не изменён, polling отменён.")
        dp = _dispatcher(users, url, wordli_url)
        print(f"Тестовый бот @{USERNAME} подтверждён; polling.", flush=True)
        offset = None
        failures = 0
        while True:
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=20,
                    allowed_updates=dp.resolve_used_update_types(),
                )
            except TelegramConflictError:
                raise DemoError("Конфликт получателей обновлений.") from None
            except TelegramNetworkError:
                failures += 1
                if failures >= 5:
                    raise DemoError("Связь с Telegram потеряна.") from None
                await asyncio.sleep(2)
                continue
            failures = 0
            for update in updates:
                await dp.feed_update(bot, update)
                offset = update.update_id + 1


def main() -> int:
    """Запускает тестовый клиент без вывода исключений с данными запроса."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTPS-туннель Квордли.")
    parser.add_argument(
        "--wordli-url", help="Подтверждённый живой тестовый адрес «5БУКВ»."
    )
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args.url, args.wordli_url))
    except KeyboardInterrupt:
        return 0
    except DemoError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Бот остановлен: проверьте связь и настройки.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
