"""Регрессионные проверки рецептов и админки без Telegram и боевых данных."""

import asyncio
import json
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from common import keyboard
from database.models import Base
from database import orm_cookbook
from handlers import admin, cookbook


@pytest.fixture(autouse=True)
def plain_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает зависимость модульных проверок от активного gettext-контекста."""
    monkeypatch.setattr(cookbook, "_", lambda text: text)
    monkeypatch.setattr(keyboard, "_", lambda text: text)
    monkeypatch.setattr(admin, "_", lambda text: text)


def _state(user_id: int = 1) -> FSMContext:
    """Создаёт отдельное состояние пользователя в оперативной памяти."""
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _message(text: str | None = None, user_id: int = 1) -> Mock:
    """Создаёт сообщение без настоящего Telegram-клиента."""
    message = Mock(spec=Message)
    message.text = text
    message.photo = []
    message.chat = SimpleNamespace(type="private", id=user_id)
    message.from_user = SimpleNamespace(id=user_id)
    message.bot = SimpleNamespace(test_mode=False, admin_list=[1, 2])
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.edit_media = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.delete = AsyncMock()
    return message


def _callback(data: str, user_id: int = 1) -> Mock:
    """Создаёт административную кнопку с изолированным ответом."""
    callback = Mock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = SimpleNamespace(id=user_id)
    callback.message = _message(user_id=user_id)
    callback.bot = callback.message.bot
    callback.answer = AsyncMock()
    return callback


def _recipe(number: int) -> SimpleNamespace:
    """Создаёт фиктивный рецепт с независимыми полями."""
    return SimpleNamespace(
        recipe_id=number,
        recipe_name=f"Рецепт {number}",
        author=f"Автор {number}",
        description=f"Описание {number}",
        price=number,
        image=f"image_{number}",
    )


@pytest.mark.parametrize("user_id,chat_type,available,allowed", [
    (1, "private", True, True),
    (3, "private", True, False),
    (1, "group", True, False),
    (1, "private", False, False),
])
def test_admin_callback_permissions(
    user_id: int, chat_type: str, available: bool, allowed: bool
) -> None:
    """Кнопки доступны только указанному администратору в личном чате."""
    callback = _callback("recipe_delete_v2_1", user_id)
    callback.message.chat.type = chat_type
    if not available:
        callback.message = None

    result = asyncio.run(admin.admin_callback_allowed(callback, callback.bot))

    assert result is allowed
    assert callback.answer.await_count == int(not allowed)


def test_router_rejects_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реальный фильтр роутера не пропускает чужую кнопку до ORM."""
    delete = AsyncMock()
    monkeypatch.setattr(admin, "orm_delete_recipe", delete)
    callback = _callback("recipe_delete_v2_1", user_id=3)

    asyncio.run(admin.admin_router.propagate_event(
        update_type="callback_query",
        event=callback,
        bot=callback.bot,
        session=AsyncMock(),
        state=_state(3),
    ))

    delete.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Недостаточно прав", show_alert=True
    )


def test_unrelated_callback_is_not_answered_by_admin_router() -> None:
    """Обычная кнопка пользователя не получает чужое предупреждение о правах."""
    callback = _callback("service_miniapp", user_id=3)

    asyncio.run(admin.admin_router.propagate_event(
        update_type="callback_query", event=callback, bot=callback.bot,
    ))

    callback.answer.assert_not_awaited()


@pytest.mark.parametrize("data", ["delete_1", "change_1"])
@pytest.mark.parametrize("user_id", [1, 3])
def test_legacy_admin_buttons_never_access_database(
    monkeypatch: pytest.MonkeyPatch, data: str, user_id: int
) -> None:
    """Кнопки старого формата не применяются даже к существующему ID."""
    delete = AsyncMock(return_value=True)
    update = AsyncMock(return_value=True)
    get_recipe = AsyncMock(return_value=_recipe(1))
    monkeypatch.setattr(admin, "orm_delete_recipe", delete)
    monkeypatch.setattr(admin, "orm_update_recipe", update)
    monkeypatch.setattr(admin, "orm_get_recipe", get_recipe)
    callback = _callback(data, user_id=user_id)

    asyncio.run(admin.admin_router.propagate_event(
        update_type="callback_query", event=callback, bot=callback.bot,
        session=object(), state=_state(user_id),
    ))

    delete.assert_not_awaited()
    update.assert_not_awaited()
    get_recipe.assert_not_awaited()
    expected = (
        "Кнопка устарела, откройте список заново"
        if user_id == 1 else "Недостаточно прав"
    )
    callback.answer.assert_awaited_once_with(expected, show_alert=True)


