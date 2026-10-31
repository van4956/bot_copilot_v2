"""Донаты Telegram Stars с проверкой счетов и устойчивым учётом."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from common import keyboard
from database.orm_payments import (
    MAX_DONATION,
    MIN_DONATION,
    PAYMENT_CURRENCY,
    orm_create_invoice,
    orm_get_invoice,
    orm_get_payment,
    orm_mark_refunded,
    orm_record_payment,
    validate_invoice,
)

logger = logging.getLogger(__name__)
payment_router = Router()
payment_router.message.filter(F.chat.type == "private")
donate_router = Router()
donate_router.message.filter(F.chat.type == "private")


class Donate(StatesGroup):
    """Состояния интерфейса, не определяющие обработку платежей."""

    donate_input = State()
    donate_input_x = State()
    donate_send = State()


def _donation_keyboard() -> InlineKeyboardBuilder:
    """Создаёт клавиатуру выбора суммы."""
    builder = InlineKeyboardBuilder()
    for amount in (10, 50, 100):
        builder.button(text=f"{amount} ⭐️", callback_data=f"donate_{amount}")
    builder.button(text=_("другое"), callback_data="donate_x")
    builder.button(text=_("Назад на главную ↩️"), callback_data="donate_back")
    builder.adjust(2, 2, 1)
    return builder


async def _send_invoice(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user_id: int,
    amount: int,
) -> None:
    """Выдаёт счёт, параметры которого уже сохранены в БД."""
    invoice = await orm_create_invoice(session, user_id, amount)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"{amount} XTR", pay=True)
    builder.button(text=_("Отменить"), callback_data="donate_cancel")
    builder.adjust(1)
    await message.answer_invoice(
        title=_("Поддержать автора донатом"),
        description=_("На сумму"),
        prices=[LabeledPrice(label=PAYMENT_CURRENCY, amount=amount)],
        provider_token="",
        payload=invoice.invoice_payload,
        currency=PAYMENT_CURRENCY,
        reply_markup=builder.as_markup(),
    )
    await state.update_data(donate_amount=amount)
    await state.set_state(Donate.donate_send)


@donate_router.message(Command("donate"))
async def cmd_donate(message: Message, state: FSMContext) -> None:
    """Открывает выбор суммы доната."""
    await message.answer(
        _("Поддержать автора донатом"),
        reply_markup=_donation_keyboard().as_markup(),
    )
    await state.set_state(Donate.donate_input)


@donate_router.callback_query(F.data == "donate")
async def callback_donate(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает донаты по кнопке меню."""
    if isinstance(callback.message, Message):
        await cmd_donate(callback.message, state)
    await callback.answer()


