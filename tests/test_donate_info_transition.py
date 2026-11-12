"""Переход из информации в донаты без сети, счетов и рабочих данных."""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InaccessibleMessage, Message
from aiogram.utils.i18n import I18n

from config_data.config import ROOT
from handlers import donate


@pytest_asyncio.fixture(params=["ru", "en"])
async def ui(request: pytest.FixtureRequest) -> AsyncIterator[tuple[str, FSMContext]]:
    """Использует настоящий каталог и отдельный FSM только в памяти."""
    locale = request.param
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=99, user_id=99))
    await state.set_data(
        {
            "locale": locale,
            "last_message_id": 41,
            "unrelated_setting": "preserved",
        }
    )
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    try:
        with i18n.context(), i18n.use_locale(locale):
            yield locale, state
    finally:
        await storage.close()


@pytest.fixture(autouse=True)
def no_payments(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Любой вызов платёжного хранилища делает проверку неуспешной."""
    calls = {}
    for name in (
        "orm_create_invoice",
        "orm_record_payment",
        "orm_get_invoice",
        "orm_get_payment",
        "orm_mark_refunded",
        "_send_invoice",
    ):
        calls[name] = AsyncMock(side_effect=AssertionError("Счёт не запрашивали"))
        monkeypatch.setattr(donate, name, calls[name])
    return calls


def _callback(chat_type: str = "private") -> Mock:
    """Подменяет только сообщения и подтверждение кнопки Telegram."""
    message = Mock(spec=Message)
    message.message_id = 41
    message.chat = SimpleNamespace(id=99, type=chat_type)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=42))
    message.delete = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.answer_invoice = AsyncMock()
    callback = Mock(spec=CallbackQuery)
    callback.data = "donate"
    callback.message = message
    callback.from_user = SimpleNamespace(id=99)
    callback.bot = SimpleNamespace(delete_message=AsyncMock())
    callback.answer = AsyncMock()
    return callback


async def test_support_replaces_only_clicked_info_card_in_current_locale(
    ui: tuple[str, FSMContext],
    no_payments: dict[str, AsyncMock],
) -> None:
    """Сначала открывается донат, затем удаляется именно карточка с кнопкой."""
    locale, state = ui
    callback = _callback()
    order = []

    async def send(*args: object, **kwargs: object) -> SimpleNamespace:
        """Запоминает показ существующего экрана выбора суммы."""
        order.append("send")
        return SimpleNamespace(message_id=42)

    async def delete() -> None:
        """Запоминает удаление старой карточки после успешного показа."""
        order.append("delete")

    callback.message.answer.side_effect = send
    callback.message.delete.side_effect = delete
    await donate.callback_donate(callback, state)

    assert order == ["send", "delete"]
    callback.message.answer.assert_awaited_once()
    callback.message.delete.assert_awaited_once_with()
    callback.message.edit_reply_markup.assert_not_awaited()
    callback.bot.delete_message.assert_not_awaited()
    callback.answer.assert_awaited_once_with()
    args = callback.message.answer.await_args
    assert args.args == (
        {
            "ru": "Поддержать автора донатом",
            "en": "Support the author with a donation",
        }[locale],
    )
    buttons = args.kwargs["reply_markup"].inline_keyboard
    assert [button.text for row in buttons for button in row] == [
        "10 ⭐️",
        "50 ⭐️",
        "100 ⭐️",
        "другое" if locale == "ru" else "other",
        "Назад на главную ↩️" if locale == "ru" else "Back to main ↩️",
    ]
    assert await state.get_data() == {
        "locale": locale,
        "last_message_id": None,
        "unrelated_setting": "preserved",
    }
    assert await state.get_state() == donate.Donate.donate_input.state
    callback.message.answer_invoice.assert_not_awaited()
    for call in no_payments.values():
        call.assert_not_awaited()


async def test_unrelated_last_message_id_is_never_deleted_or_reassigned(
    ui: tuple[str, FSMContext],
) -> None:
    """Посторонний ID сохраняется, новый донат не становится целью старого меню."""
    locale, state = ui
    await state.update_data(last_message_id=900)
    callback = _callback()
    await donate.callback_donate(callback, state)
    callback.message.delete.assert_awaited_once_with()
    callback.bot.delete_message.assert_not_awaited()
    data = await state.get_data()
    assert data["last_message_id"] == 900
    assert data["locale"] == locale
    assert "donate_message_id" not in data


async def test_new_info_card_created_during_send_keeps_its_pointer(
    ui: tuple[str, FSMContext],
) -> None:
    """При задержке отправки более новая карточка информации не теряет ID."""
    _, state = ui
    callback = _callback()

    async def send(*args: object, **kwargs: object) -> SimpleNamespace:
        """Имитирует появление другой карточки до завершения отправки доната."""
        await state.update_data(last_message_id=900)
        return SimpleNamespace(message_id=42)

    callback.message.answer.side_effect = send
    await donate.callback_donate(callback, state)
    assert (await state.get_data())["last_message_id"] == 900
    callback.bot.delete_message.assert_not_awaited()


async def test_failed_delete_removes_only_old_inline_keyboard(
    ui: tuple[str, FSMContext],
) -> None:
    """Недоступное удаление не оставляет рабочие кнопки старой карточки."""
    _, state = ui
    callback = _callback()
    callback.message.delete.side_effect = TelegramAPIError(Mock(), "cannot delete")
    await donate.callback_donate(callback, state)
    callback.message.answer.assert_awaited_once()
    callback.message.delete.assert_awaited_once_with()
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    callback.answer.assert_awaited_once_with()
    assert (await state.get_data())["last_message_id"] is None


async def test_failed_delete_and_markup_cleanup_do_not_send_donate_again(
    ui: tuple[str, FSMContext],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой очистки не дублирует новый экран и не меняет выбранный язык."""
    locale, state = ui
    callback = _callback()
    callback.message.delete.side_effect = TelegramNetworkError(Mock(), "offline")
    callback.message.edit_reply_markup.side_effect = TelegramAPIError(Mock(), "gone")
    await donate.callback_donate(callback, state)
    callback.message.answer.assert_awaited_once()
    callback.message.delete.assert_awaited_once_with()
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    callback.answer.assert_awaited_once_with()
    assert (await state.get_data())["locale"] == locale
    assert "Не удалось убрать" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        TelegramAPIError(Mock(), "send failed"),
        TelegramNetworkError(Mock(), "unknown delivery"),
        TimeoutError(),
    ],
)
async def test_failed_new_screen_keeps_old_card_and_fsm_without_retry(
    ui: tuple[str, FSMContext],
    error: Exception,
) -> None:
    """При неясной доставке старое меню остаётся, повторной отправки нет."""
    _, state = ui
    before = await state.get_data()
    callback = _callback()
    callback.message.answer.side_effect = error
    await donate.callback_donate(callback, state)
    callback.message.answer.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    callback.message.edit_reply_markup.assert_not_awaited()
    callback.answer.assert_awaited_once_with()
    assert await state.get_data() == before
    assert await state.get_state() is None


