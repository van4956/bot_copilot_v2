"""Администрирование пользователей и рецептов с индивидуальным FSM."""

import logging
import math
from typing import Any, Iterable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.cookbook_sync import CookbookSyncError, SyncResult, sync_cookbook
from common.keyboard import get_callback_btns, get_keyboard
from database.orm_cookbook import (
    orm_add_recipe,
    orm_delete_recipe,
    orm_get_recipe,
    orm_get_recipes,
    orm_update_recipe,
)
from database.orm_users import orm_get_users
from filters.chat_type import ChatTypeFilter
from filters.is_admin import IsAdminListFilter
from handlers import private
from handlers.cookbook import format_recipe_caption, resolve_recipe_photo

logger = logging.getLogger(__name__)
admin_router = Router()
admin_router.message.filter(
    ChatTypeFilter(["private"]), IsAdminListFilter(is_admin=True)
)
DELETE_PREFIX = "recipe_delete_v2_"
CHANGE_PREFIX = "recipe_change_v2_"
LEGACY_PREFIXES = ("delete_", "change_")
ASSORTMENT_BUTTON = "Ассортимент 📖"
ADD_RECIPE_BUTTON = "Добавить 📃"
SYNC_COOKBOOK_BUTTON = "Синхронизировать 🔄"
USERS_BUTTON = "Информация 👨‍👩‍👦‍👦"
ADMIN_BACK_BUTTON = "Назад на главную ↩️"


async def admin_callback_allowed(callback: CallbackQuery, bot: Bot) -> bool:
    """Разрешает административные кнопки только администратору в личном чате."""
    if not (callback.data or "").startswith(
        (DELETE_PREFIX, CHANGE_PREFIX, *LEGACY_PREFIXES)
    ):
        return False
    allowed = (
        callback.from_user.id in getattr(bot, "admin_list", ())
        and isinstance(callback.message, Message)
        and callback.message.chat.type == "private"
    )
    if not allowed:
        await callback.answer("Недостаточно прав", show_alert=True)
    return allowed


admin_router.callback_query.filter(admin_callback_allowed)


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    """Создаёт русскую админскую клавиатуру независимо от языка пользователя."""
    return get_keyboard(
        ASSORTMENT_BUTTON, ADD_RECIPE_BUTTON, SYNC_COOKBOOK_BUTTON,
        USERS_BUTTON, ADMIN_BACK_BUTTON,
        placeholder="⬇️", sizes=(3, 1, 1),
    )


class AddProduct(StatesGroup):
    """Состояния индивидуального редактора рецепта."""

    recipe_name = State()
    author = State()
    description = State()
    price = State()
    image = State()


RECIPE_FIELDS = ("recipe_name", "author", "description", "price", "image")


def get_step_prompt(step: str) -> str:
    """Возвращает русскую подсказку текущего шага редактора."""
    prompts = {
        AddProduct.recipe_name.state: "Введите название рецепта",
        AddProduct.author.state: "Введите имя автора рецепта",
        AddProduct.description.state: "Введите описание рецепта",
        AddProduct.price.state: "Введите количество приготовлений",
        AddProduct.image.state: "Загрузите изображение блюда",
    }
    return prompts[step]


async def _reset_editor(state: FSMContext) -> None:
    """Завершает редактор, сохраняя язык и данные других сценариев."""
    await state.set_state(None)
    await state.update_data(
        recipe_edit_id=None, recipe_original=None,
        **dict.fromkeys(RECIPE_FIELDS),
    )


def _recipe_id(data: str | None, prefix: str) -> int | None:
    """Разбирает только положительный идентификатор административной кнопки."""
    if not data or not data.startswith(prefix):
        return None
    value = (data or "").removeprefix(prefix)
    if not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if 0 < number <= 2_147_483_647 else None


def _snapshot(recipe: Any) -> dict[str, Any]:
    """Создаёт сериализуемую копию исходных значений для одного редактора."""
    return {
        "recipe_name": recipe.recipe_name,
        "author": recipe.author,
        "description": recipe.description,
        "price": float(recipe.price),
        "image": recipe.image,
    }


@admin_router.message(Command("a"))
async def cmd_admin(
    message: Message, bot: Bot, workflow_data: dict | None = None
) -> None:
    """Показывает администратору доступные команды."""
    await message.answer(
        "Админка:\n\n/a — панель администратора\n"
        "/users — пользователи\n/ping — количество событий",
        reply_markup=get_admin_keyboard(),
    )


