"""Проверки ручной синхронизации без OpenAI, Telegram и рабочей БД."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from common import cookbook_sync
from common.cookbook_translation import RecipeTranslation, TranslationError
from database.models import Base
from database.orm_cookbook import orm_add_recipe, orm_get_recipes, orm_update_recipe
from database.orm_cookbook_translations import orm_get_localized_recipes


def recipe_values(number: int, description: str = "") -> dict[str, Any]:
    """Создаёт короткий русский рецепт с общими метаданными."""
    return {
        "recipe_name": f"Рецепт {number}",
        "author": "Иван",
        "price": 1,
        "description": description or f"Шаг {number}: смешать 100 г муки.",
        "image": "test-fixture:1",
    }


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Выделяет отдельную временную SQLite для одного теста."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'book.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def translator(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Заменяет переводчик заглушкой, не допускающей платных запросов."""
    fake = AsyncMock(
        side_effect=lambda name, description, **kwargs: RecipeTranslation(
            f"EN {name}", f"EN {description}"
        )
    )
    monkeypatch.setattr(cookbook_sync, "translate_recipe", fake)
    return fake


async def synchronize(session: AsyncSession) -> cookbook_sync.SyncResult:
    """Вызывает сервис с фиктивным ключом и HTTP-сеансом."""
    return await cookbook_sync.sync_cookbook(
        session,
        api_key="offline-only-key",
        http_session=SimpleNamespace(closed=False),
    )


async def test_empty_and_unchanged_books_do_not_need_a_key(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
) -> None:
    """Пустая и уже переведённая книги не вызывают API."""
    async with sessions() as session:
        result = await cookbook_sync.sync_cookbook(
            session,
            api_key="",
            http_session=None,
        )
        assert result == cookbook_sync.SyncResult(total=0, updated=0)
        await orm_add_recipe(session, recipe_values(1))
        assert (await synchronize(session)).updated == 1
        result = await cookbook_sync.sync_cookbook(
            session,
            api_key="",
            http_session=None,
        )
        assert result == cookbook_sync.SyncResult(total=1, updated=0, skipped=1)
    assert translator.await_count == 1


async def test_missing_key_does_not_attempt_translation(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
) -> None:
    """При отсутствии ключа русский оригинал остаётся доступным."""
    async with sessions() as session:
        await orm_add_recipe(session, recipe_values(1))
        with pytest.raises(cookbook_sync.CookbookSyncError) as error:
            await cookbook_sync.sync_cookbook(session, api_key="", http_session=None)
        assert error.value.reason == "missing_api_key"
        assert (await orm_get_localized_recipes(session, "en"))[
            0
        ].recipe_name == "Рецепт 1"
    translator.assert_not_awaited()


async def test_publishes_english_without_changing_originals(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
) -> None:
    """Успех публикует пакет, повторный запуск пропускает те же тексты."""
    async with sessions() as session:
        for number in (1, 2):
            await orm_add_recipe(session, recipe_values(number))
        result = await synchronize(session)
        assert (result.total, result.updated, result.skipped) == (2, 2, 0)
        english = await orm_get_localized_recipes(session, "en")
        originals = await orm_get_recipes(session)
        assert [row.recipe_name for row in english] == ["EN Рецепт 1", "EN Рецепт 2"]
        assert [row.recipe_name for row in originals] == ["Рецепт 1", "Рецепт 2"]
        assert all(
            row.author == "Иван" and row.image == "test-fixture:1" for row in english
        )
        result = await synchronize(session)
        assert result.updated == 0 and result.skipped == 2
    assert translator.await_count == 2


async def test_second_translation_failure_keeps_entire_previous_english_book(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
) -> None:
    """Неудачный пакет не публикуется частично и не затирает старый EN."""
    async with sessions() as session:
        for number in (1, 2):
            await orm_add_recipe(session, recipe_values(number))
        await synchronize(session)
        for row in await orm_get_recipes(session):
            await orm_update_recipe(
                session,
                row.recipe_id,
                recipe_values(
                    row.recipe_id,
                    "Новый текст: добавить 200 г муки.",
                ),
            )
        translator.side_effect = [
            RecipeTranslation("New recipe", "New description"),
            TranslationError("offline failure"),
        ]
        with pytest.raises(cookbook_sync.CookbookSyncError) as error:
            await synchronize(session)
        assert error.value.reason == "translation_failed"
        english = await orm_get_localized_recipes(session, "en")
        assert [row.recipe_name for row in english] == ["EN Рецепт 1", "EN Рецепт 2"]
        assert all("100" in row.description for row in english)
        assert all("200" in row.description for row in await orm_get_recipes(session))


