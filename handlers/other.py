"""Настройки и состояние бота."""

import asyncio
import logging
from datetime import datetime

import psutil
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import FSMI18nMiddleware
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from common.comands import sync_user_commands
from common.locales import SUPPORTED_LOCALES
from database.orm_users import orm_update_locale

logger = logging.getLogger(__name__)


other_router = Router()


async def _status_text() -> str:
    """Описывает состояние процесса на выбранном языке."""
    try:
        process = psutil.Process()
        memory = process.memory_info().rss / 1024 / 1024
        cpu = await asyncio.to_thread(process.cpu_percent, interval=0.1)
        uptime = datetime.now() - datetime.fromtimestamp(process.create_time())
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_uptime = _("{days}д {hours:02}:{minutes:02}:{seconds:02}").format(
            days=uptime.days, hours=hours, minutes=minutes, seconds=seconds,
        )
        return _(
            "📊 Состояние бота — @Terminatorvan_bot\n\n"
            "🔸 <code>Память: {memory:.1f} МБ</code>\n"
            "🔸 <code>CPU: {cpu}%</code>\n"
            "🔸 <code>Время работы: {uptime}</code>\n"
        ).format(memory=memory, cpu=cpu, uptime=formatted_uptime)
    except Exception:
        logger.exception("Не удалось получить состояние процесса")
        return _(
            "📊 Состояние бота — @Terminatorvan_bot\n\n"
            "Не удалось получить данные."
        )


# Этот хэндлер срабатывает на команду /info
@other_router.message(Command("info"))
async def process_help_command(
    message: Message, workflow_data: dict, state: FSMContext
) -> None:
    """Открывает локализованные сведения и настройки бота."""
    await message.answer(_("Информация"), reply_markup=keyboard.del_kb)

    # Создаем инлайн кнопки
    buttons = [
        [InlineKeyboardButton(text=_("Условия использования"), callback_data="terms")],
        [InlineKeyboardButton(text=_("Сменить язык"), callback_data="lang")],
        [InlineKeyboardButton(text=_("Об авторе"), callback_data="author")],
        [InlineKeyboardButton(text=_("Поддержать"), callback_data="donate")],
        [
            InlineKeyboardButton(
                text=_("Назад на главную ↩️"), callback_data="about_back_to_main"
            )
        ],
    ]
    inline_kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await message.answer(text=await _status_text(), reply_markup=inline_kb)

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


# Клавиатура выбора языка
def get_keyboard() -> InlineKeyboardMarkup:
    """Создаёт выбор поддерживаемых языков."""
    button_1 = InlineKeyboardButton(text=_("🇺🇸 Английский"), callback_data="locale_en")
    button_2 = InlineKeyboardButton(text=_("🇷🇺 Русский"), callback_data="locale_ru")
    button_3 = InlineKeyboardButton(text=_("🇪🇸 Испанский"), callback_data="locale_es")
    button_4 = InlineKeyboardButton(text=_("🇺🇦 Украинский"), callback_data="locale_uk")
    button_6 = InlineKeyboardButton(text=_("Назад"), callback_data="back_to_info")
    button_7 = InlineKeyboardButton(
        text=_("Назад на главную ↩️"), callback_data="about_back_to_main"
    )  # обработчик этой кнопки в private.py

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button_1, button_2], [button_3, button_4], [button_6], [button_7],
        ]
    )