@admin_router.message(Command("ping"))
async def cmd_ping_bot(
    message: Message, counter: int, workflow_data: dict | None = None
) -> None:
    """Показывает счётчик событий."""
    await message.reply(f"ping-{counter}")


def _split_plain_line(line: str, limit: int) -> list[str]:
    """Разбивает слишком длинную строку по допустимому числу символов."""
    result = []
    current = []
    size = 0
    for character in line:
        width = len(character.encode("utf-16-le")) // 2
        if size + width > limit:
            result.append("".join(current))
            current = []
            size = 0
        current.append(character)
        size += width
    if current:
        result.append("".join(current))
    return result or [""]


def split_user_messages(
    users: Iterable[Any], limit: int = 4000, *, admin_ids: Iterable[int] = (),
) -> list[str]:
    """Разбивает полный список пользователей без потери строк и HTML-ошибок."""
    if limit < 2:
        raise ValueError("Слишком короткий лимит сообщения")
    users = list(users)
    id_width = max((len(str(user.user_id)) for user in users), default=1)
    active_admins = set(admin_ids)
    lines = ["Информация зарегистрированных пользователей:", ""]
    for user in users:
        status = (
            "a" if user.user_id in active_admins
            else "m" if user.status == "member" else "k"
        )
        identifier = str(user.user_id).zfill(id_width)
        username = user.user_name or "без username"
        lines.append(
            f"{identifier} | {status} - {user.locale} | @{username}"
        )
    if users:
        lines.append("")
    lines.append("Всего {count} пользователей".format(count=len(users)))
    messages = []
    current = []
    size = 0
    for line in lines:
        for part in _split_plain_line(line, limit):
            width = len(part.encode("utf-16-le")) // 2
            separator = int(bool(current))
            if size + separator + width > limit:
                if any(current):
                    messages.append("\n".join(current))
                current = []
                size = 0
                separator = 0
            current.append(part)
            size += separator + width
    if any(current):
        messages.append("\n".join(current))
    return messages


@admin_router.message(
    or_f(Command("users"), F.text == USERS_BUTTON,
         F.text == "Информация о пользователях")
)
async def get_users_info(
    message: Message, session: AsyncSession,
    workflow_data: dict | None = None,
) -> None:
    """Отправляет полный список пользователей частями."""
    active_bot = getattr(message, "bot", None)
    admin_ids = getattr(active_bot, "admin_list", ()) or ()
    for text in split_user_messages(
        await orm_get_users(session), admin_ids=admin_ids,
    ):
        await message.answer(text, parse_mode=None)


@admin_router.message(F.text.in_({ASSORTMENT_BUTTON, "Ассортимент Книги"}))
async def assortment_cookbook(
    message: Message, session: AsyncSession,
    workflow_data: dict | None = None,
) -> None:
    """Показывает рецепты и административные кнопки."""
    recipes = await orm_get_recipes(session)
    for recipe in recipes:
        try:
            await message.answer_photo(
                photo=resolve_recipe_photo(
                    recipe.image,
                    test_mode=getattr(message.bot, "test_mode", False),
                ),
                caption=format_recipe_caption(recipe, author_label="Автор:"),
                reply_markup=get_callback_btns(btns={
                    "Удалить": f"{DELETE_PREFIX}{recipe.recipe_id}",
                    "Изменить": f"{CHANGE_PREFIX}{recipe.recipe_id}",
                }),
            )
        except (TelegramAPIError, ValueError):
            logger.exception("Не удалось показать рецепт %s", recipe.recipe_id)
            await message.answer(
                "Не удалось показать изображение рецепта {recipe_id}".format(
                    recipe_id=recipe.recipe_id
                )
            )
    await message.answer(
        "ОК, вот весь список рецептов ⏫" if recipes else "В книге пока нет рецептов.",
        reply_markup=get_admin_keyboard(),
    )


def format_sync_result(result: SyncResult) -> str:
    """Собирает понятный итог синхронизации без технических подробностей."""
    if result.total == 0:
        return "В книге пока нет рецептов."
    if not result.updated and not result.changed and not result.deleted:
        return "Все рецепты уже синхронизированы."
    lines = [f"Синхронизация завершена. Обновлено рецептов: {result.updated}."]
    if result.skipped:
        lines.append(f"Без изменений: {result.skipped}.")
    if result.changed:
        lines.append(
            f"Изменены во время синхронизации: {result.changed}. "
            "Нажмите «Синхронизировать 🔄» ещё раз."
        )
    if result.deleted:
        lines.append(f"Удалены во время синхронизации: {result.deleted}.")
    return "\n".join(lines)


@admin_router.message(StateFilter(None), F.text == SYNC_COOKBOOK_BUTTON)
async def synchronize_cookbook(
    message: Message, bot: Bot, session: AsyncSession,
) -> None:
    """Синхронизирует переводы книги и обновляет единственное сообщение прогресса."""
    progress = await message.answer("Синхронизирую рецепты…")
    try:
        result = await sync_cookbook(
            session,
            api_key=getattr(bot, "api_gpt", ""),
            http_session=getattr(bot, "http_session", None),
        )
    except CookbookSyncError as error:
        text = str(error)
    else:
        text = format_sync_result(result)
    await progress.edit_text(text)


@admin_router.callback_query(
    or_f(F.data.startswith("delete_"), F.data.startswith("change_"))
)
async def legacy_recipe_callback(callback: CallbackQuery) -> None:
    """Не выполняет старые кнопки, пережившие прежнюю перенумерацию ID."""
    await callback.answer(
        "Кнопка устарела, откройте список заново", show_alert=True
    )


@admin_router.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_recipe_callback(
    callback: CallbackQuery, session: AsyncSession,
    workflow_data: dict | None = None,
) -> None:
    """Удаляет рецепт по устойчивому ID, включая обработку старых кнопок."""
    recipe_id = _recipe_id(callback.data, DELETE_PREFIX)
    if recipe_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    deleted = await orm_delete_recipe(session, recipe_id)
    text = "Рецепт удален" if deleted else "Рецепт уже удалён"
    await callback.answer(text)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)


@admin_router.callback_query(StateFilter(None), F.data.startswith(CHANGE_PREFIX))
async def change_product_callback(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
    workflow_data: dict | None = None,
) -> None:
    """Запоминает редактируемый рецепт в состоянии конкретного пользователя."""
    recipe_id = _recipe_id(callback.data, CHANGE_PREFIX)
    recipe = (
        await orm_get_recipe(session, recipe_id)
        if recipe_id is not None else None
    )
    if recipe is None:
        await callback.answer("Рецепт уже удалён или кнопка устарела", show_alert=True)
        return
    await state.update_data(
        recipe_edit_id=recipe_id,
        recipe_original=_snapshot(recipe),
        **dict.fromkeys(RECIPE_FIELDS),
    )
    await state.set_state(AddProduct.recipe_name)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Введите название рецепта. '..' — оставить старое значение.",
            reply_markup=ReplyKeyboardRemove(),
        )