async def test_original_change_during_translation_is_not_overwritten(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
) -> None:
    """Повторная проверка перед публикацией замечает новое редактирование RU."""
    async with sessions() as session:
        row = await orm_add_recipe(session, recipe_values(1))
        identifier = row.recipe_id

        async def edit_while_translating(
            *args: Any, **kwargs: Any
        ) -> RecipeTranslation:
            """Меняет исходник другой сессией пока перевод ожидает ответ."""
            async with sessions() as editor:
                await orm_update_recipe(
                    editor, identifier, recipe_values(1, "Новые 300 г муки.")
                )
            return RecipeTranslation("Old translation", "Old 100 g flour.")

        translator.side_effect = edit_while_translating
        result = await synchronize(session)
        assert result.updated == 0 and result.changed == 1
        english = await orm_get_localized_recipes(session, "en")
        assert english[0].description == "Новые 300 г муки."


async def test_parallel_sync_is_rejected_and_cancellation_releases_lock(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
) -> None:
    """Двойное нажатие не удваивает запросы, отмена освобождает синхронизацию."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_translate(*args: Any, **kwargs: Any) -> RecipeTranslation:
        """Управляемо останавливает фиктивный ответ API."""
        started.set()
        await release.wait()
        return RecipeTranslation("Recipe", "Mix flour.")

    translator.side_effect = slow_translate
    async with sessions() as first, sessions() as second:
        await orm_add_recipe(first, recipe_values(1))
        task = asyncio.create_task(synchronize(first))
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            with pytest.raises(cookbook_sync.CookbookSyncError) as error:
                await synchronize(second)
            assert error.value.reason == "already_running"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        assert (await synchronize(second)).updated == 1
    assert translator.await_count == 2


async def test_storage_error_is_safe_and_does_not_call_api(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибки БД не выдают технические детали и освобождают блокировку."""
    from sqlalchemy.exc import SQLAlchemyError

    monkeypatch.setattr(
        cookbook_sync,
        "orm_get_recipes",
        AsyncMock(
            side_effect=SQLAlchemyError("secret-database-connection"),
        ),
    )
    async with sessions() as session:
        with pytest.raises(cookbook_sync.CookbookSyncError) as error:
            await synchronize(session)
    assert error.value.reason == "storage_failed"
    assert "secret" not in str(error.value)
    translator.assert_not_awaited()


@pytest.mark.parametrize(
    "reason,expected,visible",
    [
        ("authentication_failed", "authentication_failed", "Проверьте ключ"),
        ("rate_limited", "rate_limited", "Проверьте баланс"),
        (
            "unknown-provider-reason",
            "translation_failed",
            "Попробуйте синхронизировать позже",
        ),
    ],
)
async def test_translation_reason_has_safe_actionable_message_and_preserves_english(
    sessions: async_sessionmaker[AsyncSession],
    translator: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    reason: str,
    expected: str,
    visible: str,
) -> None:
    """Причина помогает исправить ключ или баланс, не раскрывая ответы API."""
    secret = "sk-private-key-and-provider-response"
    async with sessions() as session:
        await orm_add_recipe(session, recipe_values(1))
        await synchronize(session)
        await orm_update_recipe(session, 1, recipe_values(1, "Теперь 200 г муки."))
        translator.side_effect = TranslationError(secret, reason=reason)
        with pytest.raises(cookbook_sync.CookbookSyncError) as raised:
            await synchronize(session)
        assert raised.value.reason == expected
        assert visible in str(raised.value)
        assert secret not in str(raised.value)
        assert secret not in repr(raised.value)
        assert secret not in caplog.text
        (english,) = await orm_get_localized_recipes(session, "en")
        assert english.recipe_name == "EN Рецепт 1"
        assert "100" in english.description and "200" not in english.description
