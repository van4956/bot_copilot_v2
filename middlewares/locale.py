"""Восстановление языка пользователя для всех типов событий."""

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from common.locales import normalize_locale
from database.orm_users import SUPPORTED_LOCALES, orm_get_locale

logger = logging.getLogger(__name__)


async def get_user_id(event: TelegramObject) -> int | None:
    """Извлекает пользователя из сообщения, callback и прочих апдейтов."""
    if isinstance(event, Update):
        for field in type(event).model_fields:
            nested = getattr(event, field, None)
            if isinstance(nested, TelegramObject):
                user_id = await get_user_id(nested)
                if user_id is not None:
                    return user_id
        return None
    actor = getattr(event, "from_user", None) or getattr(event, "user", None)
    return actor.id if actor is not None else None


class LocaleFromDBMiddleware(BaseMiddleware):
    """Подготавливает локаль, не перехватывая ошибки обработчика."""

    def __init__(self, workflow_data: dict[str, Any]) -> None:
        """Сохраняет общие зависимости диспетчера."""
        super().__init__()
        self.workflow_data = workflow_data

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Восстанавливает язык в FSM и один раз передаёт событие дальше."""
        data.setdefault("workflow_data", self.workflow_data)
        try:
            state = data.get("state")
            session = data.get("session")
            if state is not None:
                state_data = await state.get_data()
                cached_locale = state_data.get("locale")
                if cached_locale not in SUPPORTED_LOCALES:
                    actor = data.get("event_from_user") or getattr(
                        event, "from_user", None,
                    )
                    fallback = (
                        cached_locale if cached_locale is not None
                        else getattr(actor, "language_code", None)
                    )
                    await state.update_data(locale=normalize_locale(fallback))
                    user_id = await get_user_id(event)
                    if actor is not None:
                        user_id = actor.id
                    if user_id is not None and session is not None:
                        user_locale = await orm_get_locale(session, user_id)
                        if user_locale:
                            await state.update_data(locale=user_locale)
        except Exception:
            logger.exception("Не удалось восстановить язык пользователя")
        # Исключение downstream не должно повторять его побочные эффекты.
        return await handler(event, data)
