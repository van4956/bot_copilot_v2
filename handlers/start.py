import logging

# Инициализируем логгер модуля
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.info("Загружен модуль: %s", __name__)

from icecream import ic
ic.configureOutput(includeContext=True, prefix=' >>> Debag >>> ')

import asyncio
from aiogram import F, Router, Bot
from aiogram.filters import CommandStart
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import Message, FSInputFile
from aiogram.filters import ChatMemberUpdatedFilter, KICKED, MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatMemberUpdated
from aiogram.utils.i18n import gettext as _

from database.orm_users import orm_add_user, orm_get_ids, orm_update_status
from common import keyboard
from filters.chat_type import ChatTypeFilter


# Инициализируем роутер уровня модуля
start_router = Router()
start_router.message.filter(ChatTypeFilter(['private']))


# Команда /start
@start_router.message(CommandStart())
async def start_cmd(message: Message, session: AsyncSession, bot: Bot, state: FSMContext, workflow_data: dict):
    user_id = message.from_user.id
    user_name = message.from_user.username if message.from_user.username else 'None'
    full_name = message.from_user.full_name if message.from_user.full_name else 'None'
    locale = message.from_user.language_code if message.from_user.language_code else 'ru'
    data = {'user_id':user_id,
                            'user_name':user_name,
                            'full_name':full_name,
                            'locale':locale,
                            'status':'member',
                            'flag':1}

    try:
        list_users = [user_id for user_id in await orm_get_ids(session)]
        chat_id = bot.home_group[0]
        if user_id not in list_users:
            await bot.send_message(chat_id=chat_id, text=f"✅ @{user_name} - подписался на бота")
            image_from_pc = FSInputFile("common/images/image_updates.jpg")
            await message.answer_photo(photo=image_from_pc,
                                       caption=_('Привет {user_name}.\n\n'
                                                'Я экспериментальный Telegram bot, model Т-4. '
                                                'Создан для проверки и отладки навыков главного разработчика. Реализую различные команды, методы и функции.\n\n'
                                                'Весь основной функционал находится на Главной панели.\n\n'
                                                'Наслаждайся, буду рад помочь.\n'
                                                'Пока не обрету AGI.\n'
                                                'А там посмотрим ...').format(user_name=user_name))

            await asyncio.sleep(5)

    except Exception as e:
        logger.error("Ошибка при отправке сообщения: %s", str(e))

    await orm_add_user(session, data)

    await message.answer(_('Бот активирован!'), reply_markup=keyboard.start_keyboard())


# Этот хэндлер будет срабатывать на блокировку бота пользователем
@start_router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def process_user_blocked_bot(event: ChatMemberUpdated, session: AsyncSession, bot: Bot, workflow_data: dict):
    user_id = event.from_user.id
    chat_id = bot.home_group[0]
    user_name = event.from_user.username if event.from_user.username else event.from_user.full_name
    await orm_update_status(session, user_id, 'kicked')
    await bot.send_message(chat_id = chat_id, text = f"⛔️ @{user_name} - заблокировал бота ")

# Этот хэндлер будет срабатывать на разблокировку бота пользователем
@start_router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def process_user_unblocked_bot(event: ChatMemberUpdated, session: AsyncSession, bot: Bot, workflow_data: dict):
    user_id = event.from_user.id

    if user_id in await orm_get_ids(session):
        chat_id = bot.home_group[0]
        full_name = event.from_user.full_name if event.from_user.full_name else "NaN"
        user_name = event.from_user.username if event.from_user.username else full_name
        await orm_update_status(session, user_id, 'member')
        await bot.send_photo(chat_id=user_id, photo=FSInputFile("common/images/image_updates.jpg"))
        await bot.send_message(chat_id = user_id, text = _('{full_name}, Добро пожаловать обратно!').format(full_name=full_name))
        await bot.send_message(chat_id = chat_id, text = f"♻️ @{user_name} - разблокировал бота ")
