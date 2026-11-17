"""Готовые локальные определения без сети, генерации и словарного кэша БД."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit

from .engine import DEFAULT_DICTIONARY, GameError
from .storage import WordliStorage

DEFAULT_DEFINITIONS = (
    Path(__file__).resolve().parent / "data/definitions_ru.json"
)
MAX_CATALOG_BYTES = 4 * 1024 * 1024
_WORD = re.compile(r"[А-Я]{5}\Z")


class DefinitionCatalogError(ValueError):
    """Не позволяет запустить сервер с неполным или повреждённым словарём."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклоняет повторные ключи, которые обычный JSON-разбор скрывает."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DefinitionCatalogError(
                "Повторный ключ в словаре определений"
            )
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    """Читает ограниченный локальный файл без подмены повреждённых данных."""
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise DefinitionCatalogError("Словарь определений слишком большой")
        return json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_unique_object
        )
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, DefinitionCatalogError):
            raise
        raise DefinitionCatalogError(
            "Не удалось прочитать локальный словарь"
        ) from None


def _validate_entry(word: str, entry: Any) -> dict[str, str]:
    """Проверяет форму записи и обычную ссылку именно на её русское слово."""
    if (
        not isinstance(entry, dict)
        or set(entry) != {"headword", "definition", "wiktionary_url"}
        or any(not isinstance(value, str) for value in entry.values())
    ):
        raise DefinitionCatalogError(f"Неверная запись определения: {word}")
    headword = unicodedata.normalize("NFC", entry["headword"])
    plain = headword.replace("\u0301", "")
    accents = headword.count("\u0301")
    if (
        len(headword) > 20
        or not re.fullmatch(r"[а-яё\u0301]+", headword)
        or plain.upper().replace("Ё", "Е") != word
        or ("ё" in headword and accents)
        or ("ё" not in headword and accents != 1)
        or any(
            index == 0 or headword[index - 1] not in "аеёиоуыэюя"
            for index, letter in enumerate(headword)
            if letter == "\u0301"
        )
    ):
        raise DefinitionCatalogError(f"Неверная форма заголовка: {word}")
    definition = entry["definition"]
    if (
        not 15 <= len(definition) <= 200
        or definition != definition.strip()
        or not definition[:1].islower()
        or not definition.endswith(".")
        or any(character in definition for character in "<>`")
        or any(
            unicodedata.category(character).startswith("C")
            for character in definition
        )
    ):
        raise DefinitionCatalogError(f"Неверный формат пояснения: {word}")
    try:
        url = urlsplit(entry["wiktionary_url"])
        title = unicodedata.normalize(
            "NFC", unquote(url.path.removeprefix("/wiki/"))
        )
        if (
            url.scheme != "https"
            or url.hostname != "ru.wiktionary.org"
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or not url.path.startswith("/wiki/")
            or title.upper().replace("Ё", "Е") != word
            or url.query
            or url.fragment
        ):
            raise ValueError
    except ValueError:
        raise DefinitionCatalogError(
            f"Неверная ссылка на Викисловарь: {word}"
        ) from None
    return {
        "headword": headword,
        "definition": definition,
        "wiktionary_url": entry["wiktionary_url"],
    }


class StaticDefinitionCatalog:
    """Проверяет полный набор один раз и хранит его только на стороне сервера."""

    def __init__(
        self,
        path: Path = DEFAULT_DEFINITIONS,
        *,
        dictionary_path: Path = DEFAULT_DICTIONARY,
    ) -> None:
        """Сверяет версию и точное покрытие ответов; тесты подставляют свои пути."""
        dictionary = _read_json(Path(dictionary_path))
        catalog = _read_json(Path(path))
        if not isinstance(dictionary, dict) or not isinstance(
            dictionary.get("answers"), list
        ):
            raise DefinitionCatalogError("Неверный игровой словарь")
        answers = dictionary["answers"]
        if (
            not answers
            or any(
                not isinstance(word, str) or not _WORD.fullmatch(word)
                for word in answers
            )
            or len(set(answers)) != len(answers)
            or not isinstance(dictionary.get("dictionary_id"), str)
            or not dictionary["dictionary_id"]
        ):
            raise DefinitionCatalogError(
                "Неверный набор ответов игрового словаря"
            )
        if (
            not isinstance(catalog, dict)
            or set(catalog) != {"schema_version", "dictionary_id", "entries"}
            or type(catalog["schema_version"]) is not int
            or catalog["schema_version"] != 1
            or catalog["dictionary_id"] != dictionary["dictionary_id"]
            or not isinstance(catalog["entries"], dict)
            or set(catalog["entries"]) != set(answers)
        ):
            raise DefinitionCatalogError(
                "Определения не покрывают текущий словарь ответов"
            )
        self.dictionary_id = dictionary["dictionary_id"]
        self._entries = MappingProxyType(
            {
                word: MappingProxyType(_validate_entry(word, entry))
                for word, entry in catalog["entries"].items()
            }
        )

    def lookup(self, word: str) -> dict[str, Any]:
        """Возвращает новую короткую запись, а не весь словарь или общий объект."""
        entry = self._entries.get(word)
        if entry is None:
            raise GameError(
                "definition_unavailable", "Значение временно недоступно.", 503
            )
        return {
            "word": word,
            "status": "found",
            "headword": entry["headword"],
            "definitions": [entry["definition"]],
            "source_url": entry["wiktionary_url"],
            "source_name": "Викисловарь",
        }


class DefinitionService:
    """Проверяет личную победу до выдачи готового определения."""

    def __init__(
        self, storage: WordliStorage, catalog: StaticDefinitionCatalog
    ) -> None:
        """Принимает серверное хранилище и уже проверенный локальный набор."""
        self._storage = storage
        self._catalog = catalog

    async def get(self, user_id: int, value: str) -> dict[str, Any]:
        """Не создаёт профиль, не меняет партию и не обращается к внешнему API."""
        word = await self._storage.require_won_word(user_id, value)
        return self._catalog.lookup(word)
