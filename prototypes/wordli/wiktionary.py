"""Краткие определения русских существительных из публичного Викисловаря."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from itertools import combinations
from typing import Any
from urllib.parse import quote

import aiohttp

API_URL = "https://ru.wiktionary.org/w/api.php"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
USER_AGENT = (
    "CopilotWordDefinitions/1.0 "
    "(https://github.com/van4956/bot_copilot_v2; Telegram: @Terminatorvan_bot)"
)
FETCH_TIMEOUT = 7.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MEANING_LENGTH = 350
MAX_VARIANTS = 8
_WORD = re.compile(r"[а-яё]{5}\Z")
_VOID = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }
)
_OMIT_TAGS = frozenset({"script", "style", "table", "sup", "img"})
_OMIT_CLASSES = (
    "example", "citation", "reference", "mw-editsection", "noprint"
)


class DefinitionUnavailable(RuntimeError):
    """Источник временно недоступен или вернул неожиданный ответ."""


@dataclass(slots=True)
class _Node:
    """Минимальное дерево уже отрисованной словарной статьи."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


class _ArticleParser(HTMLParser):
    """Разбирает HTML без выполнения скриптов и загрузки ресурсов."""

    def __init__(self) -> None:
        """Ограничивает глубину и число узлов внешнего документа."""
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self._stack = [self.root]
        self._count = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Добавляет элемент, не переходя по его ссылкам."""
        self._count += 1
        if self._count > 40000 or len(self._stack) > 128:
            raise DefinitionUnavailable("Слишком сложная словарная статья")
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Обрабатывает самозакрывающийся элемент."""
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Закрывает только существующий элемент дерева."""
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        """Сохраняет текст без интерпретации как разметки."""
        self._stack[-1].children.append(data)


def _walk(node: _Node) -> Iterator[_Node]:
    """Обходит дерево без рекурсии."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(
            child for child in reversed(current.children)
            if isinstance(child, _Node)
        )


def _text(node: _Node, *, definition: bool = False) -> str:
    """Убирает примеры, сноски и служебный текст, сохраняя само определение."""
    result: list[str] = []
    stack: list[_Node | str] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            result.append(item)
            continue
        classes = item.attrs.get("class", "").split()
        if item.tag in _OMIT_TAGS or any(
            value.startswith(_OMIT_CLASSES) for value in classes
        ):
            continue
        if definition and item is not node and item.tag in {"ul", "ol", "dl"}:
            continue
        if item.tag == "br":
            result.append(" ")
        stack.extend(reversed(item.children))
    text = "".join(result).split("◆", 1)[0]
    return " ".join(text.split())


def _plain_headword(value: str) -> str:
    """Сравнивает заголовки без ударения и различия Е/Ё."""
    value = re.sub(r"\s+[IVXLCDM]+$", "", value)
    return "".join(
        letter for letter in unicodedata.normalize("NFD", value.lower())
        if letter != "\u0301"
    ).replace("е\u0308", "е")


