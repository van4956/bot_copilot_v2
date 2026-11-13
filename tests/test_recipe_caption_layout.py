"""Шаблон рецепта оформляет только введённые поля без подписи автора."""

from html import escape
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from aiogram.utils.i18n import I18n

from config_data.config import ROOT
from handlers import cookbook


class _CaptionParser(HTMLParser):
    """Проверяет теги и собирает видимый текст без изменения пробелов."""

    def __init__(self) -> None:
        """Готовит отдельные данные проверки подписи."""
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        """Допускает только жирное название и курсивного автора."""
        assert not attrs
        assert tag in {"b", "i"}
        self.stack.append(tag)
        self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        """Проверяет парность тегов оформления."""
        assert self.stack and self.stack.pop() == tag

    def handle_data(self, data: str) -> None:
        """Сохраняет видимые символы и переносы подписи."""
        self.parts.append(data)


def _parse(caption: str) -> _CaptionParser:
    """Возвращает проверенную разметку и видимый текст."""
    parser = _CaptionParser()
    parser.feed(caption)
    parser.close()
    assert not parser.stack
    assert parser.tags == ["b", "i"]
    return parser


def test_recipe_caption_adds_only_requested_layout_without_gettext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Оформление добавляется без перевода и без префикса перед автором."""
    recipe = SimpleNamespace(
        recipe_name="Название рецепта",
        author="Имя автора",
        description="Обычное описание",
    )

    def forbidden_translation(text: str) -> str:
        """Не позволяет шаблону самостоятельно переводить содержимое."""
        raise AssertionError("Перевод в шаблоне не нужен")

    monkeypatch.setattr(cookbook, "_", forbidden_translation)

    assert cookbook.format_recipe_caption(recipe) == (
        "<b>Название рецепта</b>\n<i>Имя автора</i>\n\nОбычное описание"
    )


@pytest.mark.parametrize("locale", ["ru", "en", "es", "uk"])
def test_caption_keeps_author_unchanged_in_every_locale(locale: str) -> None:
    """Язык интерфейса не добавляет подпись и не переводит имя автора."""
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    recipe = SimpleNamespace(
        recipe_name="Rice soup", author="Альберт Энштейн 🤍", description="A tasty dish."
    )
    with i18n.context(), i18n.use_locale(locale):
        caption = cookbook.format_recipe_caption(recipe)

    assert caption == "<b>Rice soup</b>\n<i>Альберт Энштейн 🤍</i>\n\nA tasty dish."
    assert "Автор:" not in caption
    assert "Author:" not in caption
    assert "Autor:" not in caption


def test_caption_preserves_description_paragraphs_and_escapes_all_fields() -> None:
    """Введённые абзацы остаются, а пользовательская разметка не исполняется."""
    recipe = SimpleNamespace(
        recipe_name="<Суп> & 'Тест'",
        author="Иван & <Jane>",
        description="  Ингредиенты:\n1. Рис <100 г>\n\n  Готовить & подать.  \n",
    )
    original = vars(recipe).copy()
    caption = cookbook.format_recipe_caption(recipe)

    assert caption == (
        f"<b>{escape(recipe.recipe_name)}</b>\n"
        f"<i>{escape(recipe.author)}</i>\n\n{escape(recipe.description)}"
    )
    assert "".join(_parse(caption).parts) == (
        f"{recipe.recipe_name}\n{recipe.author}\n\n{recipe.description}"
    )
    assert vars(recipe) == original


@pytest.mark.parametrize("extra", [0, 1, 1000])
def test_caption_limit_counts_layout_without_old_author_prefix(extra: int) -> None:
    """Удалённая подпись больше не занимает бюджет 1024 видимых символов."""
    recipe = SimpleNamespace(recipe_name="N", author="A", description="x" * (1019 + extra))
    caption = cookbook.format_recipe_caption(recipe)
    visible = "".join(_parse(caption).parts)

    assert visible.startswith("N\nA\n\n")
    assert len(visible.encode("utf-16-le")) // 2 == 1024
    assert visible.endswith("…") == bool(extra)
    if not extra:
        assert visible.endswith(recipe.description)


def test_long_unicode_caption_clips_fields_without_mutating_recipe() -> None:
    """Длинные эмодзи не ломают HTML, UTF-16 и исходные поля рецепта."""
    recipe = SimpleNamespace(
        recipe_name="🍲" * 200,
        author="👩‍🍳" * 150,
        description="😀<tag>&\n" * 1000,
    )
    original = vars(recipe).copy()
    caption = cookbook.format_recipe_caption(recipe)
    visible = "".join(_parse(caption).parts)
    title, author, description = visible.split("\n", 2)

    assert len(visible.encode("utf-16-le")) // 2 <= 1024
    assert len(title.encode("utf-16-le")) // 2 <= 150
    assert len(author.encode("utf-16-le")) // 2 <= 150
    assert description.startswith("\n")
    assert visible.endswith("…")
    assert not any(0xD800 <= ord(character) <= 0xDFFF for character in visible)
    assert vars(recipe) == original
