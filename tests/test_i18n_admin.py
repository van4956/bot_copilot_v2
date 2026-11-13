"""Русская админка при любой пользовательской локали, без сети и постоянной БД."""

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
from sqlalchemy.exc import SQLAlchemyError

from common import keyboard
from handlers import admin, cookbook, private


@pytest.fixture
def i18n(monkeypatch: pytest.MonkeyPatch) -> I18n:
    """Создаёт публичную локаль, не подключая к ней административные строки."""
    translator = I18n(
        path=Path(__file__).resolve().parents[1] / "locales",
        default_locale="ru", domain="bot_00_template",
    )
    for module in (cookbook, keyboard, private):
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


def test_admin_stays_outside_gettext() -> None:
    """Не допускает случайного включения админки в будущие языки бота."""
    source = Path(admin.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module and "i18n" in node.module
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"_", "__", "gettext", "lazy_gettext"}
        for node in ast.walk(tree)
    )


async def test_keyboard_and_steps_stay_russian_for_each_public_locale(i18n: I18n) -> None:
    """Переключение публичного языка не меняет русскую админскую клавиатуру."""
    with i18n.context():
        for locale in ("en", "ru", "en"):
            with i18n.use_locale(locale):
                message = _message()
                await admin.cmd_admin(message, message.bot)
                assert message.answer.await_args.args[0] == (
                    "Админка:\n\n/a — панель администратора\n"
                    "/users — пользователи\n/ping — количество событий"
                )
                assert _labels(message.answer.await_args.kwargs["reply_markup"]) == [
                    text for text in (
                        "Ассортимент 📖", "Добавить 📃", "Синхронизировать 🔄",
                        "Информация 👨‍👩‍👦‍👦", "Назад на главную ↩️",
                    )
                ]
                assert admin.get_step_prompt(admin.AddProduct.author.state) == (
                    "Введите имя автора рецепта"
                )
                assert [
                    len(row)
                    for row in message.answer.await_args.kwargs["reply_markup"].keyboard
                ] == [3, 1, 1]
                assert i18n.current_locale == locale


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
async def test_add_recipe_keeps_russian_steps_and_original_content(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Админские подсказки всегда русские, поля рецепта остаются исходными."""
    save = AsyncMock()
    monkeypatch.setattr(admin, "orm_add_recipe", save)
    state = _state()
    with i18n.context(), i18n.use_locale(locale):
        message = _message()
        await admin.add_product(message, state)
        assert message.answer.await_args.args[0] == "Введите название рецепта"
        for handler, text, prompt in (
            (admin.add_name, "Русский суп", "Введите имя автора рецепта"),
            (admin.add_author, "<Автор>", "Введите описание рецепта"),
            (admin.add_description, "Оригинальное описание", "Введите количество приготовлений"),
            (admin.add_price, "3,5", "Загрузите изображение блюда"),
        ):
            message.text = text
            await handler(message, state)
            assert message.answer.await_args.args[0] == prompt
        message.text = None
        message.photo = [SimpleNamespace(file_id="new-file-id")]
        await admin.add_image(message, state, object())
        assert message.answer.await_args.args[0] == "Рецепт добавлен"
    data = save.await_args.args[1]
    assert data["recipe_name"] == "Русский суп" and data["author"] == "<Автор>"
    assert data["description"] == "Оригинальное описание"
    assert data["price"] == 3.5 and data["image"] == "new-file-id"
    assert await state.get_state() is None


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
async def test_edit_and_delete_callbacks_keep_identifiers(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Изменение и удаление сохраняют русский интерфейс, ID и исходные поля."""
    monkeypatch.setattr(admin, "orm_get_recipe", AsyncMock(return_value=_recipe()))
    update = AsyncMock(return_value=True)
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(admin, "orm_update_recipe", update)
    monkeypatch.setattr(admin, "orm_delete_recipe", delete)
    state, session = _state(), object()
    with i18n.context(), i18n.use_locale(locale):
        change = _callback("recipe_change_v2_7")
        await admin.change_product_callback(change, state, session)
        assert change.message.answer.await_args.args[0] == "Введите название рецепта. '..' — оставить старое значение."
        message = _message("..")
        for handler in (admin.add_name, admin.add_author, admin.add_description, admin.add_price):
            await handler(message, state)
        await admin.add_image(message, state, session)
        assert message.answer.await_args.args[0] == "Рецепт изменен"
        callback = _callback("recipe_delete_v2_7")
        await admin.delete_recipe_callback(callback, session)
        callback.answer.assert_awaited_once_with("Рецепт удален")
    assert update.await_args.args[1] == 7
    assert update.await_args.args[2]["image"] == "original-file-id"
    assert update.await_args.args[2]["recipe_name"] == "Русский суп"
    delete.assert_awaited_once_with(session, 7)


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
async def test_permission_denial_and_old_buttons_stay_russian(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Проверка доступа остаётся прежней и не обращается к БД."""
    forbidden = AsyncMock(side_effect=AssertionError("No database access"))
    monkeypatch.setattr(admin, "orm_delete_recipe", forbidden)
    with i18n.context(), i18n.use_locale(locale):
        callback = _callback("recipe_delete_v2_7", user_id=99)
        assert not await admin.admin_callback_allowed(callback, callback.bot)
        callback.answer.assert_awaited_once_with(
            "Недостаточно прав", show_alert=True
        )
        legacy = _callback("delete_7")
        await admin.legacy_recipe_callback(legacy)
        legacy.answer.assert_awaited_once_with(
            "Кнопка устарела, откройте список заново", show_alert=True
        )
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
async def test_validation_and_save_error_stay_russian(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Валидация и откат возвращают русский ответ и русское админское меню."""
    session = SimpleNamespace(rollback=AsyncMock())
    monkeypatch.setattr(admin, "orm_add_recipe", AsyncMock(side_effect=SQLAlchemyError()))
    state = _state()
    with i18n.context(), i18n.use_locale(locale):
        message = _message(" " * 2)
        await admin.add_name(message, state)
        assert message.answer.await_args.args[0] == "Введите непустой текст до {limit} символов.".format(limit=100)
        message.text = "nan"
        await admin.add_price(message, state)
        assert message.answer.await_args.args[0] == "Введите корректное неотрицательное количество."
        message.text = None
        message.photo = [SimpleNamespace(file_id="photo")]
        await admin.add_image(message, state, session)
        assert message.answer.await_args.args[0] == "Не удалось сохранить рецепт. Попробуйте ещё раз."
        assert _labels(message.answer.await_args.kwargs["reply_markup"])[1] == (
            "Добавить 📃"
        )
    session.rollback.assert_awaited_once()
    assert await state.get_state() is None


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
async def test_recipe_list_fallback_and_user_information_stay_russian(
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
        assert "Автор:" not in message.answer_photo.await_args.kwargs["caption"]
        assert "<i>&lt;Автор&gt;</i>\n\n" in message.answer_photo.await_args.kwargs["caption"]
        assert "&lt;Автор&gt;" in message.answer_photo.await_args.kwargs["caption"]
        buttons = message.answer_photo.await_args.kwargs["reply_markup"].inline_keyboard
        assert [button.text for row in buttons for button in row] == [
            "Удалить", "Изменить"
        ]
        assert [button.callback_data for row in buttons for button in row] == [
            "recipe_delete_v2_7", "recipe_change_v2_7"
        ]
        message.answer_photo.side_effect = TelegramAPIError(method=Mock(), message="offline")
        await admin.assortment_cookbook(message, object())
        assert message.answer.await_args_list[-2].args[0] == "Не удалось показать изображение рецепта {recipe_id}".format(recipe_id=7)
        await admin.get_users_info(message, object())
        users = message.answer.await_args.args[0]
        assert users == (
            "Информация зарегистрированных пользователей:\n\n"
            "099 | m - ru | @РусскоеИмя\n"
            "100 | m - en | @без username\n\n"
            "Всего 2 пользователей"
        )
        assert message.answer.await_args.kwargs["parse_mode"] is None


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_admin_router_accepts_russian_buttons_and_editor_controls(
    i18n: I18n, monkeypatch: pytest.MonkeyPatch, locale: str, legacy: bool,
) -> None:
    """Настоящие фильтры принимают русские пункты меню, назад и отмену."""
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
    await state.update_data(locale=locale)
    try:
        with i18n.context(), i18n.use_locale(locale):
            users_button = "Информация о пользователях" if legacy else admin.USERS_BUTTON
            assortment = "Ассортимент Книги" if legacy else admin.ASSORTMENT_BUTTON
            add_button = "Добавить рецепт" if legacy else admin.ADD_RECIPE_BUTTON
            texts = [
                "/a", users_button, assortment, add_button,
                "Русский суп", "назад", "отмена",
                add_button, "Назад на главную ↩️",
                "/a", "Назад на главную ↩️",
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
            assert "Введите имя автора рецепта" in answers
            assert "Действия отменены" in answers
            assert answers[-1] == i18n.gettext("Главная панель")
        assert await state.get_state() is None
        assert (await state.get_data())["locale"] == locale
        admin.orm_get_users.assert_awaited_once()
        admin.orm_get_recipes.assert_awaited_once()
    finally:
        admin.admin_router._parent_router = None
        dispatcher.sub_routers.clear()
        await dispatcher.storage.close()
        await bot.session.close()