# Это хендлер будет срабатывать на нажатие inline кнопки "Сменить язык"
@other_router.callback_query(F.data == "lang")
async def locale_cmd(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает список языков без повторного удаления нижних кнопок."""
    await callback.message.delete()
    # Нижние кнопки уже убраны при открытии информации: не вызываем удаление снова.
    await callback.message.answer(_("Настройки языка"))
    msg = await callback.message.answer(
        text=_("Выберите язык на котором будет работать бот"),
        reply_markup=get_keyboard(),
    )

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


@other_router.callback_query(F.data.startswith("locale_"))
async def update_locale_cmd(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    workflow_data: dict,
    i18n_middleware: FSMI18nMiddleware,
) -> None:
    """Сохраняет язык и сразу обновляет переводы и общую главную панель."""
    target = {f"locale_{code}": code for code in SUPPORTED_LOCALES}.get(callback.data)
    if target is None:
        await callback.answer(_("Язык не поддерживается."), show_alert=True)
        return
    await orm_update_locale(session, callback.from_user.id, target)
    await i18n_middleware.set_locale(state, target)
    await callback.message.delete()
    confirmations = {
        "en": (_("Выбран: 🇺🇸 Английский"), _("Текущий язык \n\n 🇺🇸 Английский")),
        "ru": (_("Выбран: 🇷🇺 Русский язык"), _("Текущий язык \n\n 🇷🇺 Русский")),
        "es": (_("Выбран: 🇪🇸 Испанский"), _("Текущий язык \n\n 🇪🇸 Испанский")),
        "uk": (_("Выбран: 🇺🇦 Украинский"), _("Текущий язык \n\n 🇺🇦 Украинский")),
    }
    popup, header = confirmations[target]
    await callback.answer(popup)
    await callback.message.answer(
        header, reply_markup=keyboard.start_keyboard(bot=callback.bot)
    )
    if isinstance(callback.bot, Bot):
        await sync_user_commands(callback.bot, callback.from_user.id, target)


@other_router.message(Command("data"))
async def data_cmd(message: Message, state: FSMContext) -> None:
    """Показывает служебные данные диалога без изменения их значений."""
    data = await state.get_data()
    await message.answer(str(data))


# Этот хэндлер срабатывает на inline кнопку /terms
@other_router.callback_query(F.data == "terms")
async def terms_cmd(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает условия на выбранном языке."""
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

    await callback.message.answer(_("Условия использования"))
    text = _(
        "Условия использования @Terminatorvan_bot:\n\n"
        "1. Этот бот создан для помощи и развлечения. Он не претендует на мировое господство (пока что).\n\n"
        "2. Бот старается быть точным, но иногда может ошибаться. Он всё-таки не человек, а просто очень умная программа.\n\n"
        "3. Фотографии котиков безопасны и проходят строгий отбор на милоту.\n\n"
        "4. Рецепты из Кулинарной книги проверены на съедобность.\n\n"
        "5. Прогноз погоды и курсы валют берутся из надёжных источников, но используйте эти данные на своё усмотрение.\n\n"
        "6. Все донаты добровольные. Бот будет одинаково дружелюбен ко всем пользователям.\n\n"
    )
    button_1 = InlineKeyboardButton(text=_("Назад"), callback_data="back_to_info")
    button_2 = InlineKeyboardButton(
        text=_("Назад на главную ↩️"), callback_data="about_back_to_main"
    )  # обработчик этой кнопки в private.py
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button_1], [button_2]])

    msg = await callback.message.answer(text, reply_markup=keyboard)

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)


# callback "назад" к настройкам
@other_router.callback_query(F.data == "back_to_info")
async def cmd_callback_about(
    callback: CallbackQuery, state: FSMContext, workflow_data: dict
) -> None:
    """Возвращает локализованную информацию после вложенного раздела."""
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

    # Создаем инлайн кнопки

    buttons = [
        [InlineKeyboardButton(text=_("Условия использования"), callback_data="terms")],
        [InlineKeyboardButton(text=_("Сменить язык"), callback_data="lang")],
        [InlineKeyboardButton(text=_("Об авторе"), callback_data="author")],
        [InlineKeyboardButton(text=_("Поддержать"), callback_data="donate")],
        [
            InlineKeyboardButton(
                text=_("Назад на главную ↩️"), callback_data="about_back_to_main"
            )
        ],
    ]
    inline_kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    msg = await callback.message.answer(
        text=await _status_text(), reply_markup=inline_kb
    )

    # Сохраняем message_id в FSMContext
    await state.update_data(last_message_id=msg.message_id)
