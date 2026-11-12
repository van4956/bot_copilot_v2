"""Ручная синхронизация английских рецептов без изменения оригиналов."""

import asyncio
import logging
from dataclasses import dataclass

from aiohttp import ClientSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.cookbook_translation import MODEL, TranslationError, translate_recipe
from database.orm_cookbook import orm_get_recipes
from database.orm_cookbook_translations import (
    RecipeTranslation,
    orm_list_pending_translations,
    orm_save_translations,
)

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_ERROR_MESSAGES = {
    "already_running": "Синхронизация уже выполняется. Дождитесь её завершения.",
    "missing_api_key": "Для перевода не настроен ключ OpenAI.",
    "authentication_failed": (
        "OpenAI не разрешил перевод. Проверьте ключ и доступ к gpt-5.4-mini. "
        "Прежние переводы сохранены."
    ),
    "rate_limited": (
        "Достигнут лимит OpenAI. Проверьте баланс и повторите позже. "
        "Прежние переводы сохранены."
    ),
    "translation_failed": (
        "Не удалось перевести книгу. Прежние переводы сохранены. "
        "Попробуйте синхронизировать позже."
    ),
    "storage_failed": "Не удалось сохранить переводы. Попробуйте позже.",
}


class CookbookSyncError(RuntimeError):
    """Содержит только безопасную причину ошибки синхронизации."""

    def __init__(self, reason: str) -> None:
        """Не включает ключ, ответы API и содержимое рецептов в ошибку."""
        self.reason = reason
        super().__init__(_ERROR_MESSAGES[reason])


@dataclass(frozen=True)
class SyncResult:
    """Сводка публикации без секретов и текстов рецептов."""

    total: int
    updated: int
    skipped: int = 0
    changed: int = 0
    deleted: int = 0


async def sync_cookbook(
    session: AsyncSession,
    *,
    api_key: str,
    http_session: ClientSession | None,
) -> SyncResult:
    """Переводит изменённые тексты вне транзакции и публикует одним пакетом."""
    if _sync_lock.locked():
        raise CookbookSyncError("already_running")
    async with _sync_lock:
        try:
            total = len(await orm_get_recipes(session))
            pending = await orm_list_pending_translations(session)
            # Снимки обычные dataclass: соединение не удерживается во время API.
            await session.rollback()
            skipped = total - len(pending)
            if not pending:
                return SyncResult(total=total, updated=0, skipped=skipped)
            if not isinstance(api_key, str) or not api_key.strip():
                raise CookbookSyncError("missing_api_key")
            if http_session is None or http_session.closed:
                raise CookbookSyncError("translation_failed")
            translations = []
            for source in pending:
                translated = await translate_recipe(
                    source.recipe_name,
                    source.description,
                    api_key=api_key,
                    http_session=http_session,
                )
                translations.append(
                    RecipeTranslation(
                        source=source,
                        recipe_name=translated.recipe_name,
                        description=translated.description,
                        model=MODEL,
                    )
                )
            result = await orm_save_translations(session, translations)
            return SyncResult(
                total=total,
                updated=result.saved,
                skipped=skipped,
                changed=result.changed,
                deleted=result.deleted,
            )
        except TranslationError as error:
            logger.warning("Не удалось получить английские переводы рецептов")
            reason = getattr(error, "reason", "translation_failed")
            if reason not in {"authentication_failed", "rate_limited"}:
                reason = "translation_failed"
            raise CookbookSyncError(reason) from None
        except SQLAlchemyError:
            await session.rollback()
            logger.warning("Не удалось сохранить английские переводы рецептов")
            raise CookbookSyncError("storage_failed") from None
