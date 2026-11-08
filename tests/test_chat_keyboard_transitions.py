"""Переходы внутри информации не удаляют нижнюю клавиатуру повторно."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import ForceReply, ReplyKeyboardRemove

from common import keyboard
from handlers import other, private


@pytest.mark.parametrize("target", ["language", "author"])
async def test_info_submenu_does_not_repeat_keyboard_removal(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Повторный переход не просит Telegram переключаться на буквенный ввод."""
    for module in (other, private):
        monkeypatch.setattr(module, "_", lambda text: text)
    message = SimpleNamespace(
        delete=AsyncMock(),
        answer=AsyncMock(),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=3)),
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        message=message,
        bot=SimpleNamespace(delete_message=AsyncMock()),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"last_message_id": 2}),
        update_data=AsyncMock(),
    )
    if target == "language":
        await other.locale_cmd(callback, state)
    else:
        await private.author_cmd(callback, {}, state)
    calls = [*message.answer.await_args_list, *message.answer_photo.await_args_list]
    assert calls
    for call in calls:
        assert not isinstance(
            call.kwargs.get("reply_markup"), (ReplyKeyboardRemove, ForceReply)
        )


def test_main_keyboard_does_not_request_one_time_input() -> None:
    """Главное меню не запрашивает принудительный ответ или одноразовую клавиатуру."""
    markup = keyboard.get_keyboard("Погода", "Игры")
    payload = markup.model_dump(exclude_none=True)
    assert not payload.get("force_reply")
    assert not payload.get("one_time_keyboard")
