"""Проверки полных каталогов RU/EN без сети и секретных настроек."""

import gettext
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from string import Formatter

import pytest
from aiogram.utils.i18n import I18n
from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po

from common import keyboard

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "bot_00_template"
FORMATTER = Formatter()


def _catalog(language: str):
    """Читает только публичный каталог переводов."""
    path = ROOT / "locales" / language / "LC_MESSAGES" / f"{DOMAIN}.po"
    with path.open("rb") as stream:
        return read_po(stream)


def _translator(language: str) -> gettext.GNUTranslations:
    """Читает скомпилированный каталог приложения."""
    return gettext.translation(
        DOMAIN, localedir=ROOT / "locales", languages=[language]
    )


def _fields(value: str) -> Counter:
    """Считает все подстановки, включая вложенные спецификаторы."""
    fields = Counter()
    for _, field, spec, _ in FORMATTER.parse(value):
        if field is not None:
            fields[field] += 1
        if spec:
            fields.update(_fields(spec))
    return fields


class _Markup(HTMLParser):
    """Проверяет структуру неизменяемых HTML-тегов."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags = Counter()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        """Запоминает открывающий тег."""
        self.stack.append(tag)
        self.tags[tag] += 1

    def handle_endtag(self, tag: str) -> None:
        """Проверяет парность тегов."""
        assert self.stack and self.stack.pop() == tag


@pytest.mark.parametrize("language", ["ru", "en"])
def test_active_catalog_is_complete_and_compiled(language: str) -> None:
    """Все активные сообщения переведены и совпадают с бинарным каталогом."""
    catalog = _catalog(language)
    translator = _translator(language)
    with (ROOT / "locales" / "messages.pot").open("rb") as stream:
        template = read_po(stream)
    actual = {
        (message.id, message.context)
        for message in catalog if message.id
    }
    expected = {
        (message.id, message.context)
        for message in template if message.id
    }
    assert actual == expected
    assert not list(catalog.check())
    for message in catalog:
        if not message.id:
            continue
        assert message.string
        assert not message.fuzzy
        if isinstance(message.id, tuple):
            for original, translated in zip(message.id, message.string):
                assert _fields(original) == _fields(translated)
        else:
            assert _fields(message.id) == _fields(message.string)
            assert translator.gettext(message.id) == message.string


def test_template_matches_only_current_ui_sources() -> None:
    """Шаблон извлекается из нужных модулей, не из окружения или секретов."""
    expected = set()
    sources = [
        ROOT / "app.py",
        *sorted((ROOT / "handlers").glob("*.py")),
        *sorted((ROOT / "common").glob("*.py")),
    ]
    for path in sources:
        for _, message, _, context in extract_from_file(
            "python", path, keywords={"_": None, "__": None}
        ):
            expected.add((message, context))
    with (ROOT / "locales" / "messages.pot").open("rb") as stream:
        template = read_po(stream)
    actual = {
        (message.id, message.context)
        for message in template if message.id
    }

    assert actual == expected
    for message in template:
        for filename, _ in message.locations:
            assert filename == "app.py" or filename.startswith(
                ("handlers/", "common/")
            )


def test_russian_catalog_keeps_new_messages_in_russian() -> None:
    """Новые сообщения RU не заменяются английским переводом."""
    translator = _translator("ru")
    for text in [
        "Откройте книгу заново",
        "Не удалось открыть страницу",
        "Сервис валют временно недоступен",
        "Сервис погоды временно недоступен",
        "Счёт недействителен. Создайте новый донат.",
        "Не удалось проверить счёт. Попробуйте позже.",
        "Донат успешно возвращён.",
        "Не удалось принять результат игры.",
        "Слова 🧩",
        "Данная функция в разработке",
        "Мини-приложения временно недоступны",
        "Введите неотрицательное число до 18 символов: цифры и точка.",
        "Эта клавиатура устарела. Используйте текущую.",
    ]:
        assert translator.gettext(text) == text


@pytest.mark.parametrize("original,expected", [
    ("Погода 🌊", "Weather 🌊"),
    ("Валюта 💵", "Currency 💵"),
    ("Котики 🐱", "Cats 🐱"),
    ("Книга 📖", "Cookbook 📖"),
    ("Слова 🧩", "Words 🧩"),
    ("Назад на главную ↩️", "Back to main ↩️"),
    ("Сервисы", "Services"),
    ("Игры", "Games"),
])
def test_existing_menu_translations_are_preserved(
    original: str, expected: str
) -> None:
    """Существующие правильные переводы основных меню не потеряны."""
    assert _translator("en").gettext(original) == expected


@pytest.mark.parametrize("original,expected", [
    ("Откройте книгу заново", "Open the cookbook again."),
    ("Не удалось открыть страницу", "Could not open this page."),
    (
        "Сервис валют временно недоступен",
        "The exchange rate service is temporarily unavailable.",
    ),
    (
        "Сервис погоды временно недоступен",
        "The weather service is temporarily unavailable.",
    ),
    (
        "Не удалось проверить счёт. Попробуйте позже.",
        "Could not verify the invoice. Please try again later.",
    ),
    (
        "Не удалось сверить платёж. Обратитесь к автору бота.",
        "Could not verify the payment. Please contact the bot's author.",
    ),
    (
        "Не удалось принять результат игры.",
        "Could not accept the game result.",
    ),
    ("Укажите ID транзакции: /refund ID", "Enter the transaction ID: /refund ID"),
    ("Донат успешно возвращён.", "The donation has been refunded."),
    (
        "Мини-приложения не настроены для этого бота.",
        "Mini apps are not configured for this bot.",
    ),
])
def test_new_errors_have_human_readable_english(
    original: str, expected: str
) -> None:
    """Ошибки не возвращаются на русском при выборе EN."""
    translated = _translator("en").gettext(original)

    assert translated == expected
    assert not re.search("[А-Яа-яЁё]", translated)
    assert "<ID>" not in translated


@pytest.mark.parametrize("original,expected", [
    ("Слова 🧩", "Words 🧩"),
    ("Данная функция в разработке", "This feature is under development"),
    (
        "Мини-приложения временно недоступны",
        "Mini apps are temporarily unavailable",
    ),
    (
        "Введите неотрицательное число до 18 символов: цифры и точка.",
        "Enter a non-negative number of up to 18 characters, "
        "using digits and a decimal point.",
    ),
    (
        "Эта клавиатура устарела. Используйте текущую.",
        "This keyboard is outdated. Use the current one.",
    ),
])
def test_updated_ui_has_compiled_english_translation(
    original: str, expected: str
) -> None:
    """Новые кнопки и подсказки читаются из английского MO-каталога."""
    assert _translator("en").gettext(original) == expected


@pytest.mark.parametrize("language,previous_translation", [
    ("ru", "Введите сумму сообщением или используйте кнопки и Enter."),
    ("en", "Send the amount as a message, or use the buttons and Enter."),
])
def test_removed_amount_hint_is_only_obsolete(
    language: str, previous_translation: str
) -> None:
    """Удалённая подсказка не активна, прежний перевод сохранён."""
    original = "Введите сумму сообщением или используйте кнопки и Enter."
    catalog = _catalog(language)

    assert catalog.get(original) is None
    assert catalog.obsolete[original].string == previous_translation
    assert _translator(language).gettext(original) == original


def test_compiled_catalog_formats_game_score() -> None:
    """Подстановка результата работает после загрузки MO."""
    message = "Игра окончена!\nВаш счет: {score}"
    assert _translator("en").gettext(message).format(score=120) == (
        "Game over!\nYour score: 120"
    )


@pytest.mark.parametrize("language", ["ru", "en"])
def test_translated_markup_is_balanced(language: str) -> None:
    """Перевод не ломает и не добавляет HTML-теги."""
    for message in _catalog(language):
        if not message.id or not isinstance(message.id, str):
            continue
        original = _Markup()
        translated = _Markup()
        original.feed(message.id)
        translated.feed(message.string)
        assert original.stack == []
        assert translated.stack == []
        assert original.tags == translated.tags


def test_actual_keyboard_uses_compiled_english_catalog() -> None:
    """Настоящая клавиатура получает EN через контекст aiogram."""
    i18n = I18n(path=str(ROOT / "locales"), domain=DOMAIN, default_locale="ru")
    with i18n.context(), i18n.use_locale("en"):
        markup = keyboard.start_keyboard()
    texts = [button.text for row in markup.keyboard for button in row]

    assert texts == ["Weather 🌊", "Currency 💵", "Cats 🐱", "Words 🧩"]