@admin_router.message(
    StateFilter(None), F.text.in_({ADD_RECIPE_BUTTON, "Добавить рецепт"})
)
async def add_product(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Начинает добавление, не наследуя прежний режим редактирования."""
    await state.update_data(
        recipe_edit_id=None, recipe_original=None,
        **dict.fromkeys(RECIPE_FIELDS),
    )
    await state.set_state(AddProduct.recipe_name)
    await message.answer(
        "Введите название рецепта", reply_markup=ReplyKeyboardRemove()
    )


@admin_router.message(StateFilter(AddProduct), Command("отмена"))
@admin_router.message(
    StateFilter(AddProduct), or_f(
        F.text.casefold() == "отмена", F.text == "."
    )
)
async def cancel_handler(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Отменяет только редактор рецепта."""
    await _reset_editor(state)
    await message.answer("Действия отменены", reply_markup=get_admin_keyboard())


@admin_router.message(StateFilter(AddProduct), Command("назад"))
@admin_router.message(
    StateFilter(AddProduct),
    F.text.casefold() == "назад",
)
async def back_handler(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Возвращает на предыдущий шаг редактора."""
    current = await state.get_state()
    steps = list(AddProduct.__all_states__)
    indexes = {step.state: index for index, step in enumerate(steps)}
    index = indexes.get(current, 0)
    if index == 0:
        await message.answer("Предыдущего шага нет. Введите название или 'отмена'.")
        return
    previous = steps[index - 1]
    await state.set_state(previous)
    await message.answer(get_step_prompt(previous.state))


@admin_router.message(F.text == ADMIN_BACK_BUTTON)
@admin_router.message(StateFilter(AddProduct), Command("main"))
async def exit_recipe_editor(message: Message, state: FSMContext) -> None:
    """Открывает пользовательскую главную, сохраняя выбранный язык."""
    await _reset_editor(state)
    await private.cancel_cmd(message, state)


async def _read_text_field(
    message: Message, state: FSMContext, field: str, limit: int,
    next_state: State,
) -> None:
    """Проверяет текст или индивидуальное старое значение поля."""
    data = await state.get_data()
    text = message.text
    if text == "..":
        original = data.get("recipe_original")
        if not data.get("recipe_edit_id") or not isinstance(original, dict):
            await message.answer("При добавлении нельзя пропустить это поле.")
            return
        value = original.get(field)
    else:
        value = (text or "").strip()
    if not isinstance(value, str) or not value or len(value) > limit:
        await message.answer("Введите непустой текст до {limit} символов.".format(
            limit=limit
        ))
        return
    await state.update_data(**{field: value})
    await state.set_state(next_state)
    await message.answer(get_step_prompt(next_state.state))


@admin_router.message(AddProduct.recipe_name, F.text)
async def add_name(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Проверяет название рецепта."""
    await _read_text_field(
        message, state, "recipe_name", 100, AddProduct.author
    )


@admin_router.message(AddProduct.recipe_name)
async def add_name2(
    message: Message, workflow_data: dict | None = None
) -> None:
    """Объясняет ожидаемый тип названия."""
    await message.answer("Введите название рецепта текстом.")


@admin_router.message(AddProduct.author, F.text)
async def add_author(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Проверяет имя автора."""
    await _read_text_field(
        message, state, "author", 150, AddProduct.description
    )


@admin_router.message(AddProduct.author)
async def add_author2(
    message: Message, workflow_data: dict | None = None
) -> None:
    """Объясняет ожидаемый тип имени автора."""
    await message.answer("Введите имя автора текстом.")


@admin_router.message(AddProduct.description, F.text)
async def add_description(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Проверяет описание рецепта."""
    await _read_text_field(
        message, state, "description", 4000, AddProduct.price
    )


@admin_router.message(AddProduct.description)
async def add_description2(
    message: Message, workflow_data: dict | None = None
) -> None:
    """Объясняет ожидаемый тип описания."""
    await message.answer("Введите описание рецепта текстом.")


@admin_router.message(AddProduct.price, F.text)
async def add_price(
    message: Message, state: FSMContext, workflow_data: dict | None = None
) -> None:
    """Принимает только конечное неотрицательное количество."""
    data = await state.get_data()
    value = message.text
    if value == "..":
        original = data.get("recipe_original")
        if not data.get("recipe_edit_id") or not isinstance(original, dict):
            await message.answer("При добавлении нельзя пропустить это поле.")
            return
        value = original.get("price")
    try:
        price = float(str(value).replace(",", "."))
        if not math.isfinite(price) or price < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        await message.answer("Введите корректное неотрицательное количество.")
        return
    await state.update_data(price=price)
    await state.set_state(AddProduct.image)
    await message.answer(get_step_prompt(AddProduct.image.state))


@admin_router.message(AddProduct.price)
async def add_price2(
    message: Message, workflow_data: dict | None = None
) -> None:
    """Объясняет ожидаемый тип количества."""
    await message.answer("Введите количество приготовлений числом.")


@admin_router.message(AddProduct.image, or_f(F.photo, F.text == ".."))
async def add_image(
    message: Message, state: FSMContext, session: AsyncSession,
    workflow_data: dict | None = None,
) -> None:
    """Сохраняет изображение и рецепт только из состояния данного редактора."""
    data = await state.get_data()
    if message.text == "..":
        original = data.get("recipe_original")
        if not data.get("recipe_edit_id") or not isinstance(original, dict):
            await message.answer("При добавлении нужно отправить изображение.")
            return
        image = original.get("image")
        if not image:
            await message.answer("Старого изображения нет. Отправьте новое фото.")
            return
    elif message.photo:
        image = message.photo[-1].file_id
    else:
        await message.answer("Отправьте фото блюда.")
        return
    data["image"] = image
    try:
        recipe_id = data.get("recipe_edit_id")
        if recipe_id is not None:
            updated = await orm_update_recipe(session, int(recipe_id), data)
            text = "Рецепт изменен" if updated else "Рецепт уже удалён"
        else:
            await orm_add_recipe(session, data)
            text = "Рецепт добавлен"
    except (SQLAlchemyError, KeyError, TypeError, ValueError):
        await session.rollback()
        logger.exception("Не удалось сохранить рецепт")
        await _reset_editor(state)
        await message.answer(
            "Не удалось сохранить рецепт. Попробуйте ещё раз.",
            reply_markup=get_admin_keyboard(),
        )
        return
    await _reset_editor(state)
    await message.answer(text, reply_markup=get_admin_keyboard())


@admin_router.message(AddProduct.image)
async def add_image2(
    message: Message, workflow_data: dict | None = None
) -> None:
    """Объясняет ожидаемый тип изображения."""
    await message.answer("Отправьте фото блюда.")
