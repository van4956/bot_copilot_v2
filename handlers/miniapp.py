"""Меню мини-приложений и проверка результатов игр."""

import json
import logging
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from database.orm_games import orm_add_game
from filters.chat_type import ChatTypeFilter

logger = logging.getLogger(__name__)
miniapp_router = Router()
webapp_data_router = Router()
miniapp_router.message.filter(ChatTypeFilter(["private"]))

GAME_SCORE_LIMITS = {"snake": 2220, "platform": 400}


def _main_menu() -> InlineKeyboardMarkup:
    """Формирует выбор категории мини-приложений."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Сервисы"), callback_data="service_miniapp"
                ),
                InlineKeyboardButton(text=_("Игры"), callback_data="game_miniapp"),
            ],
            [
                InlineKeyboardButton(
                    text=_("Назад на главную ↩️"), callback_data="mini_back_to_main"
                )
            ],
        ]
    )


def _webapp_keyboard(
    bot: Bot, category: str, webapp_base_url: str | None = None
) -> ReplyKeyboardMarkup | None:
    """Создаёт обычные WebApp-кнопки только для настроенного адреса."""
    base_url = webapp_base_url
    if base_url is None:
        base_url = getattr(bot, "webapp_base_url", "")
    parsed = urlsplit(base_url or "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None

    apps = (
        [("🍕 Калькулятор", "pizza_calculator"), ("🎲 Рандомайзер", "random_generator")]
        if category == "services"
        else [("🕹 Платформа", "platform_game"), ("🐍 Змейка", "snake_game")]
    )
    buttons = [
        KeyboardButton(
            text=_(title),
            web_app=WebAppInfo(url=f"{base_url.rstrip('/')}/{slug}/"),
        )
        for title, slug in apps
    ]
    return ReplyKeyboardMarkup(
        keyboard=[
            buttons,
            [KeyboardButton(text="/mini")],
            [KeyboardButton(text=_("Назад на главную ↩️"))],
        ],
        resize_keyboard=True,
    )


@miniapp_router.message(Command("mini"))
async def cmd_miniapp(message: Message) -> None:
    """Открывает выбор сервисов и игр в личном чате."""
    await message.answer("Mini apps Telegram", reply_markup=keyboard.del_kb)
    await message.answer_photo(
        photo=FSInputFile("common/images/image_miniapp.jpg"),
        caption=_("Фабрика по производству мини-приложений:"),
        reply_markup=_main_menu(),
    )


async def _show_category(
    callback: CallbackQuery,
    category: str,
    webapp_base_url: str | None,
) -> None:
    """Показывает клавиатуру с поддерживаемым каналом sendData."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    markup = _webapp_keyboard(callback.bot, category, webapp_base_url)
    if markup is None:
        await callback.message.answer(_("Мини-приложения не настроены для этого бота."))
        return
    title = _("Сервисы") if category == "services" else _("Игры")
    await callback.message.answer(title, reply_markup=markup)


@miniapp_router.callback_query(F.data == "service_miniapp")
async def cmd_callback_service(
    callback: CallbackQuery, webapp_base_url: str | None = None
) -> None:
    """Показывает сервисные мини-приложения."""
    await _show_category(callback, "services", webapp_base_url)


@miniapp_router.callback_query(F.data == "game_miniapp")
async def cmd_callback_game(
    callback: CallbackQuery, webapp_base_url: str | None = None
) -> None:
    """Показывает игры с обычными WebApp-кнопками."""
    await _show_category(callback, "games", webapp_base_url)


@miniapp_router.callback_query(F.data == "back_to_mini")
async def cmd_callback_about(callback: CallbackQuery) -> None:
    """Обслуживает кнопку возврата из старых сообщений."""
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=_main_menu())


@miniapp_router.callback_query(F.data == "mini_back_to_main")
async def cmd_callback_about_to_main(callback: CallbackQuery) -> None:
    """Возвращает основную клавиатуру бота."""
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.delete()
        await callback.message.answer(
            _("Главная панель"), reply_markup=keyboard.start_keyboard()
        )


def _parse_game_result(raw_data: str) -> tuple[str, int]:
    """Проверяет структуру и допустимые значения игрового результата."""
    data = json.loads(raw_data)
    if not isinstance(data, dict) or data.get("action") != "game_end":
        raise ValueError("Некорректное действие игры")
    game = data.get("game")
    if not isinstance(game, str) or game not in GAME_SCORE_LIMITS:
        raise ValueError("Неизвестная игра")
    score = data.get("score")
    if (
        type(score) is not int
        or score < 0
        or score > GAME_SCORE_LIMITS[game]
        or score % 10
    ):
        raise ValueError("Некорректный счёт игры")
    return game, score


@webapp_data_router.message(F.web_app_data)
async def handle_web_app_data(message: Message, session: AsyncSession) -> None:
    """Сохраняет проверенный результат поддерживаемой игры."""
    if message.web_app_data is None or message.from_user is None:
        return
    try:
        game, score = _parse_game_result(message.web_app_data.data)
    except (ValueError, TypeError):
        logger.warning("Отклонён некорректный результат мини-приложения")
        await message.answer(_("Не удалось принять результат игры."))
        return

    user = message.from_user
    await orm_add_game(
        session,
        {
            "game_name": game,
            "user_id": user.id,
            "user_name": user.username or f"id_{user.id}",
            "score": score,
        },
    )
    await message.answer(_("Игра окончена!\nВаш счет: {score}").format(score=score))


@miniapp_router.error()
@webapp_data_router.error()
async def error_handler(event: ErrorEvent) -> bool:
    """Сообщает об ошибке мини-приложения через событие aiogram 3."""
    error = event.exception
    logger.error(
        "Ошибка мини-приложения: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    text = _("Произошла ошибка мини-приложения. Попробуйте позже.")
    if event.update.message is not None:
        await event.update.message.answer(text)
    elif event.update.callback_query is not None:
        await event.update.callback_query.answer(text, show_alert=True)
    return True
