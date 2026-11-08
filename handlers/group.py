"""Служебные события групп и локализованная команда идентификаторов."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from filters.chat_type import ChatTypeFilter

logger = logging.getLogger(__name__)
group_router = Router()
group_router.message.filter(
    ChatTypeFilter(["private", "group", "supergroup", "channel"])
)


@group_router.message(F.content_type.in_({"new_chat_members", "left_chat_member"}))
async def on_user_join_or_left(message: Message) -> None:
    """Удаляет служебное сообщение о вступлении или выходе участника."""
    await message.delete()


@group_router.message(Command("get_id"))
async def get_chat_id_cmd(message: Message) -> None:
    """Показывает идентификаторы, не меняя язык или состояние диалога."""
    user_id = message.from_user.id if message.from_user is not None else "—"
    await message.answer(
        _("ID чата: <code>{chat_id}</code>\nID пользователя: <code>{user_id}</code>").format(
            chat_id=message.chat.id, user_id=user_id
        )
    )
