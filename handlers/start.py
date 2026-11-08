"""Регистрация, запуск диалога и личные события блокировки бота."""

import logging
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import (
    KICKED,
    MEMBER,
    ChatMemberUpdatedFilter,
    CommandStart,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatMemberUpdated, FSInputFile, Message
from aiogram.utils.i18n import FSMI18nMiddleware
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from common.comands import sync_user_commands
from database.orm_users import (
    SUPPORTED_LOCALES,
    orm_add_user,
    orm_get_user,
    orm_update_status,
)

logger = logging.getLogger(__name__)
IMAGE_PATH = (
    Path(__file__).resolve().parents[1] / "common/images/image_updates.jpg"
)
start_router = Router()
start_router.message.filter(F.chat.type == "private")
start_router.my_chat_member.filter(F.chat.type == "private")


async def _notify_service(bot: Bot, text: str) -> None:
    """Отправляет необязательное служебное уведомление вне тестового режима."""
    if getattr(bot, "test_mode", False):
        return
    if not getattr(bot, "notify_service_chat", True):
        return
    group = next(iter(getattr(bot, "home_group", []) or []), None)
    if group is None:
        return
    try:
        await bot.send_message(chat_id=group, text=text)
    except Exception:
        logger.exception("Не удалось отправить служебное уведомление")


@start_router.message(CommandStart())
async def start_cmd(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    workflow_data: dict[str, Any],
    i18n_middleware: FSMI18nMiddleware | None = None,
) -> None:
    """Сначала регистрирует пользователя, затем показывает интерфейс."""
    actor = message.from_user
    if actor is None:
        return
    saved = await state.get_data()
    user_locale = saved.get("locale")
    if user_locale not in SUPPORTED_LOCALES:
        user_locale = (actor.language_code or "ru").split("-")[0]
    user, created = await orm_add_user(
        session,
        {
            "user_id": actor.id,
            "user_name": actor.username,
            "full_name": actor.full_name,
            "locale": user_locale,
            "status": "member",
            "flag": 1,
        },
    )
    await state.set_state(None)
    await state.set_data({"locale": user.locale})
    if i18n_middleware is not None:
        await i18n_middleware.set_locale(state, user.locale)
    user_label = escape(actor.username or actor.full_name or _("Пользователь"))
    if created:
        try:
            await message.answer_photo(
                photo=FSInputFile(IMAGE_PATH),
                caption=_(
                    "Привет {user_name}.\n\n"
                    "Я экспериментальный Telegram bot, model Т-4. "
                    "Создан для проверки и отладки навыков главного "
                    "разработчика. Реализую различные команды, методы "
                    "и функции.\n\n"
                    "Весь основной функционал находится на Главной панели.\n\n"
                    "Наслаждайся, буду рад помочь.\n"
                    "Пока не обрету AGI.\nА там посмотрим ..."
                ).format(user_name=user_label),
            )
        except Exception:
            logger.exception("Не удалось отправить приветственное изображение")
        await _notify_service(
            bot, f"✅ {user_label} - подписался на бота"
        )
    await message.answer(
        _("Бот активирован!"), reply_markup=keyboard.start_keyboard(bot=bot)
    )
    if isinstance(bot, Bot):
        await sync_user_commands(bot, actor.id, user.locale)


@start_router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=KICKED)
)
async def process_user_blocked_bot(
    event: ChatMemberUpdated,
    session: AsyncSession,
    bot: Bot,
    workflow_data: dict[str, Any],
) -> None:
    """Сохраняет блокировку только в личном диалоге."""
    await orm_update_status(session, event.chat.id, "kicked")
    user_label = escape(
        event.from_user.username or event.from_user.full_name
    )
    await _notify_service(bot, f"⛔️ {user_label} - заблокировал бота")


@start_router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=MEMBER)
)
async def process_user_unblocked_bot(
    event: ChatMemberUpdated,
    session: AsyncSession,
    bot: Bot,
    workflow_data: dict[str, Any],
) -> None:
    """Восстанавливает статус известного пользователя после разблокировки."""
    user = await orm_get_user(session, event.chat.id)
    if user is None:
        return
    await orm_update_status(session, event.chat.id, "member")
    full_name = escape(event.from_user.full_name or _("Пользователь"))
    try:
        await bot.send_message(
            chat_id=event.chat.id,
            text=_("{full_name}, Добро пожаловать обратно!").format(
                full_name=full_name
            ),
        )
    except Exception:
        logger.exception("Не удалось поприветствовать разблокировавшего бота")
    await _notify_service(
        bot, f"♻️ {full_name} - разблокировал бота"
    )