def test_new_delete_button_uses_v2_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Новая авторизованная кнопка удаляет ровно указанный рецепт."""
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(admin, "orm_delete_recipe", delete)
    callback = _callback("recipe_delete_v2_7")
    session = object()

    asyncio.run(admin.admin_router.propagate_event(
        update_type="callback_query", event=callback, bot=callback.bot,
        session=session, state=_state(),
    ))

    delete.assert_awaited_once_with(session, 7)
    callback.answer.assert_awaited_once_with("Рецепт удален")


def test_recipe_list_emits_only_v2_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Список больше не создаёт старые потенциально опасные кнопки."""
    monkeypatch.setattr(
        admin, "orm_get_recipes", AsyncMock(return_value=[_recipe(1)])
    )
    message = _message()

    asyncio.run(admin.assortment_cookbook(message, object()))

    markup = message.answer_photo.await_args.kwargs["reply_markup"]
    assert {
        button.callback_data
        for row in markup.inline_keyboard for button in row
    } == {"recipe_delete_v2_1", "recipe_change_v2_1"}


def test_parallel_editors_keep_their_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Другой администратор не меняет выбранный первым рецепт."""
    get_recipe = AsyncMock(side_effect=lambda session, number: _recipe(number))
    update_recipe = AsyncMock(return_value=True)
    monkeypatch.setattr(admin, "orm_get_recipe", get_recipe)
    monkeypatch.setattr(admin, "orm_update_recipe", update_recipe)

    async def scenario() -> None:
        """Перемежает действия двух администраторов."""
        first = _state(1)
        second = _state(2)
        await admin.change_product_callback(
            _callback("recipe_change_v2_1", 1), first, object()
        )
        await admin.change_product_callback(
            _callback("recipe_change_v2_2", 2), second, object()
        )
        data = await first.get_data()
        json.dumps(data)
        await first.update_data(**data["recipe_original"])
        message = _message(user_id=1)
        message.photo = [SimpleNamespace(file_id="new_photo")]
        await admin.add_image(message, first, object())
        assert update_recipe.await_args.args[1] == 1
        assert (await second.get_data())["recipe_edit_id"] == 2
        assert (await first.get_data())["recipe_edit_id"] is None

    asyncio.run(scenario())
    assert not hasattr(admin.AddProduct, "product_for_change")


def test_new_recipe_resets_previous_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Новый рецепт не перезаписывает ранее выбранный объект."""
    add = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(admin, "orm_add_recipe", add)
    monkeypatch.setattr(admin, "orm_update_recipe", update)

    async def scenario() -> None:
        """Начинает добавление после старого режима редактирования."""
        state = _state()
        await state.update_data(
            recipe_edit_id=7, recipe_original={"recipe_name": "old"},
            locale="en",
        )
        await admin.add_product(_message(), state)
        assert (await state.get_data())["recipe_edit_id"] is None
        recipe = vars(_recipe(1)).copy()
        recipe.pop("recipe_id")
        await state.update_data(**recipe)
        message = _message()
        message.photo = [SimpleNamespace(file_id="new_photo")]
        await admin.add_image(message, state, object())
        assert (await state.get_data())["locale"] == "en"

    asyncio.run(scenario())
    add.assert_awaited_once()
    update.assert_not_awaited()


def test_main_command_leaves_recipe_editor() -> None:
    """Выход в меню сбрасывает редактор, но сохраняет выбранный язык."""
    async def scenario() -> None:
        """Выходит из активного редактирования без сохранения рецепта."""
        state = _state()
        await state.update_data(recipe_edit_id=2, locale="en", recipe_name="old")
        await state.set_state(admin.AddProduct.author)
        message = _message("/main")
        await admin.exit_recipe_editor(message, state)
        assert await state.get_state() is None
        data = await state.get_data()
        assert data["recipe_edit_id"] is None
        assert data["recipe_name"] is None
        assert data["locale"] == "en"

    asyncio.run(scenario())


@pytest.mark.parametrize("name", [
    "add_name", "add_author", "add_description", "add_price", "add_image"
])
def test_skip_is_rejected_for_new_recipe(name: str) -> None:
    """Две точки не обращаются к отсутствующему исходному рецепту."""
    async def scenario() -> None:
        """Проверяет запрет пропуска при создании."""
        state = _state()
        await state.set_state(admin.AddProduct.recipe_name)
        message = _message("..")
        function = getattr(admin, name)
        if name == "add_image":
            await function(message, state, object())
        else:
            await function(message, state)
        assert await state.get_state() == admin.AddProduct.recipe_name.state
        message.answer.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "abc", None])
