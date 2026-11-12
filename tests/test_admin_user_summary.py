"""Формат русской сводки пользователей без чтения настоящей базы."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import admin

TITLE = "Информация зарегистрированных пользователей:"


def _user(
    user_id: int, username: str | None = "example", *,
    status: str = "member", locale: str = "en", flag: int = 1,
) -> SimpleNamespace:
    """Создаёт синтетического пользователя с исходными полями."""
    return SimpleNamespace(
        user_id=user_id, user_name=username, status=status, locale=locale, flag=flag,
    )


def test_summary_matches_layout_and_marks_only_configured_admin() -> None:
    """Сводка дополняет ID нулями и отделяет заголовок с итогом пустыми строками."""
    users = [
        _user(123), _user(459148628, "van4956", locale="ru"),
        _user(1234567890, "van4956", status="guest", locale="ru"),
    ]

    assert admin.split_user_messages(users, admin_ids=[459148628]) == [
        f"{TITLE}\n\n"
        "0000000123 | m - en | @example\n"
        "0459148628 | a - ru | @van4956\n"
        "1234567890 | k - ru | @van4956\n\n"
        "Всего 3 пользователей"
    ]


def test_id_width_is_not_limited_to_ten_characters() -> None:
    """Самый длинный ID определяет ширину всех строк, в том числе первых."""
    users = [_user(1), _user(123456789012345), _user(99)]
    message = admin.split_user_messages(users)[0]
    rows = message.split("\n")[2:-2]

    assert [row.split(" | ")[0] for row in rows] == [
        "000000000000001", "123456789012345", "000000000000099",
    ]


@pytest.mark.parametrize("status, expected", [("member", "m"), ("guest", "k"), ("", "k")])
def test_non_admin_status_and_flag_omission(status: str, expected: str) -> None:
    """Поле flag не показывается, а прежние m/k не зависят от username."""
    user = _user(123, "van4956", status=status, locale="ru", flag=987654)
    message = admin.split_user_messages([user])[0]

    assert f"123 | {expected} - ru | @van4956" in message
    assert "987654" not in message
    assert user.flag == 987654


def test_admin_status_uses_active_ids_even_for_non_member() -> None:
    """Буква a определяется ID из конфигурации, а не статусом или именем."""
    users = [_user(1, "van4956"), _user(2, "other", status="guest")]
    message = admin.split_user_messages(users, admin_ids=[2])[0]

    assert "1 | m - en | @van4956" in message
    assert "2 | a - en | @other" in message
    assert message.count(" | a - ") == 1


def test_summary_keeps_missing_username_unchanged() -> None:
    """Отсутствующий username использует прежнюю русскую подпись."""
    for username in (None, ""):
        assert "12 | m - en | @без username" in admin.split_user_messages(
            [_user(12, username)]
        )[0]


def test_summary_accepts_generators_without_mutating_original_data() -> None:
    """Одноразовые списки читаются один раз, исходные поля остаются прежними."""
    users = [_user(99, "first"), _user(5, "second", locale="ru")]
    before = deepcopy([vars(user) for user in users])
    admin_ids = [5]
    message = admin.split_user_messages(
        (user for user in users), admin_ids=(user_id for user_id in admin_ids),
    )[0]

    assert [vars(user) for user in users] == before
    assert admin_ids == [5]
    assert message.index("@first") < message.index("@second")
    assert "05 | a - ru | @second" in message


def test_all_chunks_use_global_id_width_without_row_loss() -> None:
    """Поздний длинный ID дополняет нулями также строки первых сообщений."""
    users = [_user(index, f"user_{index}") for index in range(1, 51)]
    users.append(_user(123456789012345, "last"))
    chunks = admin.split_user_messages(users, limit=100)

    assert len(chunks) > 1
    assert all(0 < len(text.encode("utf-16-le")) // 2 <= 100 for text in chunks)
    combined = "\n".join(chunks)
    for user in users:
        expected = f"{str(user.user_id).zfill(15)} | m - en | @{user.user_name}"
        assert combined.count(expected + "\n") == 1
    assert combined.count(TITLE) == 1
    assert combined.endswith("Всего 51 пользователей")


@pytest.mark.parametrize("limit", [2, 31, 4000])
def test_unicode_and_small_limits_never_create_empty_messages(limit: int) -> None:
    """Длинные Unicode-строки не теряются и не превышают UTF-16 лимит Telegram."""
    username = "😀" * 5001 + "<tag>&"
    chunks = admin.split_user_messages([_user(12, username)], limit=limit)

    assert all(text.strip() for text in chunks)
    assert all(len(text.encode("utf-16-le")) // 2 <= limit for text in chunks)
    assert "".join(chunks).count("😀") == 5001
    assert "<tag>&" in "".join(chunks).replace("\n", "")


def test_padded_zeroes_are_kept_even_with_two_unit_chunks() -> None:
    """Самый короткий лимит сохраняет нули ID и обе пользовательские строки."""
    users = [_user(1, "first"), _user(12345678901, "last")]
    chunks = admin.split_user_messages(users, limit=2, admin_ids=[1])
    combined = "".join(chunks).replace("\n", "")

    assert all(0 < len(text.encode("utf-16-le")) // 2 <= 2 for text in chunks)
    assert "00000000001 | a - en | @first" in combined
    assert "12345678901 | m - en | @last" in combined


def test_empty_summary_is_still_clear() -> None:
    """Пустой список сохраняет заголовок и осмысленный итог."""
    assert admin.split_user_messages([]) == [f"{TITLE}\n\nВсего 0 пользователей"]


@pytest.mark.parametrize("limit", [-1, 0, 1])
def test_too_small_limit_still_rejected(limit: int) -> None:
    """Лимит меньше одной пары UTF-16 по-прежнему отклоняется."""
    with pytest.raises(ValueError, match="Слишком короткий лимит сообщения"):
        admin.split_user_messages([], limit=limit)


async def test_handler_uses_active_bot_admins_and_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обработчик передаёт ID администраторов и запрещает разбор HTML."""
    users = [_user(1, "van4956"), _user(2, "<owner>&", locale="ru")]
    get_users = AsyncMock(return_value=users)
    monkeypatch.setattr(admin, "orm_get_users", get_users)
    bot = SimpleNamespace(admin_list=[2])
    message = SimpleNamespace(bot=bot, answer=AsyncMock())
    session = object()

    await admin.get_users_info(message, session)

    get_users.assert_awaited_once_with(session)
    message.answer.assert_awaited_once_with(
        f"{TITLE}\n\n1 | m - en | @van4956\n"
        "2 | a - ru | @<owner>&\n\nВсего 2 пользователей",
        parse_mode=None,
    )


async def test_handler_without_admin_config_does_not_guess_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отсутствующий список доступа не превращает известный username в админа."""
    monkeypatch.setattr(admin, "orm_get_users", AsyncMock(return_value=[_user(1, "van4956")]))
    message = SimpleNamespace(answer=AsyncMock())

    await admin.get_users_info(message, object())

    assert "1 | m - en | @van4956" in message.answer.await_args.args[0]
    assert " | a - " not in message.answer.await_args.args[0]
