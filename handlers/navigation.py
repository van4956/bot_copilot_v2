"""Глобальные команды доступны из любого диалогового состояния."""

from aiogram import F, Router
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from filters.chat_type import ChatTypeFilter
from handlers import miniapp, other, private

navigation_router = Router()
navigation_router.message.filter(ChatTypeFilter("private"))


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


@navigation_router.message(Command("mini"))
async def open_mini(message: Message, state: FSMContext) -> None:
    """Открывает мини-приложения, не оставляя старый FSM."""
    await reset_dialog(state)
    await miniapp.cmd_miniapp(message)


@navigation_router.message(Command("info"))
async def open_info(
    message: Message,
    state: FSMContext,
    workflow_data: dict,
) -> None:
    """Открывает настройки из любого сценария."""
    await reset_dialog(state)
    await other.process_help_command(message, workflow_data, state)
