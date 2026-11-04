"""Асинхронный конвертер валют с проверкой ответа сервиса."""

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __

from common import keyboard
from common.http import ExternalServiceError, get_json
from filters.chat_type import ChatTypeFilter

currency_router = Router()
currency_router.message.filter(ChatTypeFilter(["private"]))
currency_router.callback_query.filter(F.message.chat.type == "private")
CURRENCY = __("Валюта 💵")
REPEAT = __("Повторить 💵")
BACK_TO_MAIN = __("Назад на главную ↩️")
CURRENCIES = {
    "$, USD 🇺🇸": "USD",
    "₽, RUB 🇷🇺": "RUB",
    "₾, GEL 🇬🇪": "GEL",
    "€, EUR 🇪🇺": "EUR",
    "₸, KZT 🇰🇿": "KZT",
    "₺, TRY 🇹🇷": "TRY",
    "¥, JPY 🇯🇵": "JPY",
    "₫, VND 🇻🇳": "VND",
    "¥ CNY 🇨🇳": "CNY",
    "₪, ILS 🇮🇱": "ILS",
    "₱, PHP 🇵🇭": "PHP",
    "AED 🇦🇪": "AED",
}
markup_cur = keyboard.get_callback_btns(btns=CURRENCIES, sizes=(3, 3, 3, 3))
CURRENCY_CODES = frozenset(CURRENCIES.values())
MAX_AMOUNT_LENGTH = 18
logger = logging.getLogger(__name__)


class Currency(StatesGroup):
    """Состояния конвертации."""

    amount = State()
    base_currency = State()
    target_currency = State()


def parse_amount(value: str) -> Decimal:
    """Проверяет единый формат суммы для сообщения и inline-ввода."""
    text = value.strip()
    if len(text) > MAX_AMOUNT_LENGTH or re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text
    ) is None:
        raise ValueError("Недопустимая сумма")
    amount = Decimal(text)
    if not amount.is_finite() or amount < 0:
        raise ValueError("Недопустимая сумма")
    return amount


def _amount_prompt(amount: Decimal) -> str:
    """Формирует одинаковый переход к выбору базовой валюты."""
    return _("Сумма операции: {user_value}\nБазовая валюта: ").format(
        user_value=amount
    )


async def _confirm_amount(
    state: FSMContext, amount: Decimal, raw_value: str
) -> None:
    """Сохраняет подтверждённую сумму без очистки языка пользователя."""
    await state.update_data(currency_amount=str(amount), user_value=raw_value)
    await state.set_state(Currency.base_currency)


async def _dismiss_keypad(message: Message, message_id: int | None) -> None:
    """По возможности убирает кнопки старого цифрового сообщения."""
    if message_id is None:
        return
    try:
        await message.bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=None,
        )
    except TelegramAPIError:
        logger.warning("Не удалось убрать старую цифровую клавиатуру")


