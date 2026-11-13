"""Изолированные проверки платежей, языка и middleware без Telegram."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import RefundStarPayment
from aiogram.types import (
    CallbackQuery,
    Message,
    PreCheckoutQuery,
    Update,
    User,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from database.models import Base, Games, Payments
from database.orm_payments import (
    orm_create_invoice,
    orm_get_payment,
    orm_record_payment,
)
from handlers import donate
from middlewares import locale, throttle

USER_ID = 10


@pytest.fixture(autouse=True)
def isolate_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает транспорт Telegram и изолирует gettext."""
    monkeypatch.setattr(
        Bot,
        "__call__",
        AsyncMock(side_effect=AssertionError("Telegram запрещён")),
    )
    monkeypatch.setattr(donate, "_", lambda text: text)
    monkeypatch.setattr(donate.keyboard, "start_keyboard", lambda bot=None: None)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """Создаёт отдельную временную SQLite и закрывает все соединения."""
    database_path = (tmp_path / "payments.sqlite3").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def state() -> AsyncIterator[FSMContext]:
    """Создаёт FSM пользователя без Redis."""
    storage = MemoryStorage()
    context = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    yield context
    await storage.close()


def make_message(**fields: object) -> Message:
    """Создаёт настоящий объект aiogram с синтетическими данными."""
    data = {
        "message_id": 1,
        "date": datetime.now(timezone.utc),
        "chat": {"id": USER_ID, "type": "private"},
        "from_user": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
        **fields,
    }
    return Message.model_validate(data)


