import asyncio
import logging
import random

# Инициализируем логгер модуля
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.info("Загружен модуль: %s", __name__)

from icecream import ic
ic.configureOutput(includeContext=True, prefix=' >>> Debag >>> ')

from aiogram import F, Router
from aiogram.types import Message, CallbackQuery, InputMediaPhoto, FSInputFile
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __

from common import keyboard
from database.orm_cookbook import orm_get_recipes
from filters.chat_type import ChatTypeFilter

# создаем роутер для книги рецептов
cookbook_router = Router()
cookbook_router.message.filter(ChatTypeFilter(['private']))
cookbook_router.edited_message.filter(ChatTypeFilter(['private']))

# Создаем константы для текстов, которые используются в декораторах
BACK_TO_MAIN = __("Назад на главную ↩️")
BOOK = __("Книга 📖")

# Этот хэндлер будет срабатывать на команду "/cookbook"
# и отправлять пользователю первую страницу книги с кнопками пагинации
# @cookbook_router.message(Command(commands='book'))
@cookbook_router.message(F.text == BOOK)
async def process_cookbook_command(message: Message, state: FSMContext, session: AsyncSession):
    # photo = FSInputFile("common/images/image_cook.jpg")
    await message.answer(text=_("Книга рецептов"),
                         reply_markup=keyboard.del_kb)
    user_id = message.from_user.id
    await asyncio.sleep(1)

    try:
        book = await orm_get_recipes(session)
        len_page = len(book)
        state_data = await state.get_data()
        users_page = state_data.get('page', 1)  # получаем сохраненную страницу книги, либо устанавливаем ее на 1

        # Проверяем, что страница находится в допустимых пределах
        if users_page < 1 or users_page > len_page or len_page == 0:
            await message.answer(_("Книга рецептов пуста"), reply_markup=keyboard.start_keyboard())
            return

        # Получаем рецепт по индексу (страница - 1, т.к. индексы с 0)
        recipe = book[users_page - 1]
        text = f"<b>{recipe.recipe_name}</b>\n<i>Автор: {recipe.author}</i>\n\n{recipe.description}"
        photo = recipe.image

        # Обрезаем подпись, если она превышает 1024 символа
        if len(text) > 1024:
            text = text[:1021] + "..."

        await message.answer_photo(photo=photo,
                                    caption=text,
                                    reply_markup=keyboard.get_callback_btns(btns={' << ': 'backward',
                                                                                    f'{users_page}/{len_page}': 'curr_page',
                                                                                    ' >> ': 'forward',
                                                                                    _("Назад на главную ↩️"):'cookbook_back'},
                                                                                sizes=(3,1,)),
                                                                                )

    except (SQLAlchemyError, ValueError) as e:
        logger.error("Ошибка при выполнении команды /cookbook: %s", e)
        await message.answer(_("Ошибка при выполнении команды /cookbook"), reply_markup=keyboard.start_keyboard())

