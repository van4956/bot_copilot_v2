import asyncio
import logging

# Инициализируем логгер модуля
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.info("Загружен модуль: %s", __name__)

from icecream import ic
ic.configureOutput(includeContext=True, prefix=' >>> Debag >>> ')

import psutil
from datetime import datetime
from pathlib import Path
import os

from aiogram import Router, F, Bot
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.i18n import gettext as _


from database.orm_users import orm_update_locale
from common import keyboard

# Инициализируем роутер уровня модуля
other_router = Router()


# Этот хэндлер срабатывает на команду /info
@other_router.message(Command('info'))
async def process_help_command(message: Message, workflow_data: dict, state: FSMContext):
    await message.answer(_('Информация'), reply_markup=keyboard.del_kb)

    # Создаем инлайн кнопки
    buttons = [
        [InlineKeyboardButton(text=_('Условия использования'), callback_data='terms')],
        [InlineKeyboardButton(text=_('Сменить язык'), callback_data='lang')],
        [InlineKeyboardButton(text=_('Об авторе'), callback_data='author')],
        [InlineKeyboardButton(text=_('Поддержать'), callback_data='donate')],
        [InlineKeyboardButton(text=_('Назад на главную ↩️'), callback_data='about_back_to_main')]
    ]
    inline_kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Получаем текущий процесс
    try:
        process = psutil.Process()

        memory = process.memory_info().rss / 1024 / 1024
        cpu = process.cpu_percent(interval=0.1) # interval=0.1 - это интервал в секундах

        # Форматируем uptime в дни, часы, минуты и секунды
        uptime = datetime.now() - datetime.fromtimestamp(process.create_time())
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_uptime = f"{days}d {hours:02}:{minutes:02}:{seconds:02}"

        text = (
            f"📊 Status of Bot - @Terminatorvan_bot\n\n"
            f"🔸 <code>RAM:  {memory:.1f}MB</code>\n"
            f"🔸 <code>CPU:  {cpu}%</code>\n"
            f"🔸 <code>Time: {formatted_uptime}</code>\n"
        )

    except Exception as e:
        text = (f"📊 Status of Bot - @Terminatorvan_bot\n\nError: {e}")

    msg = await message.answer(
        text=text,
        reply_markup=inline_kb
    )

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


# Клавиатура выбора языка
def get_keyboard():
    button_1 = InlineKeyboardButton(text=_('🇺🇸 Английский'), callback_data='locale_en')
    button_2 = InlineKeyboardButton(text=_('🇷🇺 Русский'), callback_data='locale_ru')
    # button_3 = InlineKeyboardButton(text=_('🇩🇪 Немецкий'), callback_data='locale_de')
    # button_5 = InlineKeyboardButton(text=_('🇯🇵 Японский'), callback_data='locale_ja')
    button_6 = InlineKeyboardButton(text=_('Назад'), callback_data='back_to_info')
    button_7 = InlineKeyboardButton(text=_('Назад на главную ↩️'), callback_data='about_back_to_main') # обработчик этой кнопки в private.py

    # return InlineKeyboardMarkup(inline_keyboard=[[button_1, button_2], [button_3, button_5], [button_6], [button_7]])
    return InlineKeyboardMarkup(inline_keyboard=[[button_1, button_2], [button_6], [button_7]])

