"""Проверки четырёх полных каталогов без сети и секретных настроек."""

import gettext
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

import pytest
from aiogram.utils.i18n import I18n
from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po

from common import keyboard
from common.comands import get_commands
from handlers import other

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


@pytest.mark.parametrize("language", ["ru", "en", "es", "uk"])
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
        ROOT / "prototypes" / "wordli" / "bot_texts.py",
    ]
    for path in sources:
        for _, message, _, context in extract_from_file(
            "python", path, keywords={"_": None, "__": None, "gettext": None}
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
            assert filename in {
                "app.py", "prototypes/wordli/bot_texts.py",
            } or filename.startswith(
                ("handlers/", "common/")
            )


@pytest.mark.parametrize("language", ["ru", "en", "es", "uk"])
def test_game_invitation_and_reminder_notice_have_compiled_translations(
    language: str,
) -> None:
    """Общий текст бота переводится, не затрагивая русский словарь самой игры."""
    translator = _translator(language)
    notice = "Из-за особенностей механики игры игра пока доступна только на русском языке"
    reminder = "Пора сыграть в 5БУКВ! Сегодня вас ждёт новое слово."
    translated_notice = translator.gettext(notice)
    assert translated_notice == {
        "ru": notice,
        "en": "Due to the game's mechanics, it is currently available only in Russian",
        "es": "Por las características de su mecánica, el juego está disponible por ahora solo en ruso",
        "uk": "Через особливості механіки гра наразі доступна лише російською",
    }[language]
    assert not translated_notice.endswith((".", "!", "?"))
    assert not translated_notice.startswith("(")
    translated_reminder = translator.gettext(reminder)
    assert "5БУКВ" in translated_reminder
    if language == "ru":
        assert translated_reminder == reminder
    elif language == "en":
        assert translated_reminder == "Time to play 5БУКВ! A new word is waiting for you today."
    else:
        assert translated_reminder != reminder
        assert translated_reminder != _translator("en").gettext(reminder)


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


@pytest.mark.parametrize("language,previous_translation", [
    ("ru", "Выберите период прогноза"),
    ("en", "Choose the forecast period"),
])
def test_removed_weather_period_prompt_is_only_obsolete(
    language: str, previous_translation: str
) -> None:
    """Удалённый выбор периода не попадает в активные переводы и шаблон."""
    original = "Выберите период прогноза"
    catalog = _catalog(language)
    with (ROOT / "locales" / "messages.pot").open("rb") as stream:
        template = read_po(stream)

    assert catalog.get(original) is None
    assert template.get(original) is None
    assert catalog.obsolete[original].string == previous_translation
    assert _translator(language).gettext(original) == original


def test_compiled_catalog_formats_game_score() -> None:
    """Подстановка результата работает после загрузки MO."""
    message = "Игра окончена!\nВаш счет: {score}"
    assert _translator("en").gettext(message).format(score=120) == (
        "Game over!\nYour score: 120"
    )


@pytest.mark.parametrize("language", ["ru", "en", "es", "uk"])
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


@pytest.mark.parametrize("original,expected", [
    ("Игры 🕹", "Games 🕹"),
    ("◀️ Удалить", "◀️ Delete"),
    ("Ввод ▶️", "Enter ▶️"),
    ("Кулинарная книга", "Cookbook"),
    ("Настройки", "Settings"),
    ("Выбран: 🇺🇸 Английский", "Selected: 🇺🇸 English"),
    ("Текущий язык \n\n 🇺🇸 Английский", "Current language \n\n 🇺🇸 English"),
    ("Язык не поддерживается.", "This language is not supported."),
])
def test_shared_ui_labels_have_compiled_english(
    original: str, expected: str,
) -> None:
    """Общие кнопки и подписи не возвращаются к русским строкам в EN."""
    assert _translator("en").gettext(original) == expected


@pytest.mark.parametrize("language,previous", [
    ("ru", "Автор:"), ("en", "Author:"), ("es", "Autor:"), ("uk", "Автор:"),
])
def test_removed_recipe_author_label_is_only_obsolete(
    language: str, previous: str,
) -> None:
    """Убрана только подпись шаблона, а прежние переводы остаются в архиве."""
    catalog = _catalog(language)
    with (ROOT / "locales" / "messages.pot").open("rb") as stream:
        template = read_po(stream)
    assert catalog.get("Автор:") is None
    assert template.get("Автор:") is None
    assert catalog.obsolete["Автор:"].string == previous
    assert _translator(language).gettext("Автор:") == "Автор:"


@pytest.mark.parametrize("language,game,delete,enter", [
    ("ru", "Игры 🕹", "◀️ Удалить", "Ввод ▶️"),
    ("en", "Games 🕹", "◀️ Delete", "Enter ▶️"),
    ("es", "Juegos 🕹", "◀️ Borrar", "Introducir ▶️"),
    ("uk", "Ігри 🕹", "◀️ Видалити", "Ввід ▶️"),
])
def test_shared_keyboards_use_current_catalog(
    language: str, game: str, delete: str, enter: str,
) -> None:
    """Меню игр и цифровая клавиатура используют один контекст RU/EN."""
    i18n = I18n(path=str(ROOT / "locales"), domain=DOMAIN)
    bot = SimpleNamespace(
        test_mode=False, wordli_enabled_verified=True,
        wordli_webapp_url="https://example.invalid/game",
    )
    with i18n.context(), i18n.use_locale(language):
        main = keyboard.start_keyboard(bot)
        numbers = keyboard.markup_num()
    assert main.keyboard[1][1].text == game
    assert [button.text for button in numbers.inline_keyboard[4]] == [delete, enter]
    assert [button.callback_data for button in numbers.inline_keyboard[4]] == [
        "num_del", "num_enter",
    ]


@pytest.mark.parametrize("language,descriptions", [
    ("ru", ["Главная панель", "Кулинарная книга", "Настройки"]),
    ("en", ["Main panel", "Cookbook", "Settings"]),
    ("es", ["Panel principal", "Recetario", "Ajustes"]),
    ("uk", ["Головна панель", "Кулінарна книга", "Налаштування"]),
])
def test_command_descriptions_use_explicit_locale(
    language: str, descriptions: list[str],
) -> None:
    """Командное меню не зависит от языка соседнего запроса."""
    i18n = I18n(path=str(ROOT / "locales"), domain=DOMAIN)
    with i18n.context(), i18n.use_locale("ru" if language == "en" else "en"):
        commands = get_commands(language)
    assert [command.command for command in commands] == ["main", "book", "info"]
    assert [command.description for command in commands] == descriptions


@pytest.mark.parametrize("language,heading,memory,uptime", [
    ("ru", "Состояние бота", "Память: 128.0 МБ", "Время работы:"),
    ("en", "Bot status", "RAM: 128.0 MB", "Uptime:"),
    ("es", "Estado del bot", "Memoria: 128.0 MB", "Tiempo de actividad:"),
    ("uk", "Стан бота", "Пам’ять: 128.0 МБ", "Час роботи:"),
])
async def test_status_uses_current_compiled_catalog(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    heading: str,
    memory: str,
    uptime: str,
) -> None:
    """Статус переводится целиком, а числовые значения не меняются."""
    process = SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=128 * 1024 * 1024),
        cpu_percent=lambda interval: 1.5,
        create_time=lambda: 0,
    )
    monkeypatch.setattr(other.psutil, "Process", lambda: process)
    i18n = I18n(path=str(ROOT / "locales"), domain=DOMAIN)
    with i18n.context(), i18n.use_locale(language):
        text = await other._status_text()
    assert heading in text
    assert memory in text
    assert "CPU: 1.5%" in text
    assert uptime in text
    assert "Status of Bot" not in text


@pytest.mark.parametrize("language", ["en", "es"])
def test_latin_catalog_has_no_untranslated_russian_ui(language: str) -> None:
    """Русские буквы допустимы только в неизменяемых именах игры и модели."""
    for message in _catalog(language):
        if not message.id or not isinstance(message.string, str):
            continue
        text = (
            message.string.replace("5БУКВ", "").replace("4ПО5", "").replace("Т-4", "")
        )
        assert not re.search("[А-Яа-яЁё]", text), message.id
