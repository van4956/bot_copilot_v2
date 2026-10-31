"""Проверки реестра платежей только на отдельной PostgreSQL review."""

import asyncio
import os
from typing import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config_data.config import validate_test_database
from database import orm_payments
from database.models import Payments

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Отдельная PostgreSQL review не запущена",
    ),
]

PaymentScope = tuple[async_sessionmaker[AsyncSession], int, list[str]]


@pytest_asyncio.fixture
async def pg_payments() -> AsyncIterator[PaymentScope]:
    """Выделяет уникальные записи и удаляет только созданные этим тестом."""
    url = os.environ["TEST_DATABASE_URL"]
    validate_test_database(url)
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.username != "bot_app":
        raise ValueError("Нужна PostgreSQL review с ролью bot_app")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id = 9_000_000_000_000 + uuid4().int % 1_000_000_000_000
    payloads: list[str] = []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda conn: Payments.__table__.create(conn, checkfirst=True)
            )
        yield sessions, user_id, payloads
    finally:
        try:
            if payloads:
                async with sessions() as session:
                    await session.execute(
                        delete(Payments).where(
                            Payments.user_id == user_id,
                            Payments.invoice_payload.in_(payloads),
                        )
                    )
                    await session.commit()
        finally:
            await engine.dispose()


async def _new_invoice(scope: PaymentScope, amount: int = 10) -> str:
    """Создаёт искусственный счёт и сразу отмечает его для точечной очистки."""
    sessions, user_id, payloads = scope
    async with sessions() as session:
        invoice = await orm_payments.orm_create_invoice(session, user_id, amount)
        payload = invoice.invoice_payload
        payloads.append(payload)
        return payload


@pytest.mark.asyncio
async def test_postgres_payment_confirmation_is_idempotent(
    pg_payments: PaymentScope,
) -> None:
    """Повтор подтверждения в новой сессии не создаёт второй платёж."""
    sessions, user_id, _ = pg_payments
    payload = await _new_invoice(pg_payments)
    charge_id = f"review-integration:{uuid4().hex}"
    async with sessions() as session:
        payment, created = await orm_payments.orm_record_payment(
            session, payload, user_id, "XTR", 10, charge_id
        )
        assert created
        payment_id = payment.payment_id
        paid_at = payment.paid_at
        assert paid_at is not None

    async with sessions() as session:
        payment, created = await orm_payments.orm_record_payment(
            session, payload, user_id, "XTR", 10, charge_id
        )
        assert not created
        assert payment.payment_id == payment_id
        assert payment.paid_at == paid_at
        assert payment.status == "paid"
        assert payment.telegram_payment_charge_id == charge_id
        count = await session.scalar(
            select(func.count())
            .select_from(Payments)
            .where(
                Payments.user_id == user_id,
                Payments.invoice_payload == payload,
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_postgres_charge_id_cannot_pay_another_invoice(
    pg_payments: PaymentScope,
) -> None:
    """Одинаковый charge ID не фиксируется у двух разных счетов."""
    sessions, user_id, _ = pg_payments
    first_payload = await _new_invoice(pg_payments)
    second_payload = await _new_invoice(pg_payments)
    charge_id = f"review-integration:{uuid4().hex}"
    async with sessions() as session:
        await orm_payments.orm_record_payment(
            session, first_payload, user_id, "XTR", 10, charge_id
        )
    async with sessions() as session:
        with pytest.raises(ValueError):
            await orm_payments.orm_record_payment(
                session, second_payload, user_id, "XTR", 10, charge_id
            )
        second = await orm_payments.orm_get_invoice(session, second_payload)
        assert second is not None
        assert second.status == "pending"
        assert second.telegram_payment_charge_id is None
        payment = await orm_payments.orm_get_payment(session, charge_id)
        assert payment is not None
        assert payment.invoice_payload == first_payload
        count = await session.scalar(
            select(func.count())
            .select_from(Payments)
            .where(
                Payments.user_id == user_id,
                Payments.telegram_payment_charge_id == charge_id,
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_postgres_concurrent_confirmation_is_applied_once(
    pg_payments: PaymentScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Два реальных чтения pending конкурируют за одну запись подтверждения."""
    sessions, user_id, _ = pg_payments
    payload = await _new_invoice(pg_payments)
    charge_id = f"review-integration:{uuid4().hex}"
    original_get_invoice = orm_payments.orm_get_invoice
    initial_reads: set[AsyncSession] = set()
    both_read = asyncio.Event()

    async def synchronized_read(
        session: AsyncSession, requested_payload: str
    ) -> Payments | None:
        """Синхронизирует реальные SELECT перед конкурентными UPDATE."""
        invoice = await original_get_invoice(session, requested_payload)
        if session not in initial_reads:
            assert invoice is not None and invoice.status == "pending"
            initial_reads.add(session)
            if len(initial_reads) == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=10)
        return invoice

    async def confirm() -> tuple[int, bool]:
        """Использует отдельную PostgreSQL-сессию для каждого подтверждения."""
        async with sessions() as session:
            payment, created = await orm_payments.orm_record_payment(
                session, payload, user_id, "XTR", 10, charge_id
            )
            return payment.payment_id, created

    monkeypatch.setattr(orm_payments, "orm_get_invoice", synchronized_read)
    outcomes = await asyncio.wait_for(asyncio.gather(confirm(), confirm()), timeout=20)
    assert len(initial_reads) == 2
    assert sorted(created for _, created in outcomes) == [False, True]
    assert len({payment_id for payment_id, _ in outcomes}) == 1
    async with sessions() as session:
        rows = list(
            await session.scalars(
                select(Payments).where(
                    Payments.user_id == user_id,
                    Payments.invoice_payload == payload,
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].status == "paid"
        assert rows[0].telegram_payment_charge_id == charge_id
