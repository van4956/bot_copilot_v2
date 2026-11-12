"""Глобальные команды доступны из любого диалогового состояния."""

import json

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import get_i18n
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from config_data.config import ROOT
from filters.chat_type import ChatTypeFilter
from handlers import cookbook, other, private
from prototypes.wordli.bot_texts import with_game_language_notice

# from handlers import miniapp

navigation_router = Router()
navigation_router.message.filter(ChatTypeFilter("private"))
navigation_router.callback_query.filter(F.message.chat.type == "private")
WORDLI_EMOJI_CAPTURE_PATH = (
    ROOT / ".test-data/wordli-runtime/custom-emoji.json"
)
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


async def _words_button(message: Message, bot: Bot | None = None) -> bool:
    """Принимает новую и прежние подписи разрешённой игры."""
    label = _("Слова 🧩")
    if message.text == label:
        return True
    active_bot = bot if bot is not None else getattr(message, "bot", None)
    return (
        keyboard.wordli_enabled(active_bot)
        and message.text in {
            _("Игры 🕹"),
            label.removesuffix(" 🧩"),
            label.removesuffix(" 🧩") + " 🕹",
        }
    )


@navigation_router.message(_words_button)
async def open_words(
    message: Message, state: FSMContext, bot: Bot | None = None
) -> None:
    """Открывает выбор разрешённых игр или прежнюю заглушку."""
    await reset_dialog(state)
    active_bot = bot if bot is not None else getattr(message, "bot", None)
    url = getattr(active_bot, "wordli_webapp_url", "")
    if keyboard.wordli_enabled(active_bot):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="5БУКВ", web_app=WebAppInfo(url=url)
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=_("Назад на главную ↩️"),
                        callback_data="wordli_back_to_main",
                    )
                ],
            ]
        )
        await message.answer(
            "🕹", parse_mode=None,
            reply_markup=ReplyKeyboardRemove(remove_keyboard=True),
        )
        invitation = with_game_language_notice(
            _("В какую игру сыграем?"), get_i18n().current_locale, _,
        )
        await message.answer(invitation, reply_markup=markup)
        return
    await message.answer(
        _("Данная функция в разработке"),
        reply_markup=keyboard.start_keyboard(bot=active_bot),
    )


@navigation_router.callback_query(F.data == "wordli_dimensions_stub")
async def dimensions_stub(
    callback: CallbackQuery, bot: Bot | None = None
) -> None:
    """Показывает уведомление, не меняя меню и игровой прогресс."""
    active_bot = bot if bot is not None else getattr(callback, "bot", None)
    if (
        not keyboard.wordli_enabled(active_bot)
        or not isinstance(callback.message, Message)
    ):
        await callback.answer(
            _("Мини-приложения временно недоступны"), show_alert=True
        )
        return
    await callback.answer(_("Игра в разработке"), show_alert=True)


def _wordli_custom_emoji(message: Message) -> dict[str, str] | None:
    """Извлекает один эмодзи без постороннего текста или подписей."""
    if message.caption:
        return None
    sticker = message.sticker
    if sticker is not None and sticker.custom_emoji_id and not message.text:
        return {
            "custom_emoji_id": sticker.custom_emoji_id,
            "fallback": sticker.emoji or "",
        }
    text = message.text
    entities = message.entities or []
    if not text or len(entities) != 1:
        return None
    entity = entities[0]
    if entity.type != "custom_emoji" or not entity.custom_emoji_id:
        return None
    fallback = text.strip()
    prefix = text[:len(text) - len(text.lstrip())]
    if (
        not fallback
        or entity.offset != len(prefix.encode("utf-16-le")) // 2
        or entity.length != len(fallback.encode("utf-16-le")) // 2
    ):
        return None
    return {"custom_emoji_id": entity.custom_emoji_id, "fallback": fallback}


def _can_capture_wordli_emoji(
    message: Message, bot: Bot | None = None
) -> bool:
    """Пропускает только разовую отправку владельца тестового бота."""
    active_bot = bot if bot is not None else getattr(message, "bot", None)
    if (
        getattr(active_bot, "test_mode", False) is not True
        or not getattr(active_bot, "wordli_webapp_url", "")
        or message.chat.type != "private"
        or message.from_user is None
        or message.from_user.is_bot
    ):
        return False
    allowed = {
        *(getattr(active_bot, "owner", None) or []),
        *(getattr(active_bot, "admin_list", None) or []),
    }
    if message.from_user.id not in allowed:
        return False
    return (
        _wordli_custom_emoji(message) is not None
        and not WORDLI_EMOJI_CAPTURE_PATH.exists()
    )


@navigation_router.message(_can_capture_wordli_emoji)
async def capture_wordli_emoji(
    message: Message, bot: Bot | None = None
) -> None:
    """Молча сохраняет только ID и замену, не меняя отправляемый эмодзи."""
    if not _can_capture_wordli_emoji(message, bot):
        return
    payload = _wordli_custom_emoji(message)
    if payload is None:
        return
    content = json.dumps(payload, ensure_ascii=False)
    WORDLI_EMOJI_CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with WORDLI_EMOJI_CAPTURE_PATH.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        pass


@navigation_router.callback_query(F.data == "wordli_back_to_main")
async def back_from_wordli(
    callback: CallbackQuery, state: FSMContext, bot: Bot | None = None
) -> None:
    """Удаляет приглашение игры и возвращает главную панель."""
    active_bot = bot if bot is not None else getattr(callback, "bot", None)
    if (
        not keyboard.wordli_enabled(active_bot)
        or not isinstance(callback.message, Message)
    ):
        await callback.answer(
            _("Мини-приложения временно недоступны"), show_alert=True
        )
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest as error:
        if "message to delete not found" not in error.message.casefold():
            raise
    await reset_dialog(state)
    await private.cancel_cmd(callback.message, state)


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