def make_checkout(payload: str, **fields: object) -> PreCheckoutQuery:
    """Создаёт запрос оплаты без обращения к Telegram."""
    return PreCheckoutQuery.model_validate(
        {
            "id": "checkout",
            "from_user": {
                "id": USER_ID, "is_bot": False, "first_name": "Test"
            },
            "currency": "XTR",
            "total_amount": 10,
            "invoice_payload": payload,
            **fields,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("middleware_name", ["throttle", "locale"])
async def test_downstream_error_called_once(
    middleware_name: str, state: FSMContext
) -> None:
    """Ошибка обработчика распространяется без повторного исполнения."""
    middleware = (
        throttle.ThrottleMiddleware()
        if middleware_name == "throttle"
        else locale.LocaleFromDBMiddleware({})
    )
    handler = AsyncMock(side_effect=RuntimeError("downstream"))
    event = Update(update_id=1, message=make_message(text="hello"))
    with pytest.raises(RuntimeError, match="downstream"):
        await middleware(handler, event, {"state": state})
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_locale_lookup_error_does_not_repeat_handler(
    monkeypatch: pytest.MonkeyPatch, state: FSMContext
) -> None:
    """Недоступность локали не дублирует побочный эффект обработчика."""
    monkeypatch.setattr(
        locale, "orm_get_locale", AsyncMock(side_effect=RuntimeError)
    )
    handler = AsyncMock()
    event = Update(update_id=1, message=make_message(text="hello"))
    await locale.LocaleFromDBMiddleware({})(
        handler, event, {"state": state, "session": object()}
    )
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_payments_bypass_throttle() -> None:
    """Checkout и успешная оплата не теряются при частых апдейтах."""
    middleware = throttle.ThrottleMiddleware(ttl=60)
    handler = AsyncMock()
    ordinary = Update(update_id=1, message=make_message(text="hello"))
    checkout = Update(update_id=2, pre_checkout_query=make_checkout("payload"))
    successful = Update(
        update_id=3,
        message=make_message(
            successful_payment={
                "currency": "XTR", "total_amount": 10,
                "invoice_payload": "payload",
                "telegram_payment_charge_id": "charge",
                "provider_payment_charge_id": "",
            }
        ),
    )
    for event in (ordinary, ordinary, checkout, successful, successful):
        await middleware(handler, event, {})
    assert handler.await_count == 4


@pytest.mark.asyncio
async def test_callback_locale_restored(
    monkeypatch: pytest.MonkeyPatch, state: FSMContext
) -> None:
    """Первый callback после очистки FSM восстанавливает язык из БД."""
    lookup = AsyncMock(return_value="en")
    monkeypatch.setattr(locale, "orm_get_locale", lookup)
    callback = CallbackQuery(
        id="callback", chat_instance="chat", data="menu",
        from_user=User(id=USER_ID, is_bot=False, first_name="Test"),
        message=make_message(),
    )
    handler = AsyncMock()
    event = Update(update_id=1, callback_query=callback)
    await locale.LocaleFromDBMiddleware({})(
        handler, event, {"state": state, "session": object()}
    )
    lookup.assert_awaited_once()
    assert lookup.await_args.args[1] == USER_ID
    assert (await state.get_data())["locale"] == "en"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsupported_cached_locale_reloaded(
    monkeypatch: pytest.MonkeyPatch, state: FSMContext
) -> None:
    """Старый неподдерживаемый язык в FSM заменяется допустимым."""
    await state.update_data(locale="fr")
    monkeypatch.setattr(locale, "orm_get_locale", AsyncMock(return_value="en"))
    event = Update(update_id=1, message=make_message(text="hello"))
    await locale.LocaleFromDBMiddleware({})(
        AsyncMock(), event, {"state": state, "session": object()}
    )
    assert (await state.get_data())["locale"] == "en"


@pytest.mark.asyncio
async def test_duplicate_payment_one_record(db: AsyncSession) -> None:
    """Повторная доставка в новой сессии не создаёт вторую транзакцию."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    payload = invoice.invoice_payload
    _, created = await orm_record_payment(
        db, payload, USER_ID, "XTR", 10, "charge"
    )
    assert created
    async with AsyncSession(db.bind, expire_on_commit=False) as another:
        _, created = await orm_record_payment(
            another, payload, USER_ID, "XTR", 10, "charge"
        )
        assert not created
        count = await another.scalar(
            select(func.count()).select_from(Payments)
        )
        assert count == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_payment_one_record(db: AsyncSession) -> None:
    """Конкурирующие подтверждения фиксируют charge ID только один раз."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    payload = invoice.invoice_payload
    async with (
        AsyncSession(db.bind, expire_on_commit=False) as first,
        AsyncSession(db.bind, expire_on_commit=False) as second,
    ):
        results = await asyncio.gather(
            orm_record_payment(first, payload, USER_ID, "XTR", 10, "charge"),
            orm_record_payment(second, payload, USER_ID, "XTR", 10, "charge"),
        )
    assert sorted(created for _, created in results) == [False, True]
    assert await db.scalar(select(func.count()).select_from(Payments)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("payload", "unknown"), ("user_id", 999), ("currency", "USD"),
     ("amount", 11), ("charge_id", "")],
)
async def test_payment_rejects_mismatched_invoice(
    db: AsyncSession, field: str, value: object
) -> None:
    """Подмена владельца, валюты, суммы, payload или ID не проходит."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    values = {"payload": invoice.invoice_payload, "user_id": USER_ID,
              "currency": "XTR", "amount": 10, "charge_id": "charge"}
    values[field] = value
    with pytest.raises(ValueError):
        await orm_record_payment(db, **values)
    payment = await db.scalar(select(Payments))
    assert payment.status == "pending"
    assert payment.telegram_payment_charge_id is None


@pytest.mark.asyncio
async def test_charge_cannot_pay_two_invoices(db: AsyncSession) -> None:
    """UNIQUE по charge ID защищает даже разные счета."""
    first = await orm_create_invoice(db, USER_ID, 10)
    second = await orm_create_invoice(db, USER_ID, 10)
    first_payload = first.invoice_payload
    second_payload = second.invoice_payload
    await orm_record_payment(db, first_payload, USER_ID, "XTR", 10, "charge")
    with pytest.raises(ValueError):
        await orm_record_payment(
            db, second_payload, USER_ID, "XTR", 10, "charge"
        )
    count = await db.scalar(
        select(func.count())
        .select_from(Payments)
        .where(Payments.status == "paid")
    )
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_state", [None, "City:forecast_moment", "Donate:donate_input_x"]
)
async def test_successful_payment_after_main(
    db: AsyncSession, state: FSMContext, monkeypatch: pytest.MonkeyPatch,
    next_state: str | None,
) -> None:
    """Пустой FSM после /main не мешает маршрутизации и записи оплаты."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    await state.set_state(donate.Donate.donate_send)
    await state.set_state(None)  # Такой сброс выполняет /main.
    await state.set_state(next_state)
    await state.update_data(locale="en")
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    message = make_message(
        successful_payment={
            "currency": "XTR", "total_amount": 10,
            "invoice_payload": invoice.invoice_payload,
            "telegram_payment_charge_id": "charge",
            "provider_payment_charge_id": "",
        }
    )
    for _ in range(2):
        await donate.payment_router.propagate_event(
            "message", message, session=db, state=state, raw_state=next_state
        )
    assert (await orm_get_payment(db, "charge")).status == "paid"
    assert (await state.get_data())["locale"] == "en"
    assert await state.get_state() == next_state
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_donation_invoice_saved_before_send(
    db: AsyncSession, state: FSMContext
) -> None:
    """Пользователь получает только уже сохранённый проверяемый счёт."""
    await state.update_data(locale="en")
    message = SimpleNamespace(
        text="10", from_user=SimpleNamespace(id=USER_ID),
        answer_invoice=AsyncMock(),
    )
    await donate.cmd_donate_input_x(message, state, db)
    invoice = await db.scalar(select(Payments))
    assert invoice.user_id == USER_ID
    assert invoice.currency == "XTR"
    assert invoice.total_amount == 10
    assert invoice.status == "pending"
    sent_payload = message.answer_invoice.await_args.kwargs["payload"]
    assert sent_payload == invoice.invoice_payload
    assert await state.get_state() == donate.Donate.donate_send.state
    assert (await state.get_data())["locale"] == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "currency,amount,payload_ok",
    [("XTR", 10, True), ("USD", 10, True),
     ("XTR", 11, True), ("XTR", 10, False)],
)
async def test_checkout_validation_without_fsm(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    currency: str, amount: int, payload_ok: bool,
) -> None:
    """Checkout не зависит от FSM и отвергает подмену параметров."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    answer = AsyncMock()
    monkeypatch.setattr(PreCheckoutQuery, "answer", answer)
    query = make_checkout(
        invoice.invoice_payload if payload_ok else "unknown",
        currency=currency,
        total_amount=amount,
    )
    await donate.payment_router.propagate_event(
        "pre_checkout_query", query, session=db, raw_state=None
    )
    answer.assert_awaited_once()
    expected = currency == "XTR" and amount == 10 and payload_ok
    assert answer.await_args.kwargs["ok"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", [None, "", "no", "0", "2501", "²", "9" * 5000]
)
async def test_invalid_donation_input(
    text: str | None, state: FSMContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нетекстовый и некорректный ввод не создаёт счёт и не падает."""
    send_invoice = AsyncMock()
    monkeypatch.setattr(donate, "_send_invoice", send_invoice)
    message = SimpleNamespace(text=text, answer=AsyncMock())
    await donate.cmd_donate_input_x(message, state, None)
    message.answer.assert_awaited_once()
    send_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_preserves_locale(
    state: FSMContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отмена доната не очищает сохранённый язык."""
    await state.update_data(locale="en", donate_amount=10)
    await state.set_state(donate.Donate.donate_send)
    monkeypatch.setattr(Message, "answer", AsyncMock())
    monkeypatch.setattr(Message, "delete", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    callback = CallbackQuery(
        id="cb", chat_instance="chat", data="donate_cancel",
        from_user=User(id=USER_ID, is_bot=False, first_name="Test"),
        message=make_message(),
    )
    await donate.on_donate_cancel(callback, state)
    assert await state.get_state() is None
    assert (await state.get_data())["locale"] == "en"


@pytest.mark.asyncio
async def test_refund_empty_input(db: AsyncSession) -> None:
    """Пустой refund даёт подсказку без обращения к Telegram."""
    message = SimpleNamespace(answer=AsyncMock())
    bot = SimpleNamespace(refund_star_payment=AsyncMock())
    await donate.command_refund_handler(
        message, bot, CommandObject(command="refund"), db
    )
    message.answer.assert_awaited_once()
    bot.refund_star_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_feedback_and_idempotent_repeat(db: AsyncSession) -> None:
    """Успех возврата сохраняется, повтор не вызывает внешний метод."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    await orm_record_payment(
        db, invoice.invoice_payload, USER_ID, "XTR", 10, "charge"
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=USER_ID), answer=AsyncMock()
    )
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True))
    command = CommandObject(command="refund", args="charge")
    await donate.command_refund_handler(message, bot, command, db)
    await donate.command_refund_handler(message, bot, command, db)
    bot.refund_star_payment.assert_awaited_once()
    assert (await orm_get_payment(db, "charge")).status == "refunded"
    assert message.answer.await_count == 2


