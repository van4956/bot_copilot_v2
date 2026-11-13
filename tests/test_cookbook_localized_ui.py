"""Локализованная книга и обновление открытой карточки без Telegram."""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.utils.i18n import I18n
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from config_data.config import ROOT
from database.models import Base
from database.orm_cookbook import orm_add_recipe, orm_delete_recipe, orm_update_recipe
from database.orm_cookbook_translations import (
    RecipeTranslation,
    orm_list_pending_translations,
    orm_save_translations,
)
from handlers import cookbook


def values(
    name: str = "Суп",
    description: str = "Добавить 100 г риса.",
) -> dict[str, Any]:
    """Возвращает исходный русский рецепт с обычным Telegram file_id."""
    return {
        "recipe_name": name,
        "description": description,
        "author": "Иван",
        "image": "offline-file-id",
        "price": 1,
    }


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """Открывает изолированную временную БД и исходную запись."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'book-ui.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as active:
            await orm_add_recipe(active, values())
            yield active
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def state() -> AsyncIterator[FSMContext]:
    """Изолирует страницу и выбранный язык в памяти."""
    storage = MemoryStorage()
    try:
        yield FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    finally:
        await storage.close()


def message() -> Mock:
    """Подменяет сообщения и фотографии без настоящего клиента."""
    result = Mock(spec=Message)
    result.bot = SimpleNamespace(test_mode=False)
    result.answer = AsyncMock()
    result.answer_photo = AsyncMock()
    result.edit_media = AsyncMock()
    result.delete = AsyncMock()
    return result


async def publish_english(session: AsyncSession) -> None:
    """Записывает заранее подготовленный перевод без вызова API."""
    source = (await orm_list_pending_translations(session))[0]
    await orm_save_translations(
        session,
        [
            RecipeTranslation(
                source=source,
                recipe_name="Rice soup",
                description="Add 100 g of rice.",
                model="gpt-5.4-mini",
            )
        ],
    )


@pytest.mark.parametrize(
    "locale,translated", [
        ("ru", True), ("en", True), ("en", False), ("de", True),
        ("es", True), ("uk", True), ("es", False), ("uk", False),
    ]
)
async def test_open_book_selects_content_language_and_preserves_locale(
    session: AsyncSession,
    state: FSMContext,
    locale: str,
    translated: bool,
) -> None:
    """RU получает оригинал, остальные EN либо RU при отсутствии перевода."""
    if translated:
        await publish_english(session)
    await state.update_data(locale=locale)
    i18n = I18n(path=ROOT / "locales", default_locale="ru", domain="bot_00_template")
    outgoing = message()
    with i18n.context(), i18n.use_locale(locale):
        await cookbook.process_cookbook_command(outgoing, state, session)
    caption = outgoing.answer_photo.await_args.kwargs["caption"]
    expected = "Rice soup" if translated and locale != "ru" else "Суп"
    assert f"<b>{expected}</b>" in caption
    assert "Иван" in caption
    assert outgoing.answer_photo.await_args.kwargs["photo"] == "offline-file-id"
    assert (await state.get_data())["locale"] == locale


async def test_pagination_refreshes_translation_and_then_keeps_stale_english(
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Синхрон обновляет открытую страницу; последующая RU-правка не скрывает EN."""
    i18n = I18n(path=ROOT / "locales", default_locale="ru", domain="bot_00_template")
    outgoing = message()
    callback = SimpleNamespace(message=outgoing, bot=outgoing.bot, answer=AsyncMock())
    with i18n.context(), i18n.use_locale("en"):
        await cookbook.process_cookbook_command(outgoing, state, session)
        assert "<b>Суп</b>" in outgoing.answer_photo.await_args.kwargs["caption"]
        await publish_english(session)
        await cookbook.process_backward_press(callback, state, session)
        assert "Rice soup" in outgoing.edit_media.await_args.kwargs["media"].caption
        outgoing.edit_media.reset_mock()
        await orm_update_recipe(session, 1, values("Другой суп", "Теперь 200 г риса."))
        await cookbook.process_backward_press(callback, state, session)
        outgoing.edit_media.assert_not_awaited()
        # При новом открытии остаётся прежний английский перевод.
        await cookbook.process_cookbook_command(outgoing, state, session)
        caption = outgoing.answer_photo.await_args.kwargs["caption"]
        assert "Rice soup" in caption and "100 g" in caption and "200" not in caption