def _meaning(value: str) -> str:
    """Ограничивает короткое пояснение и делает первую букву строчной."""
    value = value.strip()
    if not value or value in {"—", "?", "??"}:
        return ""
    if len(value) > MAX_MEANING_LENGTH:
        value = (
            value[:MAX_MEANING_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
            + "…"
        )
    return value[:1].lower() + value[1:]


def parse_definition(html: str, title: str) -> tuple[str, str] | None:
    """Возвращает первое значение русского существительного без примеров."""
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()
    if not any(
        re.fullmatch(r"h[1-6]", node.tag) for node in _walk(parser.root)
    ):
        raise DefinitionUnavailable("Неизвестная разметка словарной статьи")
    russian_level: int | None = None
    noun = False
    meaning_level: int | None = None
    headword = title.lower()
    expected = _plain_headword(title)
    for node in _walk(parser.root):
        if re.fullmatch(r"h[1-6]", node.tag):
            level = int(node.tag[1])
            heading = _text(node)
            if heading == "Русский":
                russian_level = level
                noun = False
                meaning_level = None
                continue
            if russian_level is None:
                continue
            if level <= russian_level:
                break
            if _plain_headword(heading) == expected:
                headword = re.sub(r"\s+[IVXLCDM]+$", "", heading).lower()
                noun = False
                meaning_level = None
            if heading in {"Значение", "Значения"} and noun:
                meaning_level = level
            elif meaning_level is not None and level <= meaning_level:
                meaning_level = None
        elif russian_level is not None:
            if node.tag == "p" and re.match(
                r"^Существительное\b", _text(node), re.I
            ):
                noun = True
            if meaning_level is not None and node.tag == "ol":
                for child in node.children:
                    if isinstance(child, _Node) and child.tag == "li":
                        value = _meaning(_text(child, definition=True))
                        if value:
                            return headword, value
                meaning_level = None
    return None


def _variants(word: str) -> tuple[str, list[str]]:
    """Формирует не более восьми написаний для нормализованной игровой Е."""
    if not isinstance(word, str) or len(word) > 64:
        raise ValueError("Нужно русское слово из пяти букв")
    original = unicodedata.normalize("NFC", word).strip().lower()
    if not _WORD.fullmatch(original):
        raise ValueError("Нужно русское слово из пяти букв")
    base = original.replace("ё", "е")
    result = [original]
    if base not in result:
        result.append(base)
    positions = [index for index, letter in enumerate(base) if letter == "е"]
    for count in range(1, len(positions) + 1):
        for selected in combinations(positions, count):
            letters = list(base)
            for index in selected:
                letters[index] = "ё"
            candidate = "".join(letters)
            if candidate not in result:
                result.append(candidate)
            if len(result) == MAX_VARIANTS:
                return base.upper(), result
    return base.upper(), result


class WiktionaryClient:
    """Переиспользует HTTP-сессию и последовательно читает публичные статьи."""

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        """Принимает необязательную сессию для изолированных тестов."""
        self._session = session
        self._owns_session = session is None
        self._semaphore = asyncio.Semaphore(1)

    async def __aenter__(self) -> WiktionaryClient:
        """Открывает долгоживущую клиентскую сессию."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": USER_AGENT, "Accept": "application/json"
                },
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
            )
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Закрывает только созданную этим клиентом сессию."""
        await self.close()

    async def close(self) -> None:
        """Освобождает собственные HTTP-соединения."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _article(self, title: str) -> dict[str, Any] | None:
        """Получает ограниченный JSON только с официального HTTPS API."""
        if self._session is None:
            await self.__aenter__()
        assert self._session is not None
        params = {
            "action": "parse", "page": title, "prop": "text|revid",
            "redirects": "1", "format": "json", "formatversion": "2",
            "maxlag": "5",
        }
        async with self._session.get(
            API_URL, params=params, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise DefinitionUnavailable("Словарь временно недоступен")
            if (
                response.content_length is not None
                and response.content_length > MAX_RESPONSE_BYTES
            ):
                raise DefinitionUnavailable("Ответ словаря слишком большой")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise DefinitionUnavailable(
                        "Ответ словаря слишком большой"
                    )
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise DefinitionUnavailable(
                    "Некорректный ответ словаря"
                ) from error
            if not isinstance(payload, dict):
                raise DefinitionUnavailable("Некорректный ответ словаря")
            if "error" in payload:
                error = payload["error"]
                if (
                    isinstance(error, dict)
                    and error.get("code") == "missingtitle"
                ):
                    return None
                raise DefinitionUnavailable("Ошибка публичного API словаря")
            article = payload.get("parse")
            if (
                not isinstance(article, dict)
                or not isinstance(article.get("text"), str)
                or not isinstance(article.get("title"), str)
                or not article["title"]
                or len(article["title"]) > 128
                or any(character in article["title"] for character in "/:#?\\")
                or (
                    "revid" in article
                    and (
                        type(article["revid"]) is not int
                        or article["revid"] <= 0
                    )
                )
            ):
                raise DefinitionUnavailable("Неожиданная структура статьи")
            return article

    async def lookup(self, word: str) -> dict[str, Any]:
        """Ищет первое значение, отличая отсутствие слова от сетевого сбоя."""
        normalized, variants = _variants(word)
        try:
            return await asyncio.wait_for(
                self._lookup_serialized(normalized, variants), FETCH_TIMEOUT
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            raise DefinitionUnavailable(
                "Словарь временно недоступен"
            ) from error

    async def _lookup_serialized(
        self, normalized: str, variants: list[str]
    ) -> dict[str, Any]:
        """Делит один лимит времени между очередью и всеми вариантами Ё."""
        async with self._semaphore:
            for title in variants:
                article = await self._article(title)
                if article is None:
                    continue
                definition = parse_definition(
                    article["text"], article["title"]
                )
                if definition is None:
                    continue
                headword, meaning = definition
                source_url = (
                    "https://ru.wiktionary.org/wiki/"
                    + quote(article["title"], safe="")
                )
                if "revid" in article:
                    source_url += f"?oldid={article['revid']}"
                return {
                    "word": normalized, "status": "found",
                    "headword": headword,
                    "definitions": [meaning], "source_url": source_url,
                    "source_name": "Викисловарь",
                    "license_name": "CC BY-SA 4.0",
                    "license_url": LICENSE_URL, "modified": True,
                }
        return {
            "word": normalized, "status": "not_found",
            "headword": normalized.lower(),
            "definitions": [], "source_name": "Викисловарь",
        }
