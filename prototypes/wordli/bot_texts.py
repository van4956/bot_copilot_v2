"""Общие тексты приглашения и напоминания без зависимости от aiogram."""

from collections.abc import Callable
import re


def _(text: str) -> str:
    """Помечает исходный текст для извлечения gettext без перевода."""
    return text


GAME_LANGUAGE_NOTICE = _(
    "Из-за особенностей механики игры игра пока доступна только на русском языке"
)
REMINDER_TEXT = _("Пора сыграть в 5БУКВ! Сегодня вас ждёт новое слово.")
REMINDER_BACK_TEXT = _("Назад на главную ↩️")


def normalize_game_locale(locale: str) -> str:
    """Проверяет безопасное имя локали и объединяет русские варианты."""
    if (
        not isinstance(locale, str)
        or len(locale) > 64
        or re.fullmatch(r"[a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,8})*", locale) is None
    ):
        raise ValueError("Некорректная локаль пользователя")
    normalized = locale.lower().replace("_", "-")
    return "ru" if normalized.split("-")[0] == "ru" else normalized


def with_game_language_notice(
    text: str, locale: str, translate: Callable[[str], str]
) -> str:
    """Добавляет пометку о языке игры только нерусскому пользователю."""
    if locale.lower().replace("_", "-").split("-")[0] == "ru":
        return text
    return f"{text}\n\n({translate(GAME_LANGUAGE_NOTICE)})"
