"""Глобальная навигация не перехватывается состояниями ввода."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import navigation


@pytest.mark.parametrize("name", ["open_main", "open_mini", "open_info"])
async def test_navigation_clears_transient_state_preserves_language(
    monkeypatch,
    name,
):
    """Любая глобальная команда завершает предыдущий диалог."""
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "locale": "en",
                "recipe_edit_id": 7,
                "currency_amount": "10",
            }
        ),
        set_state=AsyncMock(),
        set_data=AsyncMock(),
    )
    target = {
        "open_main": (navigation.private, "cancel_cmd"),
        "open_mini": (navigation.miniapp, "cmd_miniapp"),
        "open_info": (navigation.other, "process_help_command"),
    }[name]
    callback = AsyncMock()
    monkeypatch.setattr(target[0], target[1], callback)
    arguments = [SimpleNamespace(), state]
    if name == "open_info":
        arguments.append({})
    await getattr(navigation, name)(*arguments)
    state.set_state.assert_awaited_once_with(None)
    state.set_data.assert_awaited_once_with({"locale": "en"})
    callback.assert_awaited_once()
