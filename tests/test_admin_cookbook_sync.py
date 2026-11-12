"""Русская кнопка синхронизации без сети, секретов и настоящей базы."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, Update

from common.cookbook_sync import CookbookSyncError, SyncResult
from handlers import admin


@pytest.mark.parametrize(
    "result, expected",
    [
        (SyncResult(total=0, updated=0), "В книге пока нет рецептов."),
        (
            SyncResult(total=3, updated=0, skipped=3),
            "Все рецепты уже синхронизированы.",
        ),
        (
            SyncResult(total=5, updated=2, skipped=1, changed=1, deleted=1),
            "Синхронизация завершена. Обновлено рецептов: 2.\n"
            "Без изменений: 1.\n"
            "Изменены во время синхронизации: 1. "
            "Нажмите «Синхронизировать 🔄» ещё раз.\n"
            "Удалены во время синхронизации: 1.",
        ),
    ],
)
async def test_sync_edits_one_progress_message(
    monkeypatch: pytest.MonkeyPatch, result: SyncResult, expected: str,
) -> None:
    """Результат заменяет прогресс, не присылая новые сообщения и клавиатуры."""
    sync = AsyncMock(return_value=result)
    monkeypatch.setattr(admin, "sync_cookbook", sync)
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock(return_value=progress))
    http_session, session = object(), object()
    bot = SimpleNamespace(api_gpt="offline-test-key", http_session=http_session)

    await admin.synchronize_cookbook(message, bot, session)

    message.answer.assert_awaited_once_with("Синхронизирую рецепты…")
    progress.edit_text.assert_awaited_once_with(expected)
    sync.assert_awaited_once_with(
        session, api_key="offline-test-key", http_session=http_session,
    )


@pytest.mark.parametrize(
    "reason", ["already_running", "missing_api_key", "translation_failed", "storage_failed"],
)
async def test_sync_errors_replace_progress_with_safe_russian_answer(
    monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    """Понятные ошибки не раскрывают ключ и не создают второе сообщение."""
    error = CookbookSyncError(reason)
    sync = AsyncMock(side_effect=error)
    monkeypatch.setattr(admin, "sync_cookbook", sync)
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock(return_value=progress))

    await admin.synchronize_cookbook(
        message, SimpleNamespace(api_gpt="offline-test-key", http_session=None), object(),
    )

    assert "offline-test-key" not in progress.edit_text.await_args.args[0]
    message.answer.assert_awaited_once()
    progress.edit_text.assert_awaited_once_with(str(error))


async def test_no_key_does_not_prevent_already_synced_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверка отсутствующего ключа остаётся у координатора после поиска изменений."""
    sync = AsyncMock(return_value=SyncResult(total=2, updated=0, skipped=2))
    monkeypatch.setattr(admin, "sync_cookbook", sync)
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock(return_value=progress))
    session = object()

    await admin.synchronize_cookbook(message, SimpleNamespace(), session)

    sync.assert_awaited_once_with(session, api_key="", http_session=None)
    progress.edit_text.assert_awaited_once_with("Все рецепты уже синхронизированы.")


@pytest.mark.parametrize(
    "user_id, chat_type, editor, allowed",
    [(1, "private", False, True), (2, "private", False, False),
     (1, "group", False, False), (1, "private", True, False)],
)
async def test_sync_reply_button_requires_private_admin_without_editor(
    monkeypatch: pytest.MonkeyPatch,
    user_id: int, chat_type: str, editor: bool, allowed: bool,
) -> None:
    """Реальный роутер сохраняет ограничения доступа и состояния редактора."""
    bot = Bot("123456:OFFLINE_ADMIN_SYNC_ONLY")
    bot.admin_list = [1]
    dispatcher = Dispatcher()
    dispatcher.include_router(admin.admin_router)
    sync = AsyncMock(return_value=SyncResult(total=0, updated=0))
    monkeypatch.setattr(admin, "sync_cookbook", sync)
    progress = Mock(spec=Message)
    progress.edit_text = AsyncMock()
    monkeypatch.setattr(Bot, "__call__", AsyncMock(return_value=progress))
    state: FSMContext = dispatcher.fsm.get_context(
        bot=bot, chat_id=user_id, user_id=user_id,
    )
    if editor:
        await state.set_state(admin.AddProduct.author)
    try:
        update = Update.model_validate({
            "update_id": 1,
            "message": {
                "message_id": 1, "date": datetime.now(timezone.utc),
                "chat": {"id": user_id, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
                "text": admin.SYNC_COOKBOOK_BUTTON,
            },
        })
        await dispatcher.feed_update(bot, update, session=object())
        assert sync.await_count == int(allowed)
    finally:
        admin.admin_router._parent_router = None
        dispatcher.sub_routers.clear()
        await dispatcher.storage.close()
        await bot.session.close()