@pytest.mark.asyncio
async def test_refund_api_error_reported(db: AsyncSession) -> None:
    """Ошибка Telegram не помечает платёж возвращённым."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    await orm_record_payment(
        db, invoice.invoice_payload, USER_ID, "XTR", 10, "charge"
    )
    error = TelegramAPIError(
        method=RefundStarPayment(
            user_id=USER_ID, telegram_payment_charge_id="charge"
        ),
        message="mock error",
    )
    bot = SimpleNamespace(refund_star_payment=AsyncMock(side_effect=error))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=USER_ID), answer=AsyncMock()
    )
    await donate.command_refund_handler(
        message, bot, CommandObject(command="refund", args="charge"), db
    )
    message.answer.assert_awaited_once()
    assert (await orm_get_payment(db, "charge")).status == "paid"


@pytest.mark.asyncio
async def test_confirmed_refund_database_error_explained(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой записи после подтверждения не скрывает результат Telegram."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    await orm_record_payment(
        db, invoice.invoice_payload, USER_ID, "XTR", 10, "charge"
    )
    monkeypatch.setattr(
        donate, "orm_mark_refunded", AsyncMock(side_effect=RuntimeError)
    )
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=USER_ID), answer=AsyncMock()
    )
    await donate.command_refund_handler(
        message, bot, CommandObject(command="refund", args="charge"), db
    )
    message.answer.assert_awaited_once()
    assert "Telegram" in message.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("age_days", [31, 365, None])
async def test_refund_has_no_arbitrary_age_limit(
    db: AsyncSession, age_days: int | None
) -> None:
    """Локальная дата не вводит несуществующий временной запрет возврата."""
    invoice = await orm_create_invoice(db, USER_ID, 10)
    payment, _ = await orm_record_payment(
        db, invoice.invoice_payload, USER_ID, "XTR", 10, "charge"
    )
    payment.paid_at = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
        if age_days is not None else None
    )
    await db.commit()
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=USER_ID), answer=AsyncMock()
    )
    await donate.command_refund_handler(
        message, bot, CommandObject(command="refund", args="charge"), db
    )
    bot.refund_star_payment.assert_awaited_once_with(
        user_id=USER_ID, telegram_payment_charge_id="charge"
    )
    message.answer.assert_awaited_once()
    assert (await orm_get_payment(db, "charge")).status == "refunded"


@pytest.mark.asyncio
async def test_game_without_username(db: AsyncSession) -> None:
    """Пользователь без Telegram username сохраняется в новой схеме."""
    db.add(Games(game_name="snake", user_id=USER_ID, user_name=None, score=1))
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(Games)) == 1
