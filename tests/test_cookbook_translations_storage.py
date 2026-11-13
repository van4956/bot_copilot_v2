"""Проверки переводов на временной SQLite без рабочих данных."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from database.models import Base, CookbookEnglish
from database.orm_cookbook import (
    orm_add_recipe,
    orm_delete_recipe,
    orm_get_recipe,
    orm_get_recipes,
    orm_update_recipe,
)
from database.orm_cookbook_translations import (
    RecipeSource,
    RecipeTranslation,
    TranslationPublishResult,
    orm_get_localized_recipes,
    orm_list_pending_translations,
    orm_save_translations,
    recipe_source_hash,
)


@asynccontextmanager
async def _database(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Создаёт отдельную базу только внутри временной папки теста."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'recipes.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _values(number: int = 1, **changes: Any) -> dict[str, Any]:
    """Формирует русский оригинал с общими реквизитами."""
    return {
        "recipe_name": f"Блины {number}",
        "author": "Иван",
        "description": f"Молоко 200 мл. Испечь {number} блинов.",
        "price": 3.0,
        "image": f"photo_{number}",
        **changes,
    }


def _translation(source: RecipeSource, suffix: str = "") -> RecipeTranslation:
    """Создаёт готовый результат без обращения к переводчику."""
    return RecipeTranslation(
        source=source,
        recipe_name=f"Pancakes {source.recipe_id}{suffix}",
        description=f"Milk 200 ml. Cook pancakes.{suffix}",
        model="test-model",
    )


def test_hash_and_source_immutability() -> None:
    """Версия зависит от обоих полей, а снимок нельзя незаметно изменить."""
    value = recipe_source_hash("Блины", "Молоко")
    assert value == recipe_source_hash("Блины", "Молоко")
    assert value != recipe_source_hash("Оладьи", "Молоко")
    assert value != recipe_source_hash("Блины", "Вода")
    assert recipe_source_hash("a:b", "c") != recipe_source_hash("a", "b:c")
    source = RecipeSource(1, "Блины", "Молоко", value)
    with pytest.raises(FrozenInstanceError):
        source.description = "Вода"


@pytest.mark.parametrize("locale", ["ru", "ru-RU", "RU_ru", "en", "es", "uk", "de", ""])
def test_missing_translation_falls_back_to_russian(
    tmp_path: Path,
    locale: str,
) -> None:
    """Без EN любой язык получает оригинал с тем же ID и фотографией."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            original = await orm_add_recipe(session, _values())
            recipes = await orm_get_localized_recipes(session, locale)
            assert len(recipes) == 1
            recipe = recipes[0]
            assert recipe.recipe_id == original.recipe_id
            for field in _values():
                assert getattr(recipe, field) == getattr(original, field)
            assert recipe.translation_revision

    asyncio.run(scenario())