@donate_router.callback_query(
    Donate.donate_input, F.data.startswith("donate_")
)
async def cmd_donate_input(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Проверяет выбранную сумму до создания счёта."""
    if not isinstance(callback.message, Message):
        await callback.answer(_("Сообщение больше недоступно"))
        return
    choice = (callback.data or "").removeprefix("donate_")
    if choice == "back":
        # Язык и остальные пользовательские настройки не удаляем.
        await state.set_state(None)
        await state.update_data(donate_amount=None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            _("Главная панель"), reply_markup=keyboard.start_keyboard()
        )
    elif choice == "x":
        await callback.message.answer(
            _("Введите произвольную сумму доната, от 1 до 2500 ⭐️"),
            reply_markup=keyboard.del_kb,
        )
        await state.set_state(Donate.donate_input_x)
    elif choice in {"10", "50", "100"}:
        await _send_invoice(
            callback.message,
            state,
            session,
            callback.from_user.id,
            int(choice),
        )
    else:
        await callback.answer(_("Выберите сумму кнопками меню"))
        return
    await callback.answer()


@payment_router.pre_checkout_query()
async def pre_checkout_query(
    query: PreCheckoutQuery, session: AsyncSession
) -> None:
    """Проверяет счёт независимо от текущего состояния диалога."""
    try:
        invoice = await orm_get_invoice(session, query.invoice_payload)
        validate_invoice(
            invoice, query.from_user.id, query.currency, query.total_amount
        )
        if invoice is None or invoice.status != "pending":
            raise ValueError("Счёт уже обработан")
    except ValueError:
        await query.answer(
            ok=False,
            error_message=_("Счёт недействителен. Создайте новый донат."),
        )
    except Exception:
        logger.exception("Не удалось проверить счёт")
        await query.answer(
            ok=False,
            error_message=_("Не удалось проверить счёт. Попробуйте позже."),
        )
    else:
        await query.answer(ok=True)


# Подтверждение оплаты важнее любых диалоговых состояний ввода.
@payment_router.message(F.successful_payment)
async def on_successfull_payment(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    """Сохраняет транзакцию один раз, даже после выхода на главную."""
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    try:
        payment_record, created = await orm_record_payment(
            session,
            payment.invoice_payload,
            message.from_user.id,
            payment.currency,
            payment.total_amount,
            payment.telegram_payment_charge_id,
        )
    except ValueError:
        logger.exception("Платёж не соответствует выданному счёту")
        await message.answer(
            _("Не удалось сверить платёж. Обратитесь к автору бота.")
        )
        return
    if not created:
        return
    if await state.get_state() == Donate.donate_send.state:
        await state.set_state(None)
        await state.update_data(donate_amount=None)
    await message.answer(
        _("<b>Спасибо!</b>\nВаш донат успешно принят.\n\n"
          "ID транзакции:\n<code>{t_id}</code>").format(
              t_id=escape(payment_record.telegram_payment_charge_id or "")
          )
    )


@payment_router.message(Command("refund"))
async def command_refund_handler(
    message: Message, bot: Bot, command: CommandObject, session: AsyncSession
) -> None:
    """Передаёт запрос возврата собственного подтверждённого доната."""
    charge_id = (command.args or "").strip()
    if (
        not charge_id
        or len(charge_id) > 255
        or any(char.isspace() for char in charge_id)
    ):
        await message.answer(_("Укажите ID транзакции: /refund ID"))
        return
    payment = await orm_get_payment(session, charge_id)
    if payment is None or payment.user_id != message.from_user.id:
        await message.answer(_("Платёж с таким ID для вас не найден."))
        return
    if payment.status == "refunded":
        await message.answer(_("Этот донат уже возвращён."))
        return
    if payment.status != "paid":
        await message.answer(_("Платёж ещё не подтверждён."))
        return
    try:
        confirmed = await bot.refund_star_payment(
            user_id=message.from_user.id, telegram_payment_charge_id=charge_id
        )
    except TelegramAPIError:
        logger.exception("Telegram не подтвердил возврат доната")
        await message.answer(
            _("Не удалось подтвердить возврат. Попробуйте позже.")
        )
        return
    if not confirmed:
        await message.answer(_("Telegram не подтвердил возврат доната."))
        return
    try:
        await orm_mark_refunded(session, charge_id)
    except Exception:
        logger.exception("Возврат подтверждён, но его статус не сохранён")
        await message.answer(_(
            "Telegram подтвердил возврат, но локальный статус не сохранён. "
            "Обратитесь к автору бота."
        ))
        return
    await message.answer(_("Донат успешно возвращён."))


@donate_router.message(Donate.donate_input_x)
async def cmd_donate_input_x(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Проверяет произвольную сумму, включая нетекстовый ввод."""
    text = (message.text or "").strip()
    if not text.isascii() or not text.isdecimal() or len(text) > 4:
        amount = 0
    else:
        amount = int(text)
    if not MIN_DONATION <= amount <= MAX_DONATION:
        await message.answer(
            _("Введите целое число от 1 до 2500 ⭐️."),
            reply_markup=keyboard.del_kb,
        )
        return
    await _send_invoice(message, state, session, message.from_user.id, amount)


@donate_router.callback_query(Donate.donate_send, F.data == "donate_cancel")
async def on_donate_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрывает интерфейс оплаты, сохраняя язык пользователя."""
    await state.set_state(None)
    await state.update_data(donate_amount=None)
    if isinstance(callback.message, Message):
        await callback.message.delete()
        await callback.message.answer(
            _("Главная панель"), reply_markup=keyboard.start_keyboard()
        )
    await callback.answer(_("Донат отменён."))
