"""Глобальные команды доступны из любого диалогового состояния."""

from aiogram import F, Router
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from filters.chat_type import ChatTypeFilter
from handlers import cookbook, other, private

# from handlers import miniapp

navigation_router = Router()
navigation_router.message.filter(ChatTypeFilter("private"))
navigation_router.callback_query.filter(F.message.chat.type == "private")
WORDS = __("Слова 🧩")
DISABLED_MINIAPP_CALLBACKS = {
    "service_miniapp",
    "game_miniapp",
    "back_to_mini",
    "mini_back_to_main",
}


async def reset_dialog(state: FSMContext) -> None:
    """Сбрасывает незавершённый диалог, сохраняя выбранный язык."""
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data({"locale": data["locale"]} if data.get("locale") else {})


@navigation_router.message(or_f(Command("main"), F.text == private.BACK_TO_MAIN))
async def open_main(message: Message, state: FSMContext) -> None:
    """Открывает главную из любого сценария."""
    await reset_dialog(state)
    await private.cancel_cmd(message, state)


# Мини-приложения временно скрыты. Для возврата раздела нужны его роутеры в app.py.
# @navigation_router.message(Command("mini"))
# async def open_mini(message: Message, state: FSMContext) -> None:
#     await reset_dialog(state)
#     await miniapp.cmd_miniapp(message)


@navigation_router.message(Command("book"))
async def open_book(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Открывает прежнюю книгу из любого диалога, сохраняя её страницу."""
    data = await state.get_data()
    await reset_dialog(state)
    if "page" in data:
        await state.update_data(page=data["page"])
    await cookbook.process_cookbook_command(message, state, session)


@navigation_router.message(F.text == WORDS)
async def open_words(message: Message, state: FSMContext) -> None:
    """Показывает заглушку будущей игры в слова."""
    await reset_dialog(state)
    await message.answer(
        _("Данная функция в разработке"), reply_markup=keyboard.start_keyboard()
    )


@navigation_router.callback_query(F.data.in_(DISABLED_MINIAPP_CALLBACKS))
async def unavailable_miniapp_callback(callback: CallbackQuery) -> None:
    """Отвечает на старые кнопки без запуска скрытого раздела."""
    await callback.answer(_("Мини-приложения временно недоступны"), show_alert=True)


@navigation_router.message(F.web_app_data)
async def unavailable_miniapp_result(message: Message) -> None:
    """Не передаёт старый результат игры в текущий диалог или базу."""
    await message.answer(_("Мини-приложения временно недоступны"))


@navigation_router.message(Command("info"))
async def open_info(
    message: Message,
    state: FSMContext,
    workflow_data: dict,
) -> None:
    """Открывает настройки из любого сценария."""
    await reset_dialog(state)
    await other.process_help_command(message, workflow_data, state)
