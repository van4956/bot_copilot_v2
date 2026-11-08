"""Админский интерфейс RU/EN без сети и постоянной базы данных."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, Update
from aiogram.utils.i18n import I18n
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from sqlalchemy.exc import SQLAlchemyError

from common import keyboard
from handlers import admin, cookbook

EN_UI = {
    "Админка:\n\n/a — панель администратора\n/users — пользователи\n/ping — количество событий":
        "Admin panel:\n\n/a — administrator panel\n/users — users\n/ping — event count",
    "Ассортимент Книги": "Cookbook contents",
    "Введите имя автора рецепта": "Enter the recipe author's name",
    "Введите имя автора текстом.": "Enter the author's name as text.",
    "Введите количество приготовлений": "Enter the number of times cooked",
    "Введите количество приготовлений числом.": "Enter the number of times cooked as a number.",
    "Введите корректное неотрицательное количество.": "Enter a valid non-negative number.",
    "Введите название рецепта": "Enter the recipe name",
    "Введите название рецепта текстом.": "Enter the recipe name as text.",
    "Введите название рецепта. '..' — оставить старое значение.":
        "Enter the recipe name. Use '..' to keep the previous value.",
    "Введите непустой текст до {limit} символов.": "Enter non-empty text up to {limit} characters.",
    "Введите описание рецепта": "Enter the recipe description",
    "Введите описание рецепта текстом.": "Enter the recipe description as text.",
    "Всего {count} пользователей": "Total: {count} users",
    "Главная панель": "Main panel",
    "Действия отменены": "Actions cancelled",
    "Добавить рецепт": "Add recipe",
    "Загрузите изображение блюда": "Upload a photo of the dish",
    "Изменить": "Edit",
    "Информация зарегистрированных пользователей:": "Registered user information:",
    "Информация о пользователях": "User information",
    "Книга рецептов пуста": "The cookbook is empty",
    "Кнопка устарела, откройте список заново": "This button is outdated. Open the list again",
    "Назад на главную ↩️": "Back to main ↩️",
    "Не удалось показать изображение рецепта {recipe_id}":
        "Could not display the image for recipe {recipe_id}",
    "Не удалось сохранить рецепт. Попробуйте ещё раз.":
        "Could not save the recipe. Please try again.",
    "Недостаточно прав": "Insufficient permissions",
    "Некорректная кнопка": "Invalid button",
    "ОК, вот весь список рецептов ⏫": "OK, here is the complete recipe list ⏫",
    "Отправьте фото блюда.": "Send a photo of the dish.",
    "Предыдущего шага нет. Введите название или 'отмена'.":
        "There is no previous step. Enter a name or 'cancel'.",
    "При добавлении нельзя пропустить это поле.": "You cannot skip this field when adding a recipe.",
    "При добавлении нужно отправить изображение.": "Send an image when adding a recipe.",
    "Рецепт добавлен": "Recipe added",
    "Рецепт изменен": "Recipe updated",
    "Рецепт удален": "Recipe deleted",
    "Рецепт уже удалён": "The recipe has already been deleted",
    "Рецепт уже удалён или кнопка устарела": "The recipe was deleted or the button is outdated",
    "Старого изображения нет. Отправьте новое фото.": "There is no previous image. Send a new photo.",
    "Удалить": "Delete",
    "без username": "no username",
    "назад": "back",
    "отмена": "cancel",
}


@pytest.fixture
def i18n(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> I18n:
    """Создаёт временные каталоги перевода, не меняя рабочие локали."""
    for locale in ("ru", "en"):
        catalog = Catalog(locale=locale)
        for source, target in EN_UI.items():
            catalog.add(source, target if locale == "en" else source)
        path = tmp_path / locale / "LC_MESSAGES" / "admin-test.mo"
        path.parent.mkdir(parents=True)
        with path.open("wb") as stream:
            write_mo(stream, catalog)
    translator = I18n(path=tmp_path, default_locale="ru", domain="admin-test")
    for module in (admin, cookbook, keyboard):
        monkeypatch.setattr(module, "_", translator.gettext)
    return translator


def _state() -> FSMContext:
    """Создаёт независимый редактор в оперативной памяти."""
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


def _message(text: str | None = None) -> Mock:
    """Подменяет сообщение и все способы ответа Telegram."""
    message = Mock(spec=Message)
    message.text = text
    message.photo = []
    message.chat = SimpleNamespace(id=1, type="private")
    message.from_user = SimpleNamespace(id=1)
    message.bot = SimpleNamespace(test_mode=False, admin_list=[1])
    for method in ("answer", "answer_photo", "edit_reply_markup", "reply"):
        setattr(message, method, AsyncMock())
    return message


def _callback(data: str, user_id: int = 1) -> Mock:
    """Создаёт административное нажатие без настоящего API."""
    callback = Mock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = _message()
    callback.bot = callback.message.bot
    callback.answer = AsyncMock()
    return callback


def _recipe() -> SimpleNamespace:
    """Возвращает пользовательский контент, который не следует переводить."""
    return SimpleNamespace(
        recipe_id=7, recipe_name="Русский суп", author="<Автор>",
        description="Оригинальное описание", price=3, image="original-file-id",
    )


def _labels(markup: object) -> list[str]:
    """Собирает подписи обычной админской клавиатуры."""
    return [button.text for row in markup.keyboard for button in row]


def test_all_admin_msgids_have_explicit_test_translations() -> None:
    """Проверяет полноту словаря тестов и отсутствие динамических msgid."""
    source = Path(admin.__file__).read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {"_", "__"}]
    assert all(isinstance(node.args[0], ast.Constant) for node in calls)
    assert {node.args[0].value for node in calls} == set(EN_UI)


async def test_keyboard_and_steps_follow_each_request_locale(i18n: I18n) -> None:
    """Чередование языков не оставляет закэшированную клавиатуру или подсказку."""
    with i18n.context():
        for locale in ("en", "ru", "en"):
            with i18n.use_locale(locale):
                message = _message()
                await admin.cmd_admin(message, message.bot)
                assert message.answer.await_args.args[0] == i18n.gettext(
                    "Админка:\n\n/a — панель администратора\n"
                    "/users — пользователи\n/ping — количество событий"
                )
                assert _labels(message.answer.await_args.kwargs["reply_markup"]) == [
                    i18n.gettext(text) for text in (
                        "Ассортимент Книги", "Добавить рецепт",
                        "Информация о пользователях", "Назад на главную ↩️",
                    )
                ]
                assert admin.get_step_prompt(admin.AddProduct.author.state) == (
                    i18n.gettext("Введите имя автора рецепта")
                )


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_add_recipe_translates_steps_but_keeps_content(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Последовательные подсказки переводятся, поля рецепта остаются исходными."""
    save = AsyncMock()
    monkeypatch.setattr(admin, "orm_add_recipe", save)
    state = _state()
    with i18n.context(), i18n.use_locale(locale):
        message = _message()
        await admin.add_product(message, state)
        assert message.answer.await_args.args[0] == i18n.gettext("Введите название рецепта")
        for handler, text, prompt in (
            (admin.add_name, "Русский суп", "Введите имя автора рецепта"),
            (admin.add_author, "<Автор>", "Введите описание рецепта"),
            (admin.add_description, "Оригинальное описание", "Введите количество приготовлений"),
            (admin.add_price, "3,5", "Загрузите изображение блюда"),
        ):
            message.text = text
            await handler(message, state)
            assert message.answer.await_args.args[0] == i18n.gettext(prompt)
        message.text = None
        message.photo = [SimpleNamespace(file_id="new-file-id")]
        await admin.add_image(message, state, object())
        assert message.answer.await_args.args[0] == i18n.gettext("Рецепт добавлен")
    data = save.await_args.args[1]
    assert data["recipe_name"] == "Русский суп" and data["author"] == "<Автор>"
    assert data["description"] == "Оригинальное описание"
    assert data["price"] == 3.5 and data["image"] == "new-file-id"
    assert await state.get_state() is None


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_edit_and_delete_callbacks_keep_identifiers(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Изменение и удаление переводят ответы, не меняя ID или исходные поля."""
    monkeypatch.setattr(admin, "orm_get_recipe", AsyncMock(return_value=_recipe()))
    update = AsyncMock(return_value=True)
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(admin, "orm_update_recipe", update)
    monkeypatch.setattr(admin, "orm_delete_recipe", delete)
    state, session = _state(), object()
    with i18n.context(), i18n.use_locale(locale):
        change = _callback("recipe_change_v2_7")
        await admin.change_product_callback(change, state, session)
        assert change.message.answer.await_args.args[0] == i18n.gettext(
            "Введите название рецепта. '..' — оставить старое значение."
        )
        message = _message("..")
        for handler in (admin.add_name, admin.add_author, admin.add_description, admin.add_price):
            await handler(message, state)
        await admin.add_image(message, state, session)
        assert message.answer.await_args.args[0] == i18n.gettext("Рецепт изменен")
        callback = _callback("recipe_delete_v2_7")
        await admin.delete_recipe_callback(callback, session)
        callback.answer.assert_awaited_once_with(i18n.gettext("Рецепт удален"))
    assert update.await_args.args[1] == 7
    assert update.await_args.args[2]["image"] == "original-file-id"
    assert update.await_args.args[2]["recipe_name"] == "Русский суп"
    delete.assert_awaited_once_with(session, 7)


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_permission_denial_and_old_buttons_are_localized(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Проверка доступа остаётся прежней и не обращается к БД."""
    forbidden = AsyncMock(side_effect=AssertionError("No database access"))
    monkeypatch.setattr(admin, "orm_delete_recipe", forbidden)
    with i18n.context(), i18n.use_locale(locale):
        callback = _callback("recipe_delete_v2_7", user_id=99)
        assert not await admin.admin_callback_allowed(callback, callback.bot)
        callback.answer.assert_awaited_once_with(
            i18n.gettext("Недостаточно прав"), show_alert=True
        )
        legacy = _callback("delete_7")
        await admin.legacy_recipe_callback(legacy)
        legacy.answer.assert_awaited_once_with(
            i18n.gettext("Кнопка устарела, откройте список заново"), show_alert=True
        )
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_validation_and_save_error_are_localized(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Валидация и откат ошибки возвращают переведённый ответ и админское меню."""
    session = SimpleNamespace(rollback=AsyncMock())
    monkeypatch.setattr(admin, "orm_add_recipe", AsyncMock(side_effect=SQLAlchemyError()))
    state = _state()
    with i18n.context(), i18n.use_locale(locale):
        message = _message(" " * 2)
        await admin.add_name(message, state)
        assert message.answer.await_args.args[0] == i18n.gettext(
            "Введите непустой текст до {limit} символов."
        ).format(limit=100)
        message.text = "nan"
        await admin.add_price(message, state)
        assert message.answer.await_args.args[0] == i18n.gettext(
            "Введите корректное неотрицательное количество."
        )
        message.text = None
        message.photo = [SimpleNamespace(file_id="photo")]
        await admin.add_image(message, state, session)
        assert message.answer.await_args.args[0] == i18n.gettext(
            "Не удалось сохранить рецепт. Попробуйте ещё раз."
        )
        assert _labels(message.answer.await_args.kwargs["reply_markup"])[1] == (
            i18n.gettext("Добавить рецепт")
        )
    session.rollback.assert_awaited_once()
    assert await state.get_state() is None


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_recipe_list_fallback_and_user_information_are_localized(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Ошибка фото не ломает список, а пользовательские данные не переводятся."""
    monkeypatch.setattr(admin, "orm_get_recipes", AsyncMock(return_value=[_recipe()]))
    monkeypatch.setattr(admin, "orm_get_users", AsyncMock(return_value=[
        SimpleNamespace(user_id=99, status="member", flag=1, locale="ru", user_name="РусскоеИмя"),
        SimpleNamespace(user_id=100, status="member", flag=1, locale="en", user_name=None),
    ]))
    with i18n.context(), i18n.use_locale(locale):
        message = _message()
        await admin.assortment_cookbook(message, object())
        buttons = message.answer_photo.await_args.kwargs["reply_markup"].inline_keyboard
        assert [button.text for row in buttons for button in row] == [
            i18n.gettext("Удалить"), i18n.gettext("Изменить")
        ]
        assert [button.callback_data for row in buttons for button in row] == [
            "recipe_delete_v2_7", "recipe_change_v2_7"
        ]
        message.answer_photo.side_effect = TelegramAPIError(method=Mock(), message="offline")
        await admin.assortment_cookbook(message, object())
        assert message.answer.await_args_list[-2].args[0] == i18n.gettext(
            "Не удалось показать изображение рецепта {recipe_id}"
        ).format(recipe_id=7)
        await admin.get_users_info(message, object())
        users = message.answer.await_args.args[0]
        assert users.startswith(i18n.gettext("Информация зарегистрированных пользователей:"))
        assert "@РусскоеИмя" in users
        assert "@" + i18n.gettext("без username") in users
        assert i18n.gettext("Всего {count} пользователей").format(count=2) in users
        assert message.answer.await_args.kwargs["parse_mode"] is None


@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_real_admin_router_accepts_localized_buttons_and_editor_controls(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Настоящие фильтры принимают локализованные пункты меню, назад и отмену."""
    bot = Bot("123456:OFFLINE_TEST_ONLY")
    bot.admin_list = [1]
    dispatcher = Dispatcher()
    dispatcher.include_router(admin.admin_router)
    monkeypatch.setattr(admin, "orm_get_users", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin, "orm_get_recipes", AsyncMock(return_value=[]))
    outgoing = []

    async def send(active_bot: Bot, method: object, **kwargs: object) -> Message:
        """Подменяет исходящие обращения Telegram полностью в памяти."""
        outgoing.append(method)
        return Message.model_validate({
            "message_id": len(outgoing), "date": datetime.now(timezone.utc),
            "chat": {"id": 1, "type": "private"}, "text": getattr(method, "text", ""),
        })

    monkeypatch.setattr(Bot, "__call__", send)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=1, user_id=1)
    try:
        with i18n.context(), i18n.use_locale(locale):
            texts = [
                "/a", i18n.gettext("Информация о пользователях"),
                i18n.gettext("Ассортимент Книги"), i18n.gettext("Добавить рецепт"),
                "Русский суп", i18n.gettext("назад"), i18n.gettext("отмена"),
                i18n.gettext("Добавить рецепт"), i18n.gettext("Назад на главную ↩️"),
            ]
            for number, text in enumerate(texts, 1):
                update = Update.model_validate({
                    "update_id": number,
                    "message": {"message_id": number, "date": datetime.now(timezone.utc),
                                "chat": {"id": 1, "type": "private"},
                                "from": {"id": 1, "is_bot": False, "first_name": "Тест"},
                                "text": text},
                })
                await dispatcher.feed_update(bot, update, session=object())
            answers = [item.text for item in outgoing]
            assert i18n.gettext("Введите имя автора рецепта") in answers
            assert i18n.gettext("Действия отменены") in answers
            assert answers[-1] == i18n.gettext("Главная панель")
        assert await state.get_state() is None
        admin.orm_get_users.assert_awaited_once()
        admin.orm_get_recipes.assert_awaited_once()
    finally:
        admin.admin_router._parent_router = None
        dispatcher.sub_routers.clear()
        await dispatcher.storage.close()
        await bot.session.close()
