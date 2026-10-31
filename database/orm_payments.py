"""Проверка выданных счетов и идемпотентный учёт Telegram Stars."""

from datetime import datetime, timezone
from secrets import token_urlsafe

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Payments

MIN_DONATION = 1
MAX_DONATION = 2500
PAYMENT_CURRENCY = "XTR"


async def orm_create_invoice(
    session: AsyncSession, user_id: int, amount: int
) -> Payments:
    """Сохраняет проверяемый счёт до его отправки в Telegram."""
    if not MIN_DONATION <= amount <= MAX_DONATION:
        raise ValueError("Недопустимая сумма доната")
    invoice = Payments(
        invoice_payload=f"donate:{token_urlsafe(24)}",
        user_id=user_id,
        currency=PAYMENT_CURRENCY,
        total_amount=amount,
        status="pending",
    )
    session.add(invoice)
    await session.commit()
    return invoice


async def orm_get_invoice(
    session: AsyncSession, payload: str
) -> Payments | None:
    """Возвращает актуальное состояние выданного счёта."""
    query = (
        select(Payments)
        .where(Payments.invoice_payload == payload)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(query)


async def orm_get_payment(
    session: AsyncSession, charge_id: str
) -> Payments | None:
    """Находит подтверждение по уникальному ID Telegram."""
    query = (
        select(Payments)
        .where(Payments.telegram_payment_charge_id == charge_id)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(query)


def validate_invoice(
    invoice: Payments | None,
    user_id: int,
    currency: str,
    amount: int,
) -> None:
    """Сверяет владельца, валюту и сумму с сохранённым счётом."""
    if (
        invoice is None
        or invoice.user_id != user_id
        or currency != PAYMENT_CURRENCY
        or invoice.currency != currency
        or invoice.total_amount != amount
        or not MIN_DONATION <= amount <= MAX_DONATION
    ):
        raise ValueError("Счёт не найден или его параметры не совпадают")


async def orm_record_payment(
    session: AsyncSession,
    payload: str,
    user_id: int,
    currency: str,
    amount: int,
    charge_id: str,
) -> tuple[Payments, bool]:
    """Однократно фиксирует платёж, включая повторную доставку апдейта."""
    if not charge_id or len(charge_id) > 255:
        raise ValueError("Не указан корректный ID платежа")
    invoice = await orm_get_invoice(session, payload)
    validate_invoice(invoice, user_id, currency, amount)
    assert invoice is not None
    if invoice.telegram_payment_charge_id == charge_id:
        return invoice, False
    if (
        invoice.status != "pending"
        or invoice.telegram_payment_charge_id is not None
    ):
        raise ValueError("Счёт уже оплачен другой транзакцией")
    query = (
        update(Payments)
        .where(
            Payments.invoice_payload == payload,
            Payments.status == "pending",
            Payments.telegram_payment_charge_id.is_(None),
        )
        .values(
            status="paid",
            telegram_payment_charge_id=charge_id,
            paid_at=datetime.now(timezone.utc),
        )
    )
    try:
        result = await session.execute(query)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await orm_get_payment(session, charge_id)
        if existing is not None and existing.invoice_payload == payload:
            validate_invoice(existing, user_id, currency, amount)
            return existing, False
        raise ValueError("ID платежа уже относится к другому счёту") from None
    invoice = await orm_get_invoice(session, payload)
    assert invoice is not None
    if invoice.telegram_payment_charge_id != charge_id:
        raise ValueError("Счёт уже оплачен другой транзакцией")
    return invoice, result.rowcount == 1


async def orm_mark_refunded(session: AsyncSession, charge_id: str) -> None:
    """Сохраняет успешный возврат после подтверждения Telegram."""
    query = (
        update(Payments)
        .where(Payments.telegram_payment_charge_id == charge_id)
        .values(status="refunded", refunded_at=datetime.now(timezone.utc))
    )
    await session.execute(query)
    await session.commit()
