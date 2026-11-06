"""Офлайн-проверки Telegram HMAC без токенов и профилей пользователей."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from prototypes.wordli.engine import GameError
from prototypes.wordli.telegram_auth import (
    MAX_TELEGRAM_USER_ID,
    TelegramIdentity,
    validate_init_data,
)

TOKEN = "123456:UNIT_TEST_NOT_A_REAL_TOKEN"
NOW = 1793870400
USER_ID = 789012345


def manual_hmac(key: bytes, value: bytes) -> bytes:
    """Строит независимый HMAC через SHA-256 и стандартные блоки."""
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    padded = key.ljust(64, b"\0")
    inner = bytes(item ^ 0x36 for item in padded)
    outer = bytes(item ^ 0x5C for item in padded)
    return hashlib.sha256(
        outer + hashlib.sha256(inner + value).digest()
    ).digest()


def sign_fields(values: dict[str, str], token: str = TOKEN) -> str:
    """Подписывает синтетические поля без кода проверяемого модуля."""
    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret = manual_hmac(b"WebAppData", token.encode("utf-8"))
    digest = manual_hmac(secret, check_string.encode("utf-8")).hex()
    return urlencode({**values, "hash": digest})


def fields(**changes: str) -> dict[str, str]:
    """Возвращает только вымышленные данные Mini App."""
    return {
        "auth_date": str(NOW),
        "query_id": "SYNTHETIC_QUERY_ID",
        "user": json.dumps(
            {"id": USER_ID, "first_name": "Тест + Ёж & кот"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        **changes,
    }


def assert_auth_error(raw: object, **options: object) -> None:
    """Проверяет одинаковую безопасную ошибку без утечки входа."""
    with pytest.raises(GameError) as caught:
        validate_init_data(raw, TOKEN, now=NOW, **options)  # type: ignore
    error = caught.value
    assert error.status == 401
    assert error.code == "authentication_failed"
    assert error.message == "Откройте игру заново через тестового бота."
    assert TOKEN not in str(error)
    assert "SYNTHETIC_QUERY_ID" not in str(error)
    assert error.__suppress_context__


def test_valid_signature_and_minimal_hidden_identity() -> None:
    """Подтверждает HMAC и не возвращает профиль или входной секрет."""
    identity = validate_init_data(sign_fields(fields()), TOKEN, now=NOW)
    assert identity == TelegramIdentity(USER_ID, NOW)
    assert identity.user_id == USER_ID
    assert identity.auth_date == NOW
    assert repr(identity) == "TelegramIdentity()"
    assert not hasattr(identity, "token")
    assert not hasattr(identity, "init_data")
    assert not hasattr(identity, "first_name")


def test_hmac_vector_does_not_reverse_key_and_value() -> None:
    """Отличает схему Telegram от ошибочной перестановки HMAC-параметров."""
    payload = fields()
    message = "\n".join(
        f"{key}={value}" for key, value in sorted(payload.items())
    ).encode("utf-8")
    wrong_secret = manual_hmac(TOKEN.encode("utf-8"), b"WebAppData")
    wrong_hash = manual_hmac(wrong_secret, message).hex()
    assert_auth_error(urlencode({**payload, "hash": wrong_hash}))


def test_signature_field_is_included_in_hmac() -> None:
    """Не смешивает HMAC с отдельной схемой Ed25519 для третьих сторон."""
    payload = fields(signature="SYNTHETIC+/=signature")
    valid = sign_fields(payload)
    assert validate_init_data(valid, TOKEN, now=NOW).user_id == USER_ID
    unsigned_extra = sign_fields(fields()) + "&signature=attacker"
    assert_auth_error(unsigned_extra)


def test_percent_encoding_unicode_and_order_are_preserved() -> None:
    """Читает UTF-8 и специальные знаки ровно один раз."""
    payload = fields(start_param="тест + & = % / \\")
    signed = sign_fields(payload)
    reversed_fields = "&".join(reversed(signed.split("&")))
    identity = validate_init_data(reversed_fields, TOKEN, now=NOW)
    assert identity.user_id == USER_ID
    assert_auth_error(signed.replace("%D0%A2", "%D0%90", 1))


@pytest.mark.parametrize(
    "raw",
    [None, b"auth_date=1", 1, True, "", "x", "hash=", "x=%", "x=%GG"],
)
def test_missing_or_malformed_query_is_rejected(raw: object) -> None:
    """Не принимает пустой ввод и неоднозначный query string."""
    assert_auth_error(raw)


@pytest.mark.parametrize(
    "suffix", ["&auth_date=0", "&user=%7B%7D", "&hash=" + "0" * 64]
)
def test_duplicate_query_fields_are_rejected(suffix: str) -> None:
    """Запрещает повторные поля, даже если первая подпись верна."""
    assert_auth_error(sign_fields(fields()) + suffix)


def test_encoded_duplicate_key_is_rejected() -> None:
    """Проверяет повтор после декодирования имени ключа."""
    assert_auth_error(sign_fields(fields()) + "&%75ser=%7B%7D")


@pytest.mark.parametrize("digest", ["g" * 64, "0" * 63, "0" * 65, "0" * 64])
def test_invalid_hash_is_rejected(digest: str) -> None:
    """Не принимает неверный формат и несовпавшую подпись."""
    assert_auth_error(urlencode({**fields(), "hash": digest}))


def test_hash_hex_case_does_not_change_signature() -> None:
    """Сравнивает байты hex-подписи без зависимости от её регистра."""
    signed = sign_fields(fields())
    prefix, digest = signed.rsplit("hash=", 1)
    assert validate_init_data(prefix + "hash=" + digest.upper(), TOKEN, NOW)


def test_wrong_bot_token_does_not_authorize_identity() -> None:
    """Запрещает подпись другого бота при совпавших пользовательских полях."""
    assert_auth_error(sign_fields(fields(), token="654321:OTHER_FAKE_TOKEN"))


@pytest.mark.parametrize("age", [86401, 1000000, -31, -999999])
def test_expired_or_future_launch_is_rejected(age: int) -> None:
    """Отклоняет старый запуск и время за пределом допуска будущего."""
    assert_auth_error(sign_fields(fields(auth_date=str(NOW - age))))


@pytest.mark.parametrize("age", [0, 86400, -30])
def test_auth_date_window_edges_are_inclusive(age: int) -> None:
    """Принимает точные границы заданного срока и допуска часов."""
    identity = validate_init_data(
        sign_fields(fields(auth_date=str(NOW - age))), TOKEN, NOW
    )
    assert identity.auth_date == NOW - age


@pytest.mark.parametrize("auth_date", ["", "-1", "1.0", "+1", "NaN", "１２"])
def test_auth_date_requires_ascii_unix_seconds(auth_date: str) -> None:
    """Не преобразует дробные, знаковые и не-ASCII значения времени."""
    assert_auth_error(sign_fields(fields(auth_date=auth_date)))


def test_configurable_max_age_and_timezone_aware_clock() -> None:
    """Поддерживает укороченный срок и доверенные часы сервера с зоной."""
    raw = sign_fields(fields(auth_date=str(NOW - 61)))
    assert_auth_error(raw, max_age=60)
    now = datetime.fromtimestamp(NOW, timezone(timedelta(hours=4)))
    identity = validate_init_data(raw, TOKEN, now=now, max_age=61)
    assert identity.user_id == USER_ID


@pytest.mark.parametrize("user", ["{}", "null", "[]", "true", '"id"'])
def test_missing_user_identity_is_rejected(user: str) -> None:
    """Не авторизует запуск без объекта пользователя с Telegram ID."""
    assert_auth_error(sign_fields(fields(user=user)))


@pytest.mark.parametrize(
    "user_id", [None, True, False, 0, -1, "123", 12.5, 1 << 52]
)
def test_user_id_is_positive_telegram_integer(user_id: object) -> None:
    """Не принимает bool, строковый ID и значения вне размера Telegram."""
    user = json.dumps({"id": user_id}, ensure_ascii=False)
    assert_auth_error(sign_fields(fields(user=user)))


def test_user_id_may_exceed_32_bits() -> None:
    """Сохраняет целый Telegram ID без потери старших разрядов."""
    raw = sign_fields(fields(user=json.dumps({"id": MAX_TELEGRAM_USER_ID})))
    identity = validate_init_data(raw, TOKEN, now=NOW)
    assert identity.user_id == MAX_TELEGRAM_USER_ID


@pytest.mark.parametrize(
    "user",
    [
        '{"id":1,"id":2}',
        '{"id":1,"other":{"x":1,"x":2}}',
        '{"id":1,"x":NaN}',
        '{"id":1,"x":Infinity}',
        '{"id":1',
    ],
)
def test_ambiguous_or_invalid_user_json_is_rejected(user: str) -> None:
    """Запрещает дубли JSON-ключей и нестандартный синтаксис профиля."""
    assert_auth_error(sign_fields(fields(user=user)))


def test_raw_line_break_does_not_ambigually_extend_check_string() -> None:
    """Не допускает внедрение новой подписываемой строки в значение."""
    assert_auth_error(sign_fields(fields(query_id="x\nauth_date=0")))
    assert_auth_error(sign_fields(fields(query_id="x\ry")))


def test_length_field_count_and_utf8_limits_are_enforced() -> None:
    """Ограничивает общий объём, число полей и неверный UTF-8."""
    assert_auth_error("a" * 16384)
    assert_auth_error(sign_fields(fields(query_id="я" * 8200)))
    too_many = {f"key_{number}": "x" for number in range(64)}
    assert_auth_error(sign_fields({**fields(), **too_many}))
    assert_auth_error(sign_fields(fields()) + "&x=%FF")


def test_untrusted_user_id_field_cannot_replace_signed_user() -> None:
    """Игнорирует неподтверждённое желание заменить ID через другое поле."""
    raw = sign_fields(fields(user_id="999999999"))
    assert validate_init_data(raw, TOKEN, now=NOW).user_id == USER_ID
    assert_auth_error(sign_fields(fields()) + "&user_id=999999999")


@pytest.mark.parametrize("max_age", [-1, True, 1.5, float("inf")])
def test_invalid_max_age_has_safe_error(max_age: object) -> None:
    """Не пропускает ошибочную настройку срока авторизации."""
    assert_auth_error(sign_fields(fields()), max_age=max_age)


@pytest.mark.parametrize("now", [True, "now", float("nan"), float("inf")])
def test_invalid_server_clock_has_safe_error(now: object) -> None:
    """Не авторизует запрос при неподходящем значении часов."""
    with pytest.raises(GameError, match="Откройте игру заново"):
        validate_init_data(sign_fields(fields()), TOKEN, now=now)  # type: ignore


def test_naive_clock_and_empty_token_are_rejected() -> None:
    """Не использует неявный локальный часовой пояс и пустой секрет."""
    raw = sign_fields(fields())
    with pytest.raises(GameError):
        validate_init_data(raw, TOKEN, now=datetime(2026, 11, 5))
    with pytest.raises(GameError):
        validate_init_data(raw, "", now=NOW)
