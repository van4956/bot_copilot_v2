"""Ограничение частоты обычных действий без потери платежей."""

from collections import OrderedDict
from time import monotonic
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import (
    Message,
    PreCheckoutQuery,
    ShippingQuery,
    TelegramObject,
)


def _is_critical_event(event: TelegramObject) -> bool:
    """Отличает платежи и результаты игр, которые нельзя отбрасывать."""
    if isinstance(event, (PreCheckoutQuery, ShippingQuery)):
        return True
    if getattr(event, "pre_checkout_query", None) is not None:
        return True
    if getattr(event, "shipping_query", None) is not None:
        return True
    message = getattr(event, "message", None)
    if isinstance(event, Message):
        message = event
    return message is not None and any(
        getattr(message, field, None) is not None
        for field in ("successful_payment", "refunded_payment", "web_app_data")
    )


class ThrottleMiddleware(BaseMiddleware):
    """Ограничивает повторы, вызывая следующий обработчик не более раза."""

    def __init__(self, ttl: float = 0.5, max_users: int = 10_000) -> None:
        """Настраивает окно и ограниченный размер локального кэша."""
        super().__init__()
        self.ttl = ttl
        self.max_users = max_users
        self._seen: OrderedDict[int, float] = OrderedDict()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Пропускает платежи и не повторяет упавший обработчик."""
        if _is_critical_event(event):
            return await handler(event, data)
        actor = data.get("event_from_user") or getattr(event, "from_user", None)
        if actor is None:
            for field in ("message", "callback_query"):
                actor = getattr(getattr(event, field, None), "from_user", None)
                if actor is not None:
                    break
        if actor is not None:
            now = monotonic()
            while self._seen:
                first_user, first_time = next(iter(self._seen.items()))
                if now - first_time < self.ttl:
                    break
                self._seen.pop(first_user)
            if actor.id in self._seen:
                return None
            self._seen[actor.id] = now
            while len(self._seen) > self.max_users:
                self._seen.popitem(last=False)
        return await handler(event, data)