@pytest.mark.parametrize("locale", ["en", "en-US", "es", "es-MX", "uk", "uk-UA", "de", "fr", ""])
def test_english_is_used_for_all_non_russian_locales(
    tmp_path: Path,
    locale: str,
) -> None:
    """Меняются только переводимые поля, а RU и общие поля сохранены."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            original = await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            result = await orm_save_translations(
                session, [_translation(source)]
            )
            assert result == TranslationPublishResult(saved=1)
            (english,) = await orm_get_localized_recipes(session, locale)
            (russian,) = await orm_get_localized_recipes(session, "ru")
            assert english.recipe_name == "Pancakes 1"
            assert english.description == "Milk 200 ml. Cook pancakes."
            assert russian.recipe_name == original.recipe_name
            assert russian.description == original.description
            assert english.recipe_id == russian.recipe_id == original.recipe_id
            stored = await session.get(CookbookEnglish, original.recipe_id)
            assert stored is not None
            assert stored.source_hash == source.source_hash
            assert stored.model == "test-model"
            for field in ("author", "price", "image"):
                assert getattr(english, field) == getattr(original, field)
            assert english.translation_revision != russian.translation_revision
            assert await orm_list_pending_translations(session) == []

    asyncio.run(scenario())


def test_stale_english_remains_visible_until_synchronization(
    tmp_path: Path,
) -> None:
    """Изменение RU помечает EN устаревшим, но не скрывает его."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            recipe = await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            await orm_save_translations(session, [_translation(source)])
            (old_english,) = await orm_get_localized_recipes(session, "en")
            (old_russian,) = await orm_get_localized_recipes(session, "ru")
            await orm_update_recipe(
                session,
                recipe.recipe_id,
                _values(
                    recipe_name="Новые блины",
                    description="Вода 100 мл.",
                    author="Другой автор",
                    image="new_photo",
                    price=5,
                ),
            )
            (english,) = await orm_get_localized_recipes(session, "en")
            (russian,) = await orm_get_localized_recipes(session, "ru")
            assert english.recipe_name == old_english.recipe_name
            assert english.description == old_english.description
            assert (
                english.translation_revision
                == old_english.translation_revision
            )
            assert english.author == "Другой автор"
            assert english.image == "new_photo"
            assert english.price == 5
            assert russian.recipe_name == "Новые блины"
            assert (
                russian.translation_revision
                != old_russian.translation_revision
            )
            (pending,) = await orm_list_pending_translations(session)
            assert pending.source_hash != source.source_hash
            await orm_save_translations(
                session, [_translation(pending, " new")]
            )
            (refreshed,) = await orm_get_localized_recipes(session, "en")
            assert refreshed.recipe_name.endswith(" new")
            assert (
                refreshed.translation_revision
                != old_english.translation_revision
            )
            assert await orm_list_pending_translations(session) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("author", "Новый автор"),
        ("image", "different_photo"),
        ("price", 11),
    ],
)
def test_shared_fields_do_not_require_translation(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    """Смена автора, фото или количества приготовлений не меняет хеш текста."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            recipe = await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            await orm_save_translations(session, [_translation(source)])
            await orm_update_recipe(
                session, recipe.recipe_id, _values(**{field: value})
            )
            assert await orm_list_pending_translations(session) == []

    asyncio.run(scenario())


def test_new_and_changed_recipes_have_stable_order(tmp_path: Path) -> None:
    """Новый рецепт добавляется в очередь, существующий не меняет ID."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            first = await orm_add_recipe(session, _values(1))
            second = await orm_add_recipe(session, _values(2))
            pending = await orm_list_pending_translations(session)
            assert [source.recipe_id for source in pending] == [1, 2]
            await orm_save_translations(session, map(_translation, pending))
            assert await orm_update_recipe(
                session, first.recipe_id, _values(recipe_name="Новые блины")
            )
            await orm_add_recipe(session, _values(3))
            assert [
                source.recipe_id
                for source in await orm_list_pending_translations(session)
            ] == [1, 3]
            assert [
                recipe.recipe_id
                for recipe in await orm_get_localized_recipes(session, "en")
            ] == [1, 2, 3]
            assert [
                recipe.recipe_id for recipe in await orm_get_recipes(session)
            ] == [first.recipe_id, second.recipe_id, 3]

    asyncio.run(scenario())


def test_delete_cleans_english_with_sqlite_foreign_keys_disabled(
    tmp_path: Path,
) -> None:
    """Удаление очищает EN даже без поддержки каскада SQLite."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            assert await session.scalar(text("pragma foreign_keys")) == 0
            first = await orm_add_recipe(session, _values())
            second = await orm_add_recipe(session, _values(2))
            pending = await orm_list_pending_translations(session)
            await orm_save_translations(session, map(_translation, pending))
            assert await orm_delete_recipe(session, first.recipe_id)
            assert await session.get(CookbookEnglish, first.recipe_id) is None
            assert not await orm_delete_recipe(session, first.recipe_id)
            (english,) = await orm_get_localized_recipes(session, "en")
            assert english.recipe_id == second.recipe_id
            assert await orm_get_recipe(session, first.recipe_id) is None

    asyncio.run(scenario())


def test_publish_rechecks_changes_and_deletion(tmp_path: Path) -> None:
    """Ответ переводчика не воскрешает удалённое и не перезаписывает новую RU."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            for number in (1, 2, 3):
                await orm_add_recipe(session, _values(number))
            pending = await orm_list_pending_translations(session)
            await session.rollback()
            async with factory() as editor:
                await orm_update_recipe(
                    editor, 1, _values(recipe_name="Изменено")
                )
                assert await orm_delete_recipe(editor, 2)
            result = await orm_save_translations(
                session, map(_translation, pending)
            )
            assert result == TranslationPublishResult(
                saved=1, changed=1, deleted=1
            )
            assert await session.get(CookbookEnglish, 1) is None
            assert await session.get(CookbookEnglish, 2) is None
            assert await session.get(CookbookEnglish, 3) is not None

    asyncio.run(scenario())


def test_changed_snapshot_does_not_overwrite_existing_english(
    tmp_path: Path,
) -> None:
    """Опоздавший ответ не затирает уже сохранённый EN."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            await orm_save_translations(session, [_translation(source)])
            await orm_update_recipe(
                session, 1, _values(description="Другое описание")
            )
            result = await orm_save_translations(
                session, [_translation(source, " late")]
            )
            assert result == TranslationPublishResult(changed=1)
            (english,) = await orm_get_localized_recipes(session, "en")
            assert english.recipe_name == "Pancakes 1"

    asyncio.run(scenario())


def test_failed_commit_rolls_back_entire_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка фиксации не публикует даже первую строку пакета."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            for number in (1, 2):
                await orm_add_recipe(session, _values(number))
            pending = await orm_list_pending_translations(session)
            monkeypatch.setattr(
                session, "commit", AsyncMock(side_effect=RuntimeError("fail"))
            )
            with pytest.raises(RuntimeError, match="fail"):
                await orm_save_translations(
                    session, map(_translation, pending)
                )
            async with factory() as reader:
                assert (
                    list(await reader.scalars(select(CookbookEnglish))) == []
                )
                assert len(await orm_get_recipes(reader)) == 2
            assert not session.in_transaction()

    asyncio.run(scenario())


def test_failed_delete_rolls_back_russian_and_english(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неуспешное удаление не оставляет рецепт без его перевода."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            await orm_save_translations(session, [_translation(source)])
            monkeypatch.setattr(
                session, "commit", AsyncMock(side_effect=RuntimeError("fail"))
            )
            with pytest.raises(RuntimeError, match="fail"):
                await orm_delete_recipe(session, 1)
            async with factory() as reader:
                assert await orm_get_recipe(reader, 1) is not None
                assert await reader.get(CookbookEnglish, 1) is not None

    asyncio.run(scenario())


def test_failed_second_write_restores_previous_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка второй записи откатывает обновление первого готового EN."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            for number in (1, 2):
                await orm_add_recipe(session, _values(number))
            pending = await orm_list_pending_translations(session)
            await orm_save_translations(session, map(_translation, pending))
            for number in (1, 2):
                await orm_update_recipe(
                    session, number, _values(number, description="Новый текст")
                )
            pending = await orm_list_pending_translations(session)
            original_scalar = session.scalar
            writes = 0

            async def intercept(
                statement: Any,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                """Имитирует ошибку БД после первой успешной записи."""
                nonlocal writes
                if statement.is_insert:
                    writes += 1
                    if writes == 2:
                        raise RuntimeError("second write failed")
                return await original_scalar(statement, *args, **kwargs)

            monkeypatch.setattr(session, "scalar", intercept)
            with pytest.raises(RuntimeError, match="second write failed"):
                await orm_save_translations(
                    session,
                    [_translation(source, " new") for source in pending],
                )
            assert writes == 2
            async with factory() as reader:
                recipes = await orm_get_localized_recipes(reader, "en")
                assert [recipe.recipe_name for recipe in recipes] == [
                    "Pancakes 1",
                    "Pancakes 2",
                ]
                assert len(await orm_list_pending_translations(reader)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["changed", "deleted"])
def test_conditional_publish_guards_race_after_initial_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """SQLite проверяет оригинал также непосредственно при вставке EN."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            await session.rollback()
            original_scalar = session.scalar
            triggered = False

            async def intercept(
                statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                """Меняет оригинал между первой проверкой и условной вставкой."""
                nonlocal triggered
                if statement.is_insert and not triggered:
                    triggered = True
                    async with factory() as editor:
                        if change == "changed":
                            await orm_update_recipe(
                                editor, 1, _values(recipe_name="Другой")
                            )
                        else:
                            assert await orm_delete_recipe(editor, 1)
                return await original_scalar(statement, *args, **kwargs)

            monkeypatch.setattr(session, "scalar", intercept)
            result = await orm_save_translations(
                session, [_translation(source)]
            )
            assert triggered
            assert result == TranslationPublishResult(**{change: 1})
            assert await session.get(CookbookEnglish, 1) is None

    asyncio.run(scenario())


def test_invalid_batch_and_empty_batch_are_not_published(
    tmp_path: Path,
) -> None:
    """Пустой пакет безопасен, дубликаты и подменённый хеш отклоняются."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            assert (
                await orm_save_translations(session, [])
                == TranslationPublishResult()
            )
            await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            translated = _translation(source)
            with pytest.raises(ValueError, match="повторно"):
                await orm_save_translations(session, [translated, translated])
            forged = replace(
                translated, source=replace(source, source_hash="0" * 64)
            )
            with pytest.raises(ValueError, match="Хеш"):
                await orm_save_translations(session, [forged])
            assert await session.get(CookbookEnglish, 1) is None

    asyncio.run(scenario())


def test_english_name_can_be_longer_than_russian(tmp_path: Path) -> None:
    """Расширенное название EN не обрезается ограничением RU-колонки."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            translated = replace(_translation(source), recipe_name="A" * 300)
            await orm_save_translations(session, [translated])
            (english,) = await orm_get_localized_recipes(session, "en")
            assert english.recipe_name == "A" * 300

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipe_name", ""),
        ("recipe_name", "A" * 301),
        ("description", ""),
        ("description", " " * 3),
        ("description", "A" * 12001),
        ("description", None),
        ("model", ""),
        ("model", "A" * 101),
    ],
)
def test_invalid_translation_is_rejected_before_writes(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    """Границы хранения совпадают с проверкой ответа переводчика."""

    async def scenario() -> None:
        async with _database(tmp_path) as factory, factory() as session:
            await orm_add_recipe(session, _values())
            (source,) = await orm_list_pending_translations(session)
            invalid = replace(_translation(source), **{field: value})
            with pytest.raises(ValueError):
                await orm_save_translations(session, [invalid])
            assert await session.get(CookbookEnglish, 1) is None

    asyncio.run(scenario())


def test_foreign_key_cascade_is_declared() -> None:
    """Схема PostgreSQL дополнительно запрещает осиротевший перевод."""
    (foreign_key,) = CookbookEnglish.__table__.c.recipe_id.foreign_keys
    assert foreign_key.target_fullname == "cookbook.recipe_id"
    assert foreign_key.ondelete == "CASCADE"
