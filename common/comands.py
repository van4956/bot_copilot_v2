"""Локализованное меню команд без зависимости от языка текущего запроса."""

import asyncio
import gettext
import logging

from aiohttp import ClientError
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

from common.locales import SUPPORTED_LOCALES
from config_data.config import ROOT

logger = logging.getLogger(__name__)


def get_commands(locale: str = "ru") -> list[BotCommand]:
    """Читает подписи команд из собранного каталога выбранного языка."""
    if locale not in SUPPORTED_LOCALES:
        raise ValueError("Неподдерживаемый язык меню команд")
    catalog = gettext.translation(
        "bot_00_template", localedir=ROOT / "locales", languages=[locale]
    )
    _ = catalog.gettext
    return [
        BotCommand(command="main", description=_("Главная панель")),
        BotCommand(command="book", description=_("Кулинарная книга")),
        BotCommand(command="info", description=_("Настройки")),
    ]


async def sync_user_commands(bot: Bot, chat_id: int, locale: str) -> None:
    """Обновляет личное меню, не отменяя смену языка при сбое Telegram."""
    try:
        await bot.set_my_commands(
            get_commands(locale),
            scope=BotCommandScopeChat(chat_id=chat_id),
            request_timeout=5,
        )
    except (TelegramAPIError, ClientError, TimeoutError, asyncio.TimeoutError):
        logger.warning("Не удалось обновить язык личного меню команд")
