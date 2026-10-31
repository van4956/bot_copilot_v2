"""Служебные результаты игр не теряются в других диалогах."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Message, Update

from handlers import miniapp
from middlewares.throttle import ThrottleMiddleware


async def test_webapp_results_bypass_throttle():
    """Игра сохраняется даже сразу после предыдущего действия."""
    middleware = ThrottleMiddleware(ttl=60)
    actor = SimpleNamespace(id=9000000001)
    ordinary = SimpleNamespace(
        from_user=actor,
        successful_payment=None,
        refunded_payment=None,
        web_app_data=None,
    )
    result = SimpleNamespace(
        from_user=actor,
        successful_payment=None,
        refunded_payment=None,
        web_app_data=object(),
    )
    handler = AsyncMock()
    await middleware(handler, ordinary, {"event_from_user": actor})
    await middleware(
        handler, SimpleNamespace(message=result), {"event_from_user": actor}
    )
    assert handler.await_count == 2


async def test_webapp_result_router_has_no_state_filter(monkeypatch):
    """Результат приходит и при незавершённом вводе валюты."""
    save = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(miniapp, "orm_add_game", save)
    monkeypatch.setattr(miniapp, "_", lambda value: value)
    monkeypatch.setattr(Message, "answer", answer)
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": datetime.now(timezone.utc),
                "chat": {"id": 9000000001, "type": "private"},
                "from": {
                    "id": 9000000001,
                    "is_bot": False,
                    "first_name": "Synthetic",
                },
                "web_app_data": {
                    "data": '{"action":"game_end","game":"snake","score":10}',
                    "button_text": "Snake",
                },
            },
        }
    )
    await miniapp.webapp_data_router.propagate_event(
        "message",
        update.message,
        session=object(),
        workflow_data={},
        raw_state="Currency:amount",
    )
    save.assert_awaited_once()
    answer.assert_awaited_once()