# Это хендлер будет срабатывать на нажатие inline кнопки "Сменить язык"
@other_router.callback_query(F.data == "lang")
async def locale_cmd(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(_("Настройки языка"), reply_markup=keyboard.del_kb)
    msg =await callback.message.answer(
        text=_('Выберите язык на котором будет работать бот'),
        reply_markup=get_keyboard()
    )

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


@other_router.callback_query(F.data.startswith("locale_"))
async def update_locale_cmd(callback: CallbackQuery, session: AsyncSession, state: FSMContext, workflow_data: dict):
    user_id = callback.from_user.id

    if callback.data == 'locale_en':
        await orm_update_locale(session, user_id, 'en')  # Обновляем локаль в бд
        await state.update_data(locale='en')  # Обновляем локаль в контексте
        # await callback.message.edit_text('Choose a language ', reply_markup=None)  # Редактируем сообщение, скрываем inline клавиатуру
        await callback.message.delete()
        await callback.answer("Selected: 🇺🇸 English")  # Отправляем всплывашку
        await callback.message.answer("Current language \n\n 🇺🇸 English", # Отправляем новое сообщение
                                      reply_markup=keyboard.get_keyboard("Weather 🌊", "Currency 💵", "Cats 🐱", "Cookbook 📖", sizes=(2, 2, ), placeholder='⬇️'))

    elif callback.data == 'locale_ru':
        await orm_update_locale(session, user_id, 'ru')  # Обновляем локаль в бд
        await state.update_data(locale='ru')  # Обновляем локаль в контексте
        # await callback.message.edit_text('Выберите язык ', reply_markup=None)   # Редактируем сообщение, скрываем inline клавиатуру
        await callback.message.delete()
        await callback.answer("Выбран: 🇷🇺 Русский язык")  # Отправляем всплывашку
        await callback.message.answer("Текущий язык \n\n 🇷🇺 Русский", # Отправляем новое сообщение
                                      reply_markup=keyboard.get_keyboard("Погода 🌊", "Валюта 💵", "Котики 🐱", "Книга 📖", sizes=(2, 2, ), placeholder='⬇️'))

    # elif callback.data == 'locale_de':
    #     await orm_update_locale(session, user_id, 'de')  # Обновляем локаль в бд
    #     await state.update_data(locale='de')  # Обновляем локаль в контексте
    #     # await callback.message.edit_text('Wählen Sie eine Sprache ', reply_markup=None)  # type: ignore # Редактируем сообщение,скрываем клавиатуру
    #     await callback.message.delete()
    #     await callback.answer("Ausgewählt: 🇩🇪 Deutsch")  # Отправляем всплывашку
    #     await callback.message.answer("Aktuelle Sprache \n\n 🇩🇪 Deutsch",   # Отправляем новое сообщение
    #                                   reply_markup=keyboard.get_keyboard("Wetter 🌊", "Währung 💵", "Katzen 🐱", "LLMs 🤖", sizes=(2, 2, ), placeholder='⬇️'))

    # elif callback.data == 'locale_ja':
    #     await orm_update_locale(session, user_id, 'ja')  # Обновляем локаль в бд
    #     await state.update_data(locale='ja')  # Обновляем локаль в контексте
    #     # await callback.message.edit_text('言語を選択してください ', reply_markup=None)  # type: ignore # Редактируем сообщение,скрываем клавиатуру
    #     await callback.message.delete()
    #     await callback.answer("選択された: 🇯🇵 日本語")  # Отправляем всплывашку
    #     await callback.message.answer("現在の言語 \n\n 🇯🇵 日本語",   # Отправляем новое сообщение
    #                                   reply_markup=keyboard.get_keyboard("テンキ 🌊", "カワセ 💵", "ネコ 🐱", "エルエルエム 🤖", sizes=(2, 2, ), placeholder='⬇️'))


# секретный хендлер, покажет содержимое data пользователя
@other_router.message(Command("data"))
async def data_cmd(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer(str(data))


# Этот хэндлер срабатывает на inline кнопку /terms
@other_router.callback_query(F.data == "terms")
async def terms_cmd(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    last_message_id = data.get('last_message_id')

    # Если есть предыдущее сообщение, удаляем его
    if last_message_id:
        try:
            await callback.bot.delete_message(chat_id=user_id,
                                                message_id=last_message_id)
        except Exception as e:
            logger.error("Ошибка при удалении last_message_id сообщения: %s", e)
    else:
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error("Ошибка при удалении сообщения: %s", e)

    await callback.message.answer(_("Условия использования"))
    text = _("Terms of use @Terminatorvan_bot:\n\n"
              "1. Этот бот создан для помощи и развлечения. Он не претендует на мировое господство (пока что).\n\n"
              "2. Бот старается быть точным, но иногда может ошибаться. Он всё-таки не человек, а просто очень умная программа.\n\n"
              "3. Фотографии котиков безопасны и проходят строгий отбор на милоту.\n\n"
              "4. Прогноз погоды и курсы валют берутся из надёжных источников, но используйте эти данные на своё усмотрение.\n\n"
              "5. Рецепты из Кулинарной книги проверены на съедобность. Но за ваши кулинарные эксперименты бот не в ответе!\n\n"
              "6. Калькулятор пиццы поможет с расчётами, но окончательный выбор пиццы всегда за вами!\n\n"
              "7. Все донаты добровольные. Бот будет одинаково дружелюбен ко всем пользователям.\n\n"
              "8. В случае сбоев не переживайте - просто подождите немного или перезапустите бота.\n\n")
    button_1 = InlineKeyboardButton(text=_("Назад"), callback_data="back_to_info")
    button_2 = InlineKeyboardButton(text=_('Назад на главную ↩️'), callback_data='about_back_to_main') # обработчик этой кнопки в private.py
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button_1], [button_2]])

    msg = await callback.message.answer(text, reply_markup=keyboard)

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


# callback "назад" к настройкам
@other_router.callback_query(F.data == 'back_to_info')
async def cmd_callback_about(callback: CallbackQuery, state: FSMContext, workflow_data: dict):
    user_id = callback.from_user.id
    data = await state.get_data()
    last_message_id = data.get('last_message_id')

    # Если есть предыдущее сообщение, удаляем его
    if last_message_id:
        try:
            await callback.bot.delete_message(chat_id=user_id,
                                                message_id=last_message_id)
        except Exception as e:
            logger.error("Ошибка при удалении last_message_id сообщения: %s", e)
    else:
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error("Ошибка при удалении сообщения: %s", e)

    # Создаем инлайн кнопки

    buttons = [
        [InlineKeyboardButton(text=_('Условия использования'), callback_data='terms')],
        [InlineKeyboardButton(text=_('Сменить язык'), callback_data='lang')],
        [InlineKeyboardButton(text=_('Об авторе'), callback_data='author')],
        [InlineKeyboardButton(text=_('Поддержать'), callback_data='donate')],
        [InlineKeyboardButton(text=_('Назад на главную ↩️'), callback_data='about_back_to_main')]
    ]
    inline_kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Получаем текущий процесс
    try:
        process = psutil.Process()

        memory = process.memory_info().rss / 1024 / 1024  # Получаем использование памяти в мегабайтах
        cpu = process.cpu_percent(interval=0.1) # interval=0.1 - это интервал в секундах

        # Форматируем uptime в дни, часы, минуты и секунды
        uptime = datetime.now() - datetime.fromtimestamp(process.create_time())
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_uptime = f"{days}d {hours:02}:{minutes:02}:{seconds:02}"

        text = (
            f"📊 Status of Bot - @Terminatorvan_bot\n\n"
            f"🔸 <code>RAM:  {memory:.1f}MB</code>\n"
            f"🔸 <code>CPU:  {cpu}%</code>\n"
            f"🔸 <code>Time: {formatted_uptime}</code>\n"
        )

    except Exception as e:
        text = (f"📊 Status of Bot - @Terminatorvan_bot\n\nError: {e}")

    msg = await callback.message.answer(
        text=text,
        reply_markup=inline_kb
    )

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)
