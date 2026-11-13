"""Четыре языка сохраняются без внешних сервисов и изменения других данных."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
import gettext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update, User
from aiogram.utils.i18n import I18n
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from common.comands import get_commands, sync_user_commands
from common.locales import LANGUAGE_CODES, SUPPORTED_LOCALES, normalize_locale
from config_data.config import ROOT
from database.models import Base, Users
from database.orm_users import (
    orm_add_user,
    orm_get_locale,
    orm_get_user,
    orm_update_locale,
)
from handlers import other
from middlewares import locale as locale_middleware


@pytest_asyncio.fixture
async def locale_database() -> AsyncIterator[AsyncSession]:
    """Создаёт только изолированную SQLite в памяти."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


def _user_data(language: str) -> dict:
    """Готовит одного вымышленного пользователя без реальных идентификаторов."""
    return {
        "user_id": 900001,
        "user_name": "synthetic_user",
        "full_name": "Synthetic User",
        "locale": language,
        "status": "member",
        "flag": 0,
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ru", "ru"),
        ("en", "en"),
        ("es", "es"),
        ("uk", "uk"),
        ("es-MX", "es"),
        ("es-419", "es"),
        ("ES_mx", "es"),
        ("es_419", "es"),
        ("UK-UA", "uk"),
        ("uk_UA", "uk"),
        ("ru-RU", "ru"),
        ("EN_us", "en"),
        ("fr", "ru"),
        ("ua", "ru"),
        ("", "ru"),
        (None, "ru"),
        ("../../en", "ru"),
        ("es/secret", "ru"),
    ],
)
def test_locale_normalization_is_supported_base_only(
    raw: str | None,
    expected: str,
) -> None:
    """Региональные коды не создают новые языки или пути к каталогам."""
    assert normalize_locale(raw) == expected
    assert SUPPORTED_LOCALES == frozenset(LANGUAGE_CODES)
    assert LANGUAGE_CODES == ("ru", "en", "es", "uk")


@pytest.mark.parametrize("selected", LANGUAGE_CODES)
@pytest.mark.parametrize("incoming", LANGUAGE_CODES)
async def test_repeated_registration_preserves_every_selected_language(
    locale_database: AsyncSession,
    selected: str,
    incoming: str,
) -> None:
    """Повторный /start не заменяет сохранённый выбор другим языком Telegram."""
    await orm_add_user(locale_database, _user_data(selected))
    user, created = await orm_add_user(locale_database, _user_data(incoming))
    assert not created
    assert user.locale == selected
    assert user.flag == 0
    assert await orm_get_locale(locale_database, user.user_id) == selected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("es-MX", "es"),
        ("es_419", "es"),
        ("UK-UA", "uk"),
        ("fr", "ru"),
    ],
)
async def test_registration_stores_canonical_language(
    locale_database: AsyncSession,
    raw: str,
    expected: str,
) -> None:
    """При регистрации в поле языка записывается только базовый код."""
    user, created = await orm_add_user(locale_database, _user_data(raw))
    assert created and user.locale == expected


@pytest.mark.parametrize("selected", LANGUAGE_CODES)
async def test_changing_language_preserves_other_user_fields(
    locale_database: AsyncSession,
    selected: str,
) -> None:
    """Смена локали не меняет имя, статус, флаг или идентификатор пользователя."""
    before, _ = await orm_add_user(locale_database, _user_data("ru"))
    original = (
        before.user_id,
        before.user_name,
        before.full_name,
        before.status,
        before.flag,
    )
    await orm_update_locale(locale_database, before.user_id, selected)
    after = await orm_get_user(locale_database, before.user_id)
    assert after.locale == selected
    assert (
        after.user_id,
        after.user_name,
        after.full_name,
        after.status,
        after.flag,
    ) == original


@pytest.mark.parametrize("unknown", ["ua", "fr", "", "../ru", "es-MX"])
async def test_explicit_choice_rejects_noncanonical_codes(
    locale_database: AsyncSession,
    unknown: str,
) -> None:
    """Поддельный явный выбор не меняет сохранённый язык."""
    user, _ = await orm_add_user(locale_database, _user_data("uk"))
    with pytest.raises(ValueError, match="Язык не поддерживается"):
        await orm_update_locale(locale_database, user.user_id, unknown)
    assert await orm_get_locale(locale_database, user.user_id) == "uk"


async def test_legacy_regional_code_is_read_without_database_rewrite(
    locale_database: AsyncSession,
) -> None:
    """Чтение старого регионального кода не запускает массовое исправление данных."""
    user, _ = await orm_add_user(locale_database, _user_data("ru"))
    await locale_database.execute(
        update(Users).where(Users.user_id == user.user_id).values(locale="ES_mx")
    )
    await locale_database.commit()
    assert await orm_get_locale(locale_database, user.user_id) == "es"
    assert await locale_database.scalar(select(Users.locale)) == "ES_mx"


@pytest.mark.parametrize("language", LANGUAGE_CODES)
def test_language_keyboard_has_two_rows_and_unchanged_navigation(language: str) -> None:
    """Все четыре кнопки и переходы имеют одинаковые технические значения."""
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    with i18n.context(), i18n.use_locale(language):
        rows = other.get_keyboard().inline_keyboard
        assert [[button.callback_data for button in row] for row in rows] == [
            ["locale_en", "locale_ru"],
            ["locale_es", "locale_uk"],
            ["back_to_info"],
            ["about_back_to_main"],
        ]
        originals = [
            "🇺🇸 Английский",
            "🇷🇺 Русский",
            "🇪🇸 Испанский",
            "🇺🇦 Украинский",
            "Назад",
            "Назад на главную ↩️",
        ]
        assert [button.text for row in rows for button in row] == [
            i18n.gettext(text) for text in originals
        ]


@pytest.mark.parametrize("language", LANGUAGE_CODES)
async def test_personal_menu_uses_explicit_choice_not_active_context(
    language: str,
) -> None:
    """Личное меню перекрывает глобальную локаль и не создаёт языковые overrides."""
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    with i18n.context(), i18n.use_locale("en" if language != "en" else "ru"):
        await sync_user_commands(bot, 900001, language)
    call = bot.set_my_commands.await_args
    assert call.args[0] == get_commands(language)
    assert call.kwargs["scope"].chat_id == 900001
    assert "language_code" not in call.kwargs
    translator = gettext.translation(
        "bot_00_template", localedir=ROOT / "locales", languages=[language]
    ).gettext
    assert [command.description for command in call.args[0]] == [
        translator("Главная панель"),
        translator("Кулинарная книга"),
        translator("Настройки"),
    ]


@pytest.mark.parametrize(
    "raw,expected", [("es_419", "es"), ("UK-UA", "uk"), ("zz", "ru")]
)
async def test_new_user_locale_is_normalized_before_aiogram_parses_it(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    """Middleware не передаёт Babel региональный код с неподходящим разделителем."""
    storage = MemoryStorage()
    state = FSMContext(
        storage=storage, key=StorageKey(bot_id=1, chat_id=900001, user_id=900001)
    )
    actor = User(id=900001, is_bot=False, first_name="Synthetic", language_code=raw)
    event = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat={"id": actor.id, "type": "private"},
            from_user=actor,
        ),
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(locale_middleware, "orm_get_locale", lookup)
    handler = AsyncMock()
    try:
        await locale_middleware.LocaleFromDBMiddleware({})(
            handler,
            event,
            {"state": state, "session": object(), "event_from_user": actor},
        )
        assert (await state.get_data())["locale"] == expected
        lookup.assert_awaited_once()
        handler.assert_awaited_once()
    finally:
        await storage.close()
