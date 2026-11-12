"""Общие тексты игры и наличие каталогов в будущем игровом образе."""

import gettext
from pathlib import Path

import pytest

from prototypes.wordli.bot_texts import (
    GAME_LANGUAGE_NOTICE,
    REMINDER_BACK_TEXT,
    REMINDER_TEXT,
    with_game_language_notice,
)

ROOT = Path(__file__).resolve().parents[1]
EN_NOTICE = "Due to the game's mechanics, it is currently available only in Russian"


def english(text: str) -> str:
    """Использует настоящий скомпилированный каталог без aiogram."""
    return gettext.translation(
        "bot_00_template",
        localedir=ROOT / "locales",
        languages=["en"],
    ).gettext(text)


@pytest.mark.parametrize("locale", ["ru", "RU", "ru-RU", "ru_RU", "ru-Cyrl-RU"])
def test_russian_variants_never_get_language_warning(locale: str) -> None:
    """Русскоговорящим не показывается ненужная языковая пометка."""

    def unexpected_translation(text: str) -> str:
        """Предупреждение даже не запрашивает перевод для русского."""
        raise AssertionError(text)

    assert (
        with_game_language_notice(REMINDER_TEXT, locale, unexpected_translation)
        == REMINDER_TEXT
    )


@pytest.mark.parametrize("locale", ["en", "en-US", "en_US", "de", "fr", "zh-Hant"])
def test_every_non_russian_locale_gets_notice_without_final_period(locale: str) -> None:
    """Проверка не ограничивается только английским кодом локали."""
    base = "Which game shall we play?"
    assert (
        with_game_language_notice(base, locale, english) == f"{base}\n\n({EN_NOTICE})"
    )


def test_compiled_reminder_and_button_are_english_but_game_brand_is_unchanged() -> None:
    """Напоминание целиком английское, неизменяемый бренд остаётся прежним."""
    assert (
        english(REMINDER_TEXT)
        == "Time to play 5БУКВ! A new word is waiting for you today."
    )
    assert english(REMINDER_BACK_TEXT) == "Back to main ↩️"
    assert english(GAME_LANGUAGE_NOTICE) == EN_NOTICE


def test_game_runtime_image_includes_translation_catalogs() -> None:
    """Самостоятельный игровой образ не остаётся без MO-файлов напоминаний."""
    dockerfile = (ROOT / "Dockerfile.wordli").read_text("utf-8")
    assert "COPY locales/ /app/locales/" in dockerfile
    for locale in ("ru", "en"):
        assert (
            ROOT / "locales" / locale / "LC_MESSAGES" / "bot_00_template.mo"
        ).is_file()
