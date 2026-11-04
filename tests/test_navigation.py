"""Глобальная навигация не перехватывается состояниями ввода."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import navigation


@pytest.mark.parametrize("name", ["open_main", "open_book", "open_info"])
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
        update_data=AsyncMock(),
    )
    target = {
        "open_main": (navigation.private, "cancel_cmd"),
        "open_book": (navigation.cookbook, "process_cookbook_command"),
        "open_info": (navigation.other, "process_help_command"),
    }[name]
    callback = AsyncMock()
    monkeypatch.setattr(target[0], target[1], callback)
    arguments = [SimpleNamespace(), state]
    if name in {"open_info", "open_book"}:
        arguments.append({})
    await getattr(navigation, name)(*arguments)
    state.set_state.assert_awaited_once_with(None)
    state.set_data.assert_awaited_once_with({"locale": "en"})
    callback.assert_awaited_once()


async def test_book_keeps_page_when_exiting_another_dialog(monkeypatch) -> None:
    """Новая команда использует прежнюю книгу и не теряет выбранную страницу."""
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"locale": "ru", "page": 3}),
        set_state=AsyncMock(),
        set_data=AsyncMock(),
        update_data=AsyncMock(),
    )
    handler = AsyncMock()
    monkeypatch.setattr(navigation.cookbook, "process_cookbook_command", handler)
    message, session = object(), object()

    await navigation.open_book(message, state, session)

    state.set_state.assert_awaited_once_with(None)
    state.set_data.assert_awaited_once_with({"locale": "ru"})
    state.update_data.assert_awaited_once_with(page=3)
    handler.assert_awaited_once_with(message, state, session)