async def test_page_refresh_detects_locale_author_photo_and_total_changes(
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Одна и та же страница обновляет язык, общие поля и счётчик страниц."""
    await publish_english(session)
    i18n = I18n(path=ROOT / "locales", default_locale="ru", domain="bot_00_template")
    outgoing = message()
    callback = SimpleNamespace(message=outgoing, bot=outgoing.bot, answer=AsyncMock())
    with i18n.context(), i18n.use_locale("en"):
        await cookbook.process_cookbook_command(outgoing, state, session)
    with i18n.context(), i18n.use_locale("ru"):
        await cookbook.process_backward_press(callback, state, session)
        assert "Суп" in outgoing.edit_media.await_args.kwargs["media"].caption
        outgoing.edit_media.reset_mock()
        data = values()
        data.update(author="Jane", image="another-file-id")
        await orm_update_recipe(session, 1, data)
        await cookbook.process_backward_press(callback, state, session)
        media = outgoing.edit_media.await_args.kwargs["media"]
        assert media.media == "another-file-id" and "Jane" in media.caption
        outgoing.edit_media.reset_mock()
        await orm_add_recipe(session, values("Каша"))
        await cookbook.process_backward_press(callback, state, session)
        assert (
            outgoing.edit_media.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][1]
            .text
            == "1/2"
        )


@pytest.mark.parametrize(
    "locale,empty",
    [
        ("ru", "В книге пока нет рецептов."),
        ("en", "There are no recipes in the cookbook yet."),
        ("es", "Todavía no hay recetas en el recetario."),
        ("uk", "У книзі поки немає рецептів."),
    ],
)
async def test_empty_book_is_localized_on_open_and_old_button(
    session: AsyncSession,
    state: FSMContext,
    locale: str,
    empty: str,
) -> None:
    """Пустая книга и кнопка после удаления последнего рецепта возвращают меню."""
    await publish_english(session)
    await orm_delete_recipe(session, 1)
    i18n = I18n(path=ROOT / "locales", default_locale="ru", domain="bot_00_template")
    outgoing = message()
    callback = SimpleNamespace(message=outgoing, bot=outgoing.bot, answer=AsyncMock())
    with i18n.context(), i18n.use_locale(locale):
        await cookbook.process_cookbook_command(outgoing, state, session)
        assert outgoing.answer.await_args.args[0] == empty
        outgoing.answer_photo.assert_not_awaited()
        await cookbook.process_backward_press(callback, state, session)
        callback.answer.assert_awaited_with(empty)
        outgoing.delete.assert_awaited_once()
        assert outgoing.answer.await_args.args[0] == empty


async def test_identical_legacy_page_is_not_reported_as_error(
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Старый FSM без ревизии допускает ответ Telegram об одинаковом сообщении."""
    i18n = I18n(path=ROOT / "locales", default_locale="ru", domain="bot_00_template")
    outgoing = message()
    outgoing.edit_media.side_effect = TelegramBadRequest(
        method=Mock(),
        message="Bad Request: message is not modified",
    )
    callback = SimpleNamespace(message=outgoing, bot=outgoing.bot, answer=AsyncMock())
    await state.update_data(page=1, cookbook_recipe_id=1)
    with i18n.context(), i18n.use_locale("ru"):
        await cookbook.process_backward_press(callback, state, session)
    callback.answer.assert_awaited_once_with()
    assert (await state.get_data())["cookbook_revision"]