async def get_currency_rate(
    base_currency: str,
    target_currency: str,
    *,
    api_key: str = "",
    session: aiohttp.ClientSession | None = None,
) -> tuple[str, str, Decimal]:
    """Возвращает подтверждённый курс, не подставляя вымышленную единицу."""
    if not api_key:
        raise ExternalServiceError("Сервис валют не настроен")
    if base_currency not in CURRENCY_CODES or target_currency not in CURRENCY_CODES:
        raise ExternalServiceError("Неизвестная валюта")
    data = await get_json(
        "https://openexchangerates.org/api/latest.json",
        params={"app_id": api_key},
        session=session,
        timeout=10,
    )
    try:
        rates = data["rates"]
        base = Decimal(str(rates[base_currency]))
        target = Decimal(str(rates[target_currency]))
        if not base.is_finite() or not target.is_finite() or min(base, target) <= 0:
            raise ValueError
        timestamp = datetime.fromtimestamp(data["timestamp"], tz=timezone.utc)
        return (
            timestamp.strftime("%H:%M"),
            timestamp.strftime("%d-%m-%Y"),
            target / base,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as error:
        raise ExternalServiceError("Некорректный ответ сервиса валют") from error


async def update_num_text(message: Message, new_value: str) -> None:
    """Обновляет сумму на клавиатуре."""
    await message.edit_text(
        _("Сумма операции: {new_value}").format(new_value=new_value or "0"),
        reply_markup=keyboard.markup_num(),
    )


def _next_keyboard():
    """Возвращает меню после расчёта."""
    return keyboard.get_keyboard(
        _("Повторить 💵"),
        _("Назад на главную ↩️"),
        placeholder="⬇️",
        sizes=(1, 1),
    )


@currency_router.message(StateFilter(None), or_f(F.text == CURRENCY, F.text == REPEAT))
async def process_reopen(
    message: Message,
    state: FSMContext,
    workflow_data: dict,
) -> None:
    """Начинает новый расчёт."""
    await state.update_data(user_value="")
    await message.answer("💵", reply_markup=keyboard.del_kb)
    keypad = await message.answer(
        _("Сумма операции: 0.0"),
        reply_markup=keyboard.markup_num(),
    )
    await state.update_data(currency_keypad_message_id=keypad.message_id)
    await state.set_state(Currency.amount)


@currency_router.callback_query(Currency.amount, F.data.startswith("num_"))
async def process_amout(callback: CallbackQuery, state: FSMContext) -> None:
    """Принимает только допустимые действия цифровой клавиатуры."""
    data = await state.get_data()
    keypad_id = data.get("currency_keypad_message_id")
    if (
        await state.get_state() != Currency.amount.state
        or callback.message is None
        or (
            keypad_id is not None
            and callback.message.message_id != keypad_id
        )
    ):
        await callback.answer(
            _("Эта клавиатура устарела. Используйте текущую.")
        )
        return
    value = str(data.get("user_value", ""))
    action = (callback.data or "").removeprefix("num_")
    if action == "enter":
        try:
            amount = parse_amount(value)
        except (ValueError, InvalidOperation):
            await callback.answer(_("Ошибка в получении суммы"), show_alert=True)
            return
        await _confirm_amount(state, amount, value)
        await callback.message.edit_text(
            _amount_prompt(amount),
            reply_markup=markup_cur,
        )
    else:
        previous = value
        if action == "del":
            value = value[:-1]
        elif action == "com" and "." not in value:
            value = (value or "0") + "."
        elif action in {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "000"}:
            value += action
        if len(value) > MAX_AMOUNT_LENGTH:
            await callback.answer(_("Слишком длинная сумма"))
            return
        if value != previous:
            await state.update_data(user_value=value)
            await update_num_text(callback.message, value)
    await callback.answer()


@currency_router.message(Currency.amount)
async def process_amout_(message: Message, state: FSMContext) -> None:
    """Принимает текст суммы как Enter, сохраняя FSM при ошибке ввода."""
    if await state.get_state() != Currency.amount.state:
        return
    try:
        amount = parse_amount(message.text or "")
    except (ValueError, InvalidOperation):
        await message.answer(_(
            "Введите неотрицательное число до 18 символов: цифры и точка."
        ))
        return
    data = await state.get_data()
    await _confirm_amount(state, amount, (message.text or "").strip())
    await _dismiss_keypad(message, data.get("currency_keypad_message_id"))
    await message.answer(_amount_prompt(amount), reply_markup=markup_cur)


@currency_router.callback_query(F.data.startswith("num_"))
async def stale_numeric_keypad(callback: CallbackQuery) -> None:
    """Подтверждает устаревшие нажатия без изменения текущего диалога."""
    await callback.answer(_("Эта клавиатура устарела. Используйте текущую."))


@currency_router.callback_query(Currency.base_currency, F.data.in_(CURRENCY_CODES))
async def process_base_currency(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбирает базовую валюту без перехвата чужих кнопок."""
    data = await state.get_data()
    await state.update_data(currency_base=callback.data)
    await state.set_state(Currency.target_currency)
    await callback.message.edit_text(
        _("Сумма операции: {amount}\nБазовая валюта: {base}\nЦелевая валюта: ").format(
            amount=data["currency_amount"], base=callback.data
        ),
        reply_markup=markup_cur,
    )
    await callback.answer()


@currency_router.message(Currency.base_currency)
async def process_base_currency_(message: Message) -> None:
    """Подсказывает способ выбора базовой валюты."""
    await message.answer(
        _(
            "Вы ввели не допустимые данные, введите базовую валюту используя inline кнопки"
        )
    )


@currency_router.callback_query(Currency.target_currency, F.data.in_(CURRENCY_CODES))
async def process_target_currency(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Выполняет расчёт и завершает диалог даже при отказе сервиса."""
    try:
        data = await state.get_data()
        base, target = data["currency_base"], callback.data
        time, date, rate = await get_currency_rate(
            base,
            target,
            api_key=getattr(callback.bot, "api_currency", ""),
            session=getattr(callback.bot, "http_session", None),
        )
        amount = Decimal(data["currency_amount"])
        count = (amount * rate).quantize(Decimal("0.01"))
        await callback.message.edit_text(
            _(
                "Сумма операции: {amount}\nБазовая валюта: {base}\nЦелевая валюта: {target}\n\n<i>{dt},  {time}</i>\n\n<b>{amount} {base}  ~  {count} {target}</b>"
            ).format(
                amount=amount,
                base=base,
                target=target,
                count=count,
                dt=date,
                time=time,
            )
        )
        await callback.answer(_("Успех"))
    except (ExternalServiceError, KeyError, InvalidOperation):
        await callback.message.edit_text(_("Сервис валют временно недоступен"))
        await callback.answer(_("Ошибка"))
    finally:
        await state.set_state(None)
        await state.update_data(
            currency_amount=None, currency_base=None, currency_target=None
        )
    await callback.message.answer(_("Что дальше?"), reply_markup=_next_keyboard())


@currency_router.message(Currency.target_currency)
async def process_target_currency_(message: Message) -> None:
    """Подсказывает способ выбора целевой валюты."""
    await message.answer(
        _(
            "Вы ввели не допустимые данные, введите целевую валюту используя inline кнопки"
        )
    )
