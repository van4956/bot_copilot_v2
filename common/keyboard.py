"""Клавиатуры пользовательского интерфейса."""

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

del_kb = ReplyKeyboardRemove()

# del_inline_kb = ReplyI


# Функция создания клавиатуры
def get_keyboard(
    *btns: str,
    placeholder: str | None = None,
    request_contact: int | None = None,
    request_location: int | None = None,
    sizes: tuple = (2,),
) -> ReplyKeyboardMarkup:
    """
    Функция создания обычной клавиатуры.
    Параметры request_contact и request_location должны быть индексами аргументов btns для нужных вам кнопок.
    Пример:
    get_keyboard("Меню", "Варианты оплаты", "Варианты доставки", "Отправить номер телефона", placeholder="Что вас интересует?", request_contact=3, sizes=(2, 2, 1))
    """
    keyboard = ReplyKeyboardBuilder()

    for index, text in enumerate(btns, start=0):
        if request_contact is not None and request_contact == index:
            keyboard.add(KeyboardButton(text=text, request_contact=True))
        elif request_location is not None and request_location == index:
            keyboard.add(KeyboardButton(text=text, request_location=True))
        else:
            keyboard.add(KeyboardButton(text=text))

    return keyboard.adjust(*sizes).as_markup(
        resize_keyboard=True,  # сделать кнопки поменьше
        input_field_placeholder=placeholder,
    )  # в поле ввода выводим текст placeholder


def wordli_custom_emoji(bot: Bot | None = None) -> tuple[str, str] | None:
    """Возвращает явно настроенный custom emoji только тестового Wordli."""
    identifier = getattr(bot, "wordli_custom_emoji_id", "")
    if (
        getattr(bot, "test_mode", False) is not True
        or not getattr(bot, "wordli_webapp_url", "")
        or not isinstance(identifier, str)
        or not identifier.isascii()
        or not identifier.isdigit()
        or not 1 <= len(identifier) <= 32
    ):
        return None
    fallback = getattr(bot, "wordli_custom_emoji_fallback", "") or "🧩"
    if not isinstance(fallback, str) or not fallback.strip():
        return None
    return identifier, fallback


def wordli_enabled(bot: Bot | None = None) -> bool:
    """Проверяет тестовый вход либо явно разрешённую рабочую игру."""
    test_mode = getattr(bot, "test_mode", None)
    return (
        (
            test_mode is True
            or (
                test_mode is False
                and getattr(bot, "wordli_enabled_verified", False) is True
            )
        )
        and bool(getattr(bot, "wordli_webapp_url", ""))
    )


def start_keyboard(bot: Bot | None = None) -> ReplyKeyboardMarkup:
    """Создаёт главную панель со значком разрешённой игры."""
    markup = get_keyboard(
        _("Погода 🌊"),
        _("Валюта 💵"),
        _("Котики 🐱"),
        _("Слова 🧩"),
        sizes=(
            2,
            2,
        ),
        placeholder="⬇️",
    )
    if wordli_enabled(bot):
        markup.keyboard[1][1].text = _("Игры 🕹")
    return markup


# создать обычные inline кнопки с отображаемым текстом
def get_callback_btns(
    *,  # запрет на передачу неименованных аргументов
    btns: dict[
        str, str
    ],  # передаем словарик text:data, text то что будет отображаться в боте, data то что отправится внутри
    sizes: tuple = (2,),
) -> InlineKeyboardMarkup:  # кортеж, разметка кнопок
    """создать обычные кнопки с отображаемым текстом"""

    keyboard = InlineKeyboardBuilder()

    for text, data in btns.items():
        keyboard.add(
            InlineKeyboardButton(text=text, callback_data=data)
        )  # событие callback_data

    return keyboard.adjust(*sizes).as_markup()


# создать inline кнопки с url ссылками
def get_url_btns(*, btns: dict[str, str], sizes: tuple = (2,)) -> InlineKeyboardMarkup:
    """создать кнопки с url ссылками"""

    keyboard = InlineKeyboardBuilder()

    for text, url in btns.items():
        keyboard.add(InlineKeyboardButton(text=text, url=url))

    return keyboard.adjust(*sizes).as_markup()


# создать микс из CallBack кнопок и URL кнопок
def get_inlineMix_btns(
    *, btns: dict[str, str], sizes: tuple = (2,)
) -> InlineKeyboardMarkup:
    """создать микс из CallBack кнопок и URL кнопок"""

    keyboard = InlineKeyboardBuilder()

    for text, value in btns.items():
        if "://" in value:
            keyboard.add(InlineKeyboardButton(text=text, url=value))
        else:
            keyboard.add(InlineKeyboardButton(text=text, callback_data=value))

    return keyboard.adjust(*sizes).as_markup()


# inline клавиатура с цифрами и точкой, удалением и вводом
def markup_num() -> InlineKeyboardMarkup:
    """inline клавиатура с цифрами и точкой, удалением и вводом"""
    buttons = [
        [
            InlineKeyboardButton(text=" 1 ", callback_data="num_1"),
            InlineKeyboardButton(text="   2   ", callback_data="num_2"),
            InlineKeyboardButton(text=" 3 ", callback_data="num_3"),
        ],
        [
            InlineKeyboardButton(text=" 4 ", callback_data="num_4"),
            InlineKeyboardButton(text=" 5 ", callback_data="num_5"),
            InlineKeyboardButton(text="   6   ", callback_data="num_6"),
        ],
        [
            InlineKeyboardButton(text=" 7 ", callback_data="num_7"),
            InlineKeyboardButton(text=" 8 ", callback_data="num_8"),
            InlineKeyboardButton(text=" 9 ", callback_data="num_9"),
        ],
        [
            InlineKeyboardButton(text=" 0 ", callback_data="num_0"),
            InlineKeyboardButton(text=" 000 ", callback_data="num_000"),
            InlineKeyboardButton(text=" . ", callback_data="num_com"),
        ],
        [
            InlineKeyboardButton(text=_("◀️ Удалить"), callback_data="num_del"),
            InlineKeyboardButton(text=_("Ввод ▶️"), callback_data="num_enter"),
        ],
        [
            InlineKeyboardButton(
                text=_("Назад на главную ↩️"), callback_data="about_back_to_main"
            )
        ],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


# inline клавиатура только с 9 цифрами
def markup_num_9() -> InlineKeyboardMarkup:
    """inline клавиатура только с 9 цифрами"""
    buttons = [
        [
            InlineKeyboardButton(text=" 1 ", callback_data="num_1"),
            InlineKeyboardButton(text=" 2 ", callback_data="num_2"),
            InlineKeyboardButton(text=" 3 ", callback_data="num_3"),
        ],
        [
            InlineKeyboardButton(text=" 4 ", callback_data="num_4"),
            InlineKeyboardButton(text=" 5 ", callback_data="num_5"),
            InlineKeyboardButton(text=" 6 ", callback_data="num_6"),
        ],
        [
            InlineKeyboardButton(text=" 7 ", callback_data="num_7"),
            InlineKeyboardButton(text=" 8 ", callback_data="num_8"),
            InlineKeyboardButton(text=" 9 ", callback_data="num_9"),
        ],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard
