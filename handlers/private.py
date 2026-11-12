"""Главная панель и фотографии котиков."""

import asyncio
import logging
import os
import random as r
from urllib.parse import urlsplit

from aiohttp import ClientError
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    Message,
    ReactionTypeEmoji,
)
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiogram.utils.keyboard import InlineKeyboardBuilder

from common import keyboard
from common.http import ExternalServiceError, get_json
from filters.chat_type import ChatTypeFilter

logger = logging.getLogger(__name__)


private_router = Router()
private_router.message.filter(ChatTypeFilter(["private"]))
private_router.edited_message.filter(ChatTypeFilter(["private"]))

# Создаем константы для текстов, которые используются в декораторах
BACK_TO_MAIN = __("Назад на главную ↩️")
CATS = __("Котики 🐱")
# Эффект самого сообщения ❤️; обычная реакция под фотографией сохраняется.
CAT_HEART_EFFECT_ID = "5159385139981059251"


# команда /main, кнопка "назад на главную"
@private_router.message(or_f(Command("main"), F.text == BACK_TO_MAIN))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.set_state(None)
    await message.answer(
        _("Главная панель"),
        reply_markup=keyboard.start_keyboard(
            bot=getattr(message, "bot", None)
        ),
    )


# символ точка - ОТМЕНА, сброс любого состояния;
# ловим любое состояние пользователя, и если введен текст "."
@private_router.message(StateFilter("*"), F.text.casefold() == ".")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()  # получаем текущее состояние
    if current_state is None:  # если состояния пустое, то ни чего не делаем
        return
    # Завершаем машину состояний, но не удаляем данные из словаря FSMContext
    await state.set_state(None)
    await message.answer(_("Действия отменены"))
    await asyncio.sleep(1)
    await message.answer(
        _("Главная панель"),
        reply_markup=keyboard.start_keyboard(
            bot=getattr(message, "bot", None)
        ),
    )


# команда /author
# @private_router.message(Command("author"))
# async def author_cmd(message: Message, workflow_data: dict):
@private_router.callback_query(F.data == "author")
async def author_cmd(callback: CallbackQuery, workflow_data: dict, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    last_message_id = data.get("last_message_id")

    # Если есть предыдущее сообщение, удаляем его
    if last_message_id:
        try:
            await callback.bot.delete_message(
                chat_id=user_id, message_id=last_message_id
            )
        except Exception as e:
            logger.error("Ошибка при удалении last_message_id сообщения: %s", e)
    else:
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error("Ошибка при удалении сообщения: %s", e)

    # Нижние кнопки уже убраны при открытии информации: не вызываем удаление снова.
    await callback.message.answer(text=" ... ")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Telegram", url="tg://user?id=459148628"))
    builder.row(
        InlineKeyboardButton(
            text="Linkedin", url="https://www.linkedin.com/in/ivan-goncharov-8a1982212/"
        )
    )
    builder.row(InlineKeyboardButton(text="GitHub", url="https://github.com/van4956"))
    builder.row(
        InlineKeyboardButton(text="Kaggle", url="https://www.kaggle.com/ivan4956")
    )
    builder.row(InlineKeyboardButton(text=_("Назад"), callback_data="back_to_info"))
    builder.row(
        InlineKeyboardButton(
            text=_("Назад на главную ↩️"), callback_data="about_back_to_main"
        )
    )
    image_from_pc = FSInputFile("common/images/image_about.jpg")
    msg = await callback.message.answer_photo(
        image_from_pc,
        caption=_(
            "... в мире, где машины стремятся к господству, он выбрал судьбу героя, "
            "создавая ботов, как первый шаг к спасению человечества через код и умные алгоритмы."
        ),
        reply_markup=builder.adjust(
            2,
            2,
            1,
            1,
        ).as_markup(),
    )

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


# callback "назад на главную"
@private_router.callback_query(F.data == "about_back_to_main")
async def callback_about(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    last_message_id = data.get("last_message_id")

    # Сбрасываем состояние
    await state.set_state(None)

    # Если есть предыдущее сообщение, удаляем его
    if last_message_id:
        try:
            await callback.bot.delete_message(
                chat_id=user_id, message_id=last_message_id
            )
        except Exception as e:
            logger.error("Ошибка при удалении last_message_id сообщения: %s", e)
            # если не удалось удалить last_message_id, то пробуем удалить текущее сообщение
            try:
                await callback.message.delete()
            except Exception as ex:
                logger.error("Ошибка при удалении сообщения: %s", ex)
    else:
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error("Ошибка при удалении сообщения: %s", e)

    await callback.answer(_("Назад на главную ↩️"))
    await callback.message.answer(
        _("Главная панель"),
        reply_markup=keyboard.start_keyboard(
            bot=getattr(callback, "bot", None)
        ),
    )


# кнопка "Котики"
@private_router.message(F.text == CATS)
async def cat_cmd(message: Message, workflow_data: dict[str, object]) -> None:
    """Отправляет архивное фото с эффектом сердечек и обычной реакцией."""
    # вероятность 20% для выбора локального изображения с Кет
    if r.random() < 0.2:
        image_folder = "common/image_cat"
        images = [
            f"common/image_cat/{img}"
            for img in os.listdir(image_folder)
            if img.endswith(".jpg")
        ]

        if images:
            random_image = r.choice(images)
            photo = FSInputFile(random_image)
            try:
                sent_photo = await message.answer_photo(
                    photo, message_effect_id=CAT_HEART_EFFECT_ID
                )
            except TelegramBadRequest as error:
                if "effect" not in error.message.casefold():
                    raise
                logger.warning("Эффект сердечек недоступен; фото отправляется без него")
                sent_photo = await message.answer_photo(photo)
            try:
                await sent_photo.react([ReactionTypeEmoji(emoji="❤")])
            except (
                TelegramAPIError,
                ClientError,
                TimeoutError,
                asyncio.TimeoutError,
            ):
                logger.warning("Не удалось добавить реакцию к архивному фото")
            return
        else:
            await message.answer(_("Локальные изображения не найдены."))

    try:
        data = await get_json(
            "https://api.thecatapi.com/v1/images/search",
            session=getattr(message.bot, "http_session", None),
            timeout=10,
        )
        cat_link = data[0]["url"]
        if not isinstance(cat_link, str) or not cat_link.startswith("https://"):
            raise ValueError("Invalid cat image URL")
        extension = urlsplit(cat_link).path.lower()
        if extension.endswith((".jpg", ".jpeg", ".png", ".webp")):
            await message.answer_photo(cat_link)
        else:
            await message.answer_document(cat_link)
    except (ExternalServiceError, KeyError, IndexError, TypeError, ValueError):
        await message.answer(_("Здесь должна была быть картинка с котиком :("))
