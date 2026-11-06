"""Проверка подписи Telegram Mini App без хранения входных данных."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

from .engine import GameError

MAX_INIT_DATA_BYTES = 16384
MAX_INIT_DATA_FIELDS = 64
MAX_TELEGRAM_USER_ID = (1 << 52) - 1
FUTURE_TOLERANCE_SECONDS = 30
HASH_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
KEY_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]{0,63}\Z")
AUTH_DATE_PATTERN = re.compile(r"[0-9]{1,12}\Z")
BAD_ESCAPE_PATTERN = re.compile(r"%(?![0-9a-fA-F]{2})")


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    """Минимальная подтверждённая личность без профиля и секретов."""

    user_id: int = field(repr=False)
    auth_date: int = field(repr=False)


def _auth_error() -> GameError:
    """Возвращает одинаковую безопасную ошибку авторизации."""
    return GameError(
        "authentication_failed",
        "Откройте игру заново через тестового бота.",
        401,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Не допускает неоднозначные повторные ключи в JSON профиля."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    """Запрещает нестандартные JSON-числа NaN и Infinity."""
    raise ValueError("Invalid JSON constant")


def _timestamp(now: datetime | int | float | None) -> float:
    """Приводит доверенные часы сервера к Unix-секундам."""
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Timezone required")
        value = now.timestamp()
    elif isinstance(now, (int, float)) and not isinstance(now, bool):
        value = float(now)
    else:
        raise ValueError("Invalid server clock")
    if not math.isfinite(value):
        raise ValueError("Invalid server clock")
    return value


def validate_init_data(
    init_data: str,
    bot_token: str,
    now: datetime | int | float | None = None,
    max_age: int = 86400,
) -> TelegramIdentity:
    """Проверяет HMAC, срок и Telegram ID по официальной схеме Mini App."""
    try:
        if (
            not isinstance(init_data, str)
            or not init_data
            or len(init_data) >= MAX_INIT_DATA_BYTES
            or len(init_data.encode("utf-8")) >= MAX_INIT_DATA_BYTES
            or BAD_ESCAPE_PATTERN.search(init_data)
            or not isinstance(bot_token, str)
            or not bot_token
            or not isinstance(max_age, int)
            or isinstance(max_age, bool)
            or max_age < 0
        ):
            raise ValueError("Invalid authorization input")
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_INIT_DATA_FIELDS,
        )
        values: dict[str, str] = {}
        for key, value in pairs:
            if (
                key in values
                or not KEY_PATTERN.fullmatch(key)
                or "\n" in value
                or "\r" in value
            ):
                raise ValueError("Ambiguous authorization data")
            values[key] = value
        received_hash = values.pop("hash", "")
        if not HASH_PATTERN.fullmatch(received_hash):
            raise ValueError("Invalid hash")
        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        # Для HMAC исключается только hash; signature тоже подписана.
        secret = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret, check_string.encode("utf-8"), hashlib.sha256
        ).digest()
        received_bytes = bytes.fromhex(received_hash)
        if not hmac.compare_digest(expected_hash, received_bytes):
            raise ValueError("Invalid signature")
        auth_value = values.get("auth_date", "")
        if not AUTH_DATE_PATTERN.fullmatch(auth_value):
            raise ValueError("Invalid authorization time")
        auth_date = int(auth_value)
        age = _timestamp(now) - auth_date
        if age > max_age or age < -FUTURE_TOLERANCE_SECONDS:
            raise ValueError("Expired authorization")
        user = json.loads(
            values.get("user", ""),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        user_id = user.get("id") if isinstance(user, dict) else None
        if (
            type(user_id) is not int
            or not 0 < user_id <= MAX_TELEGRAM_USER_ID
        ):
            raise ValueError("Invalid Telegram user")
        return TelegramIdentity(user_id=user_id, auth_date=auth_date)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise _auth_error() from None
