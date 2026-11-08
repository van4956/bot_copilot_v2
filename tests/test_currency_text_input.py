"""Проверки общего текстового и inline-ввода суммы без внешних запросов."""

from decimal import Decimal
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageReplyMarkup

from handlers import currency


@pytest.fixture(autouse=True)
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Изолирует проверку ввода от контекста переводов."""
    monkeypatch.setattr(currency, "_", lambda text: text)
    monkeypatch.setattr(currency.keyboard, "_", lambda text: text)
    monkeypatch.setattr(currency, "_next_keyboard", lambda: None)


@pytest_asyncio.fixture
async def state() -> AsyncIterator[FSMContext]:
    """Создаёт отдельный диалог ввода суммы в памяти."""
    storage = MemoryStorage()
    context = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=10, user_id=10),
    )
    await context.set_state(currency.Currency.amount)
    await context.update_data(
        locale="en", user_value="123", currency_keypad_message_id=100
    )
    yield context
    await storage.close()


def make_message(
    text: str | None = None, message_id: int = 200
) -> SimpleNamespace:
    """Заменяет все операции Telegram асинхронными заглушками."""
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(), api_currency="test-key"
    )
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        chat=SimpleNamespace(id=10, type="private"),
        bot=bot,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=201)),
        edit_text=AsyncMock(),
    )


def make_callback(data: str, message_id: int = 100) -> SimpleNamespace:
    """Создаёт нажатие клавиатуры без привязки к настоящему боту."""
    message = make_message(message_id=message_id)
    return SimpleNamespace(
        data=data, message=message, bot=message.bot, answer=AsyncMock()
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", "0"),
        ("0.0", "0.0"),
        ("12.50", "12.50"),
        (" 12.50 ", "12.50"),
        (".5", "0.5"),
        ("5.", "5"),
        ("000000000000000000", "0"),
        ("123456789012345678", "123456789012345678"),
        ("0.0000000000000001", "0.0000000000000001"),
    ],
)
def test_parse_amount_valid(text: str, expected: str) -> None:
    """Допускает только конечные суммы установленного формата."""
    assert currency.parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        ".",
        "words",
        "12USD",
        "1..2",
        "1,2",
        "1 000",
        "1_000",
        "1/2",
        "1e3",
        "1E+3",
        "+1",
        "-1",
        "-0",
        "NaN",
        "nan",
        "Inf",
        "Infinity",
        "0x10",
        "²",
        "١",
        "１２",
        "1\t2",
        "1\n2",
        "1234567890123456789",
        "123456789012345678.",
    ],
)
def test_parse_amount_rejects_invalid_format(text: str) -> None:
    """Не допускает слова, специальные числа, экспоненты и разделители."""
    with pytest.raises(ValueError):
        currency.parse_amount(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", [None, "", "USD 10", "-5", "NaN", "1e2", "1,5", "9" * 19]
)
async def test_invalid_message_preserves_dialog(
    text: str | None, state: FSMContext
) -> None:
    """Нетекстовый или неверный ввод не сбрасывает состояние и черновик."""
    before = await state.get_data()
    message = make_message(text)
    await currency.process_amout_(message, state)
    assert await state.get_state() == currency.Currency.amount.state
    assert await state.get_data() == before
    message.answer.assert_awaited_once()
    message.bot.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_confirms_amount_and_removes_old_keypad(
    state: FSMContext,
) -> None:
    """Текст заменяет черновик и ведёт к базовой валюте как inline Enter."""
    message = make_message(" 12.50 ")
    await currency.process_amout_(message, state)
    data = await state.get_data()
    assert data["currency_amount"] == "12.50"
    assert data["user_value"] == "12.50"
    assert data["locale"] == "en"
    assert await state.get_state() == currency.Currency.base_currency.state
    message.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=10, message_id=100, reply_markup=None
    )
    message.answer.assert_awaited_once_with(
        "Сумма операции: 12.50\nБазовая валюта: ",
        reply_markup=currency.markup_cur,
    )


@pytest.mark.asyncio
async def test_old_keypad_cleanup_failure_does_not_block_text(
    state: FSMContext,
) -> None:
    """Неудаляемая старая клавиатура не блокирует корректный текст суммы."""
    message = make_message("10")
    message.bot.edit_message_reply_markup.side_effect = TelegramBadRequest(
        method=EditMessageReplyMarkup(chat_id=10, message_id=100),
        message="message cannot be edited",
    )
    await currency.process_amout_(message, state)
    assert await state.get_state() == currency.Currency.base_currency.state
    assert (await state.get_data())["currency_amount"] == "10"
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_enter_retains_existing_contract(
    state: FSMContext,
) -> None:
    """Enter подтверждает цифровой черновик и заменяет клавиши валютами."""
    await state.update_data(user_value="12.50")
    callback = make_callback("num_enter")
    await currency.process_amout(callback, state)
    assert await state.get_state() == currency.Currency.base_currency.state
    assert (await state.get_data())["currency_amount"] == "12.50"
    callback.message.edit_text.assert_awaited_once_with(
        "Сумма операции: 12.50\nБазовая валюта: ",
        reply_markup=currency.markup_cur,
    )
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "NaN", "1e2", "-1", "9" * 19])
async def test_invalid_inline_enter_uses_same_validation(
    value: str, state: FSMContext
) -> None:
    """Inline Enter также не принимает некорректное значение из FSM."""
    await state.update_data(user_value=value)
    before = await state.get_data()
    callback = make_callback("num_enter")
    await currency.process_amout(callback, state)
    assert await state.get_state() == currency.Currency.amount.state
    assert await state.get_data() == before
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["num_1", "num_000", "num_com"])
async def test_inline_length_limit_includes_decimal_point(
    action: str, state: FSMContext
) -> None:
    """Ни цифры, ни точка не увеличивают черновик сверх 18 символов."""
    await state.update_data(user_value="123456789012345678")
    before = await state.get_data()
    callback = make_callback(action)
    await currency.process_amout(callback, state)
    assert await state.get_data() == before
    assert await state.get_state() == currency.Currency.amount.state
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once_with("Слишком длинная сумма")


@pytest.mark.asyncio
async def test_inline_digits_point_delete_and_enter(state: FSMContext) -> None:
    """Все прежние действия цифровой клавиатуры сохраняют смысл."""
    await state.update_data(user_value="")
    for action in ("num_com", "num_5", "num_000", "num_del"):
        await currency.process_amout(make_callback(action), state)
    assert (await state.get_data())["user_value"] == "0.500"
    await currency.process_amout(make_callback("num_enter"), state)
    assert (await state.get_data())["currency_amount"] == "0.500"
    assert await state.get_state() == currency.Currency.base_currency.state


@pytest.mark.asyncio
async def test_stale_numeric_callback_after_text_does_not_change_state(
    state: FSMContext,
) -> None:
    """После текстового Enter старые цифровые кнопки только получают ответ."""
    await currency.process_amout_(make_message("10"), state)
    before = await state.get_data()
    callback = make_callback("num_9")
    await currency.currency_router.propagate_event(
        "callback_query",
        callback,
        state=state,
        raw_state=currency.Currency.base_currency.state,
    )
    assert await state.get_data() == before
    assert await state.get_state() == currency.Currency.base_currency.state
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Эта клавиатура устарела. Используйте текущую."
    )


@pytest.mark.asyncio
async def test_repeat_rejects_old_keypad_but_accepts_current(
    state: FSMContext,
) -> None:
    """Повторный расчёт привязан только к новому цифровому сообщению."""
    await state.set_state(None)
    message = make_message()
    message.answer.return_value = SimpleNamespace(message_id=300)
    await currency.process_reopen(message, state, {})
    assert (await state.get_data())["currency_keypad_message_id"] == 300
    assert message.answer.await_args.args[0] == "Сумма операции: 0.0"
    old = make_callback("num_9", message_id=100)
    await currency.process_amout(old, state)
    assert (await state.get_data())["user_value"] == ""
    old.message.edit_text.assert_not_awaited()
    current = make_callback("num_9", message_id=300)
    await currency.process_amout(current, state)
    assert (await state.get_data())["user_value"] == "9"
    current.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_then_currency_selection_completes_existing_calculation(
    state: FSMContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сервис вызывается только после суммы и выбора обеих валют."""
    rate = AsyncMock(return_value=("12:00", "01-01-2026", Decimal("0.9")))
    monkeypatch.setattr(currency, "get_currency_rate", rate)
    await currency.process_amout_(make_message("12.50"), state)
    rate.assert_not_awaited()
    await currency.process_base_currency(make_callback("USD", 201), state)
    assert await state.get_state() == currency.Currency.target_currency.state
    rate.assert_not_awaited()
    target = make_callback("EUR", 201)
    await currency.process_target_currency(target, state)
    rate.assert_awaited_once()
    assert "11.25 EUR" in target.message.edit_text.await_args.args[0]
    assert await state.get_state() is None
    assert (await state.get_data())["locale"] == "en"