async def test_cancelled_send_keeps_old_card_and_does_not_retry(
    ui: tuple[str, FSMContext],
) -> None:
    """Остановка задачи не удаляет старую карточку и не повторяет запрос."""
    _, state = ui
    callback = _callback()
    callback.message.answer.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await donate.callback_donate(callback, state)
    callback.message.answer.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    assert (await state.get_data())["last_message_id"] == 41
    assert await state.get_state() is None


@pytest.mark.parametrize("missing", [None, Mock(spec=InaccessibleMessage)])
async def test_unavailable_callback_only_acknowledges(
    ui: tuple[str, FSMContext],
    missing: object,
) -> None:
    """Callback без доступного сообщения не меняет состояние или язык."""
    _, state = ui
    before = await state.get_data()
    callback = _callback()
    callback.message = missing
    await donate.callback_donate(callback, state)
    callback.answer.assert_awaited_once_with()
    assert await state.get_data() == before
    assert await state.get_state() is None


async def test_group_callback_is_rejected_by_actual_router(
    ui: tuple[str, FSMContext],
) -> None:
    """Открытие доната по этой кнопке разрешено только в личном чате."""
    _, state = ui
    callback = _callback("group")
    await donate.donate_router.propagate_event(
        update_type="callback_query",
        event=callback,
        state=state,
    )
    callback.message.answer.assert_not_awaited()
    callback.message.delete.assert_not_awaited()
    callback.answer.assert_not_awaited()
    assert await state.get_state() is None
