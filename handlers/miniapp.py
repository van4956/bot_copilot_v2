import logging

# Инициализируем логгер модуля
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.info("Загружен модуль: %s", __name__)

from icecream import ic
ic.configureOutput(includeContext=True, prefix=' >>> Debag >>> ')

import asyncio
import json
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import WebAppInfo, FSInputFile, CallbackQuery, InlineKeyboardButton, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from database.models import Games
from database.orm_games import orm_add_game

miniapp_router = Router()

# URL веб-приложения
WEBAPP_URL_PIZZA = "https://van4956.github.io/bot_copilot_v2/pizza_calculator/"
WEBAPP_URL_RANDOM = "https://van4956.github.io/bot_copilot_v2/random_generator/"
WEBAPP_URL_SNAKE = "https://van4956.github.io/bot_copilot_v2/snake_game/"
WEBAPP_URL_PLATFORM = "https://van4956.github.io/bot_copilot_v2/platform_game/"

# команда /mini - "Мини-приложения"
@miniapp_router.message(Command("mini"))
async def cmd_miniapp(message: types.Message):
    await message.answer(text="Mini apps Telegram",reply_markup=keyboard.del_kb)
    await asyncio.sleep(1)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("Сервисы"), callback_data='service_miniapp'))
    builder.row(InlineKeyboardButton(text=_("Игры"), callback_data='game_miniapp'))
    builder.row(InlineKeyboardButton(text=_("Назад на главную ↩️"), callback_data='mini_back_to_main'))
    photo = FSInputFile("common/images/image_miniapp.jpg")
    await message.answer_photo(
        photo=photo,
        caption=_("Фабрика по производству мини-приложений:"),
        reply_markup=builder.adjust(2,1).as_markup() # type: ignore
    )

# callback "Сервисы"
@miniapp_router.callback_query(F.data == 'service_miniapp')
async def cmd_callback_service(callback: CallbackQuery, workflow_data: dict):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("🍕 Калькулятор"), web_app=WebAppInfo(url=WEBAPP_URL_PIZZA)))
    builder.row(InlineKeyboardButton(text=_("🎲 Рандомайзер"), web_app=WebAppInfo(url=WEBAPP_URL_RANDOM)))
    builder.row(InlineKeyboardButton(text=_("Назад"), callback_data="back_to_mini"))

    # builder.row(InlineKeyboardButton(text=_("Назад на главную ↩️"), callback_data='mini_back_to_main'))
    markup: InlineKeyboardMarkup = builder.adjust(2,1,1).as_markup() # type: ignore
    await callback.message.edit_reply_markup(reply_markup=markup)

# callback "Игры"
@miniapp_router.callback_query(F.data == 'game_miniapp')
async def cmd_callback_game(callback: CallbackQuery, workflow_data: dict):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("🕹 Платформа"), web_app=WebAppInfo(url=WEBAPP_URL_PLATFORM)))
    builder.row(InlineKeyboardButton(text=_("🐍 Змейка"), web_app=WebAppInfo(url=WEBAPP_URL_SNAKE)))
    builder.row(InlineKeyboardButton(text=_("Назад"), callback_data="back_to_mini"))

    # builder.row(InlineKeyboardButton(text=_("Назад на главную ↩️"), callback_data='mini_back_to_main'))
    markup: InlineKeyboardMarkup = builder.adjust(2,1,1).as_markup() # type: ignore
    await callback.message.edit_reply_markup(reply_markup=markup)

# callback "назад" к сервисам и играм
@miniapp_router.callback_query(F.data == 'back_to_mini')
async def cmd_callback_about(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("Сервисы"), callback_data='service_miniapp'))
    builder.row(InlineKeyboardButton(text=_("Игры"), callback_data='game_miniapp'))
    builder.row(InlineKeyboardButton(text=_("Назад на главную ↩️"), callback_data='mini_back_to_main'))
    markup: InlineKeyboardMarkup = builder.adjust(2,1).as_markup() # type: ignore
    await callback.message.edit_reply_markup(reply_markup=markup)

# callback "назад на главную"
@miniapp_router.callback_query(F.data == 'mini_back_to_main')
async def cmd_callback_about_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer(_('Назад на главную ↩️'))
    await asyncio.sleep(1)
    await callback.message.answer(_('Главная панель'), reply_markup=keyboard.start_keyboard())

# Обработка данных от веб-приложения
@miniapp_router.message(F.web_app_data)
async def handle_web_app_data(message: Message, session: AsyncSession, workflow_data: dict):
    logger.info("Получены данные: %s", message.web_app_data)
    logger.info("Raw data: %s", message.web_app_data.data)
    try:
        data = json.loads(message.web_app_data.data)
        logger.info("Parsed data: %s", data)
        score=data.get('score', 0)

        if data.get('action') == 'game_start' and data.get('game') == 'snake':
            logger.info("Starting snake game for user %s", message.from_user.id)

        if data.get('action') == 'game_end' and data.get('game') == 'snake':
            logger.info("Ending snake game for user %s with score %s", message.from_user.id, data.get('score', 0))
            # добавляем данные игры в бд
            data = {'game_name': 'snake',
                                    'user_id': message.from_user.id,
                                    'user_name': message.from_user.username,
                                    'score': score}

            await orm_add_game(session=session, data=data)

            # Отправляем сообщение пользователю
            await message.answer(f"Игра окончена!\nВаш счет: {data.get('score', 0)}")

    except Exception as e:
        logger.error("Error processing data: %s", e, exc_info=True)

# Обработка ошибок при работе с веб-приложением
@miniapp_router.error()
async def error_handler(update: types.Update, exception: Exception):
    logger.error("Ошибка при работе с веб-приложением: %s", exception)
    if isinstance(update.message, Message):
        await update.message.answer("Произошла ошибка при запуске приложения. Попробуйте позже.")
