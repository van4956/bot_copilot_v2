"""Поддерживаемые языки бота и нормализация кода Telegram."""

LANGUAGE_CODES = ("ru", "en", "es", "uk")
SUPPORTED_LOCALES = frozenset(LANGUAGE_CODES)


def normalize_locale(value: str | None) -> str:
    """Возвращает базовый поддерживаемый язык либо русский резерв."""
    if not isinstance(value, str):
        return "ru"
    language = value.strip().replace("_", "-").split("-", 1)[0].lower()
    return language if language in SUPPORTED_LOCALES else "ru"