def test_invalid_amount_is_rejected(value: str | None) -> None:
    """Нечисловое и бесконечное количество не попадает в состояние."""
    async def scenario() -> None:
        """Проверяет состояние после неправильного ввода."""
        state = _state()
        await state.set_state(admin.AddProduct.price)
        await admin.add_price(_message(value), state)
        assert "price" not in await state.get_data()
        assert await state.get_state() == admin.AddProduct.price.state

    asyncio.run(scenario())


def test_deleted_recipe_callback_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Старые кнопки не создают новый рецепт и не вызывают исключение."""
    get_recipe = AsyncMock(return_value=None)
    delete_recipe = AsyncMock(return_value=False)
    monkeypatch.setattr(admin, "orm_get_recipe", get_recipe)
    monkeypatch.setattr(admin, "orm_delete_recipe", delete_recipe)

    async def scenario() -> None:
        """Нажимает изменение и удаление отсутствующего ID."""
        state = _state()
        change = _callback("recipe_change_v2_100")
        await admin.change_product_callback(change, state, object())
        assert await state.get_state() is None
        change.answer.assert_awaited_once_with(
            "Рецепт уже удалён или кнопка устарела", show_alert=True
        )
        delete = _callback("recipe_delete_v2_100")
        await admin.delete_recipe_callback(delete, object())
        delete.answer.assert_awaited_once_with("Рецепт уже удалён")

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    "recipe_delete_v2_bad", "recipe_delete_v2_-1", "recipe_delete_v2_0",
    "recipe_delete_v2_99999999999999999999999999",
])
def test_malformed_delete_button_never_reaches_database(
    monkeypatch: pytest.MonkeyPatch, data: str
) -> None:
    """Неправильная кнопка отклоняется до обращения к БД."""
    delete = AsyncMock()
    monkeypatch.setattr(admin, "orm_delete_recipe", delete)
    callback = _callback(data)

    asyncio.run(admin.delete_recipe_callback(callback, object()))

    delete.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Некорректная кнопка", show_alert=True
    )


def test_user_list_is_complete_and_within_limit() -> None:
    """Длинный список делится на сообщения без потери пользователей."""
    users = [
        SimpleNamespace(
            user_id=index, status="member", flag=1, locale="en",
            user_name=f"user_{index}_" + "x" * 120,
        )
        for index in range(1, 121)
    ]
    chunks = admin.split_user_messages(users)

    assert len(chunks) > 1
    assert all(len(text.encode("utf-16-le")) // 2 <= 4000 for text in chunks)
    combined = "\n".join(chunks)
    for index in range(1, 121):
        assert f"@user_{index}_" in combined
    assert "Всего 120 пользователей" in combined


def test_oversized_unicode_user_row_is_split() -> None:
    """Даже повреждённая длинная строка не превышает лимит Telegram."""
    user = SimpleNamespace(
        user_id=1, status="member", flag=1, locale="ru",
        user_name="😀" * 5000,
    )
    chunks = admin.split_user_messages([user])

    assert all(len(text.encode("utf-16-le")) // 2 <= 4000 for text in chunks)
    assert "".join(chunks).count("😀") == 5000


class _CaptionParser(HTMLParser):
    """Собирает видимый текст и проверяет баланс разрешённых тегов."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        """Запоминает открытие безопасного тега."""
        assert tag in {"b", "i"}
        self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        """Проверяет корректное закрытие тега."""
        assert self.tags.pop() == tag

    def handle_data(self, data: str) -> None:
        """Собирает текст после разбора сущностей."""
        self.text += data


def test_caption_escapes_and_truncates_without_breaking_html() -> None:
    """HTML в названии и длинное описание не ломают подпись."""
    recipe = _recipe(1)
    recipe.recipe_name = "<script> & название"
    recipe.author = "<Автор>"
    recipe.description = "😀 & <b>описание</b> " * 500
    caption = cookbook.format_recipe_caption(recipe)
    parser = _CaptionParser()

    parser.feed(caption)

    assert parser.tags == []
    assert len(parser.text.encode("utf-16-le")) // 2 <= 1024
    assert "<script>" in parser.text
    assert "&lt;script&gt;" in caption


@pytest.mark.parametrize("value,expected", [
    (99, 2), (-4, 1), (None, 1), ("wrong", 1), (float("inf"), 1)
])
def test_page_is_normalized(value: object, expected: int) -> None:
    """Удаление рецептов не оставляет недопустимый номер страницы."""
    assert cookbook.normalize_page(value, 2) == expected


def test_book_reopens_last_existing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Непустая книга не объявляется пустой из-за старой страницы."""
    monkeypatch.setattr(
        cookbook, "orm_get_recipes",
        AsyncMock(return_value=[_recipe(2), _recipe(3)]),
    )

    async def scenario() -> None:
        """Открывает две записи с сохранённой третьей страницей."""
        state = _state()
        await state.update_data(page=3)
        message = _message()
        await cookbook.process_cookbook_command(message, state, object())
        assert (await state.get_data())["page"] == 2
        message.answer_photo.assert_awaited_once()
        assert message.answer_photo.await_args.kwargs["photo"] == "image_3"
        message.answer.assert_not_awaited()

    asyncio.run(scenario())


