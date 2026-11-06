"""Просмотр книги рецептов с безопасными подписями и пагинацией."""

import logging
from html import escape
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from database.orm_cookbook import orm_get_recipes
from filters.chat_type import ChatTypeFilter

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
cookbook_router = Router()
cookbook_router.message.filter(ChatTypeFilter(["private"]))
cookbook_router.callback_query.filter(F.message.chat.type == "private")
BOOK = __("Книга 📖")


def _clip_text(value: str, limit: int) -> str:
    """Ограничивает видимый текст с учётом символов Telegram."""
    if len(value.encode("utf-16-le")) // 2 <= limit:
        return value
    clipped = value.encode("utf-16-le")[: max(0, limit - 1) * 2]
    return clipped.decode("utf-16-le", errors="ignore") + "…"


def format_recipe_caption(recipe: Any) -> str:
    """Создаёт экранированную подпись, не разрезая HTML-разметку."""
    name = _clip_text(str(recipe.recipe_name or ""), 150)
    author = _clip_text(str(recipe.author or ""), 150)
    heading = f"{name}\nАвтор: {author}\n\n"
    remaining = 1024 - len(heading.encode("utf-16-le")) // 2
    description = _clip_text(str(recipe.description or ""), remaining)
    return (
        f"<b>{escape(name)}</b>\n<i>Автор: {escape(author)}</i>\n\n"
        f"{escape(description)}"
    )


def resolve_recipe_photo(
    image: str | None, *, test_mode: bool = False
) -> str | FSInputFile:
    """Разрешает file_id и строго ограниченные тестовые картинки."""
    if image and image.startswith("test-fixture:"):
        number = image.removeprefix("test-fixture:")
        if not test_mode or number not in {"1", "2", "3", "4", "5"}:
            raise ValueError("Тестовое изображение запрещено в этом режиме")
        path = (
            PROJECT_ROOT / "testing" / "fixtures" / "images"
            / f"test_image_{number}.png"
        )
        if not path.is_file():
            raise ValueError("Тестовое изображение не найдено")
        return FSInputFile(path)
    if image:
        return image
    return FSInputFile(PROJECT_ROOT / "common" / "images" / "image_cookbook.jpg")


def normalize_page(value: Any, total: int) -> int:
    """Возвращает допустимую страницу непустой книги."""
    try:
        page = int(value)
    except (TypeError, ValueError, OverflowError):
        page = 1
    return min(max(page, 1), max(total, 1))


def _page_keyboard(page: int, total: int) -> Any:
    """Создаёт кнопки навигации по книге."""
    return keyboard.get_callback_btns(
        btns={
            " << ": "backward",
            f"{page}/{total}": "curr_page",
            " >> ": "forward",
            _("Назад на главную ↩️"): "cookbook_back",
        },
        sizes=(3, 1),
    )


@cookbook_router.message(F.text == BOOK)
async def process_cookbook_command(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Открывает книгу и восстанавливает допустимую страницу."""
    try:
        book = await orm_get_recipes(session)
        if not book:
            await state.update_data(page=1)
            await message.answer(
                _("Книга рецептов пуста"),
                reply_markup=keyboard.start_keyboard(
                    bot=getattr(message, "bot", None)
                ),
            )
            return
        page = normalize_page((await state.get_data()).get("page"), len(book))
        recipe = book[page - 1]
        await message.answer_photo(
            photo=resolve_recipe_photo(
                recipe.image, test_mode=getattr(message.bot, "test_mode", False)
            ),
            caption=format_recipe_caption(recipe),
            reply_markup=_page_keyboard(page, len(book)),
        )
        await state.update_data(page=page, cookbook_recipe_id=recipe.recipe_id)
    except (SQLAlchemyError, TelegramAPIError, ValueError):
        logger.exception("Не удалось открыть книгу рецептов")
        await message.answer(_("Ошибка при выполнении команды /cookbook"))


async def _turn_page(
    callback: CallbackQuery, state: FSMContext,
    session: AsyncSession, direction: int,
) -> None:
    """Перелистывает книгу с учётом удалений и устаревшего состояния."""
    if not isinstance(callback.message, Message):
        await callback.answer(_("Откройте книгу заново"))
        return
    try:
        book = await orm_get_recipes(session)
        if not book:
            await state.update_data(page=1)
            await callback.answer(_("Книга рецептов пуста"))
            await callback.message.delete()
            await callback.message.answer(
                _("Книга рецептов пуста"),
                reply_markup=keyboard.start_keyboard(
                    bot=getattr(callback, "bot", None)
                ),
            )
            return
        state_data = await state.get_data()
        saved = state_data.get("page", 1)
        current = normalize_page(saved, len(book))
        page = normalize_page(current + direction, len(book))
        recipe = book[page - 1]
        needs_refresh = (
            page != current or saved != current
            or state_data.get("cookbook_recipe_id") != recipe.recipe_id
        )
        if needs_refresh:
            await callback.message.edit_media(
                media=InputMediaPhoto(
                    media=resolve_recipe_photo(
                        recipe.image,
                        test_mode=getattr(callback.bot, "test_mode", False),
                    ),
                    caption=format_recipe_caption(recipe),
                ),
                reply_markup=_page_keyboard(page, len(book)),
            )
        await state.update_data(page=page, cookbook_recipe_id=recipe.recipe_id)
        await callback.answer()
    except (SQLAlchemyError, TelegramAPIError, ValueError):
        logger.exception("Не удалось перелистнуть книгу рецептов")
        await callback.answer(_("Не удалось открыть страницу"), show_alert=True)


@cookbook_router.callback_query(F.data == "forward")
async def process_forward_press(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Показывает следующую страницу."""
    await _turn_page(callback, state, session, 1)


@cookbook_router.callback_query(F.data == "backward")
async def process_backward_press(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Показывает предыдущую страницу."""
    await _turn_page(callback, state, session, -1)


@cookbook_router.callback_query(F.data == "curr_page")
async def process_curr_page_press(callback: CallbackQuery) -> None:
    """Подтверждает нажатие индикатора страницы."""
    await callback.answer("🔥")


@cookbook_router.callback_query(F.data == "cookbook_back")
async def process_cookbook_back_press(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Возвращает пользователя на главную панель."""
    await state.set_state(None)
    await callback.answer(_("Назад на главную ↩️"))
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.info("Старая карточка книги уже недоступна")
        await callback.message.answer(
            _("Главная панель"),
            reply_markup=keyboard.start_keyboard(
                bot=getattr(callback, "bot", None)
            ),
        )