# Этот хэндлер будет срабатывать на нажатие инлайн-кнопки "вперед"
# во время взаимодействия пользователя с сообщением-книгой
@cookbook_router.callback_query(F.data == 'forward')
async def process_forward_press(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        book = await orm_get_recipes(session)
        len_page = len(book)
        state_data = await state.get_data()
        users_page = state_data.get('page', 1)

        if users_page < len_page:
            users_page += 1
            await state.update_data(page=users_page)

            # Получаем рецепт по индексу (страница - 1, т.к. индексы с 0)
            recipe = book[users_page - 1]
            text = f"<b>{recipe.recipe_name}</b>\n<i>Автор: {recipe.author}</i>\n\n{recipe.description}"
            photo = recipe.image

            # Обрезаем подпись, если она превышает 1024 символа
            if len(text) > 1024:
                text = text[:1021] + "..."

            await callback.message.edit_media(media=InputMediaPhoto(media=photo, caption=text),
                                                reply_markup=keyboard.get_callback_btns(btns={' << ': 'backward', # type: ignore
                                                                                             f'{users_page}/{len_page}': 'curr_page',
                                                                                            ' >> ': 'forward',
                                                                                            _("Назад на главную ↩️"):'cookbook_back'},
                                                                                            sizes=(3,1,))
                                                                                            )

        await callback.answer()

    except (SQLAlchemyError, TelegramAPIError) as e:
        logger.error("Ошибка при выполнении inline кнопки '>>': %s", e)
        await callback.answer(_("Ошибка при выполнении inline кнопки '>>'"), reply_markup=keyboard.start_keyboard())



# Этот хэндлер будет срабатывать на нажатие инлайн-кнопки "назад"
# во время взаимодействия пользователя с сообщением-книгой
@cookbook_router.callback_query(F.data == 'backward')
async def process_backward_press(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        book = await orm_get_recipes(session)
        len_page = len(book)
        state_data = await state.get_data()
        users_page = state_data.get('page', 1)

        if users_page > 1 and users_page <= len_page:
            users_page -= 1
            await state.update_data(page=users_page)

            # Получаем рецепт по индексу (страница - 1, т.к. индексы с 0)
            recipe = book[users_page - 1]
            text = f"<b>{recipe.recipe_name}</b>\n<i>Автор: {recipe.author}</i>\n\n{recipe.description}"
            photo = recipe.image

            # Обрезаем подпись, если она превышает 1024 символа
            if len(text) > 1024:
                text = text[:1021] + "..."

            await callback.message.edit_media(media=InputMediaPhoto(media=photo, caption=text),
                                              reply_markup=keyboard.get_callback_btns(btns={' << ': 'backward', # type: ignore
                                                                                            f'{users_page}/{len_page}': 'curr_page',
                                                                                            ' >> ': 'forward',
                                                                                            _("Назад на главную ↩️"):'cookbook_back'},
                                                                                    sizes=(3,1,)))

        await callback.answer()

    except (SQLAlchemyError, TelegramAPIError) as e:
        logger.error("Ошибка при выполнении inline кнопки '<<': %s", e)
        await callback.answer(_("Ошибка при выполнении inline кнопки '<<'"), reply_markup=keyboard.start_keyboard())

# Этот хэндлер будет срабатывать на нажатие инлайн-кнопки "текущая страница"
# во время взаимодействия пользователя с сообщением-книгой
@cookbook_router.callback_query(F.data == 'curr_page')
async def process_curr_page_press(callback: CallbackQuery):
    # message_effect = {"🔥": "5104841245755180586",
    #                                     "👍": "5107584321108051014",
    #                                     "👎": "5104858069142078462",
    #                                     "❤️": "5159385139981059251",
    #                                     "🎉": "5046509860389126442",
    #                                     "💩": "5046589136895476101"}
    # random_emoji, random_effect = random.choice(list(message_effect.items()))
    # emoji_message = await callback.message.answer(text="🔥",message_effect_id='5104841245755180586')
    # await asyncio.sleep(2)
    # await emoji_message.delete()
    await callback.answer("🔥")

# Этот хэндлер будет срабатывать на нажатие инлайн-кнопки "назад на главную"
# во время взаимодействия пользователя с сообщением-книгой
@cookbook_router.callback_query(F.data == 'cookbook_back')
async def process_cookbook_back_press(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        # book = await orm_get_recipes(session)
        # state_data = await state.get_data()
        # users_page = state_data.get('page', 1)
        # caption = [(f"<b>{rec.recipe_name}</b>\n<i>Автор: {rec.author}</i>\n\n{rec.description}", rec.image) for rec in book if rec.recipe_id == users_page]
        # text = caption[0][0]
        # photo = caption[0][1]
        # await callback.message.edit_media(media=InputMediaPhoto(media=photo, caption=text), reply_markup=None)
        await callback.message.delete()
        await state.set_state(None)
        await callback.answer(_("Назад на главную ↩️"))
        await asyncio.sleep(1)
        await callback.message.answer(_('Главная панель'), reply_markup=keyboard.start_keyboard())

    except (SQLAlchemyError, TelegramAPIError) as e:
        logger.error("Ошибка при выполнении inline кнопки 'Назад на главную ↩️': %s", e)
        await callback.message.answer(_("Ошибка при выполнении inline кнопки 'Назад на главную ↩️'"), reply_markup=keyboard.start_keyboard())
        await state.set_state(None)