def test_old_book_button_refreshes_deleted_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """На границе книги устаревшая карточка заменяется существующей."""
    monkeypatch.setattr(
        cookbook, "orm_get_recipes", AsyncMock(return_value=[_recipe(2)])
    )

    async def scenario() -> None:
        """Нажимает назад после удаления старого первого рецепта."""
        state = _state()
        await state.update_data(page=1, cookbook_recipe_id=1)
        callback = _callback("backward")
        await cookbook.process_backward_press(callback, state, object())
        callback.message.edit_media.assert_awaited_once()
        media = callback.message.edit_media.await_args.kwargs["media"]
        assert media.media == "image_2"
        assert (await state.get_data())["cookbook_recipe_id"] == 2

    asyncio.run(scenario())


def test_failed_page_render_preserves_last_displayed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой Telegram не продвигает страницу, которую пользователь не увидел."""
    monkeypatch.setattr(
        cookbook, "orm_get_recipes",
        AsyncMock(return_value=[_recipe(1), _recipe(2)]),
    )

    async def scenario() -> None:
        """Имитирует ошибку при показе второй страницы."""
        state = _state()
        await state.update_data(page=1, cookbook_recipe_id=1)
        callback = _callback("forward")
        callback.message.edit_media.side_effect = TelegramAPIError(
            method=Mock(), message="synthetic error"
        )
        await cookbook.process_forward_press(callback, state, object())
        assert (await state.get_data())["page"] == 1
        assert (await state.get_data())["cookbook_recipe_id"] == 1
        callback.answer.assert_awaited_once_with(
            "Не удалось открыть страницу", show_alert=True
        )

    asyncio.run(scenario())


def test_local_images_are_only_enabled_in_test_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Тестовая метка не позволяет читать произвольные файлы."""
    images = tmp_path / "testing" / "fixtures" / "images"
    images.mkdir(parents=True)
    (images / "test_image_1.png").write_bytes(b"fixture")
    monkeypatch.setattr(cookbook, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError):
        cookbook.resolve_recipe_photo("test-fixture:1")
    with pytest.raises(ValueError):
        cookbook.resolve_recipe_photo("test-fixture:../../secret", test_mode=True)
    photo = cookbook.resolve_recipe_photo("test-fixture:1", test_mode=True)
    assert isinstance(photo, FSInputFile)
    assert str(photo.path).endswith("test_image_1.png")
    assert cookbook.resolve_recipe_photo("telegram_file_id") == "telegram_file_id"


def test_recipe_ids_are_stable_and_ordered() -> None:
    """Удаление и повторная старая кнопка не затрагивают соседние рецепты."""
    async def scenario() -> None:
        """Проверяет ORM в полностью отдельной SQLite в памяти."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                for number in range(1, 4):
                    data = vars(_recipe(number)).copy()
                    data.pop("recipe_id")
                    await orm_cookbook.orm_add_recipe(session, data)
                assert await orm_cookbook.orm_delete_recipe(session, 1)
                assert [
                    item.recipe_id
                    for item in await orm_cookbook.orm_get_recipes(session)
                ] == [2, 3]
                assert not await orm_cookbook.orm_delete_recipe(session, 1)
                assert (await orm_cookbook.orm_get_recipe(session, 2)).recipe_name
                assert await orm_cookbook.orm_delete_recipe(session, 3)
                new = vars(_recipe(4)).copy()
                new.pop("recipe_id")
                created = await orm_cookbook.orm_add_recipe(session, new)
                assert created.recipe_id > 3
                assert not await orm_cookbook.orm_update_recipe(session, 3, new)
                assert [
                    item.recipe_id
                    for item in await orm_cookbook.orm_get_recipes(session)
                ] == [2, created.recipe_id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
