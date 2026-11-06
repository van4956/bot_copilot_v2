"""Проверки регистрации и служебных уведомлений без внешних сервисов."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ChatMemberUpdated, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from database import orm_users
from database.models import Base, Users
from handlers import start

USER_ID = 10


@pytest.fixture(autouse=True)
def isolate_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает реальный транспорт Telegram и изолирует перевод."""
    monkeypatch.setattr(
        Bot, "__call__", AsyncMock(side_effect=AssertionError("No Telegram"))
    )
    monkeypatch.setattr(start, "_", lambda text: text)
    monkeypatch.setattr(start.keyboard, "start_keyboard", lambda bot=None: None)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """Создаёт отдельную временную базу и закрывает соединения."""
    path = (tmp_path / "users.sqlite3").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def state() -> AsyncIterator[FSMContext]:
    """Создаёт память FSM без Redis."""
    storage = MemoryStorage()
    context = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    yield context
    await storage.close()


def make_user_data(**values: object) -> dict[str, object]:
    """Возвращает синтетические данные пользователя."""
    return {
        "user_id": USER_ID,
        "user_name": "test",
        "full_name": "Test User",
        "locale": "en",
        "status": "member",
        "flag": 1,
        **values,
    }


def make_message(**user_values: object) -> SimpleNamespace:
    """Заменяет отправку пользовательских сообщений заглушками."""
    user = User.model_validate({
        "id": USER_ID, "is_bot": False, "first_name": "Test",
        "language_code": "ru", **user_values,
    })
    return SimpleNamespace(
        from_user=user, answer=AsyncMock(), answer_photo=AsyncMock()
    )


@pytest.mark.asyncio
async def test_upsert_preserves_selected_locale_and_flag(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторная регистрация обновляет профиль, но не выбранный язык."""
    scan = AsyncMock(side_effect=AssertionError("Full scan forbidden"))
    monkeypatch.setattr(orm_users, "orm_get_ids", scan)
    _, created = await orm_users.orm_add_user(
        db, make_user_data(locale="en", flag=0)
    )
    assert created
    user, created = await orm_users.orm_add_user(
        db, make_user_data(locale="ru", user_name="new", status="member")
    )
    assert not created
    assert user.locale == "en"
    assert user.user_name == "new"
    assert user.flag == 0
    scan.assert_not_awaited()
    assert await db.scalar(select(func.count()).select_from(Users)) == 1


@pytest.mark.asyncio
async def test_concurrent_registration_one_user(db: AsyncSession) -> None:
    """Две одновременные регистрации не дают IntegrityError и дубликат."""
    async with (
        AsyncSession(db.bind, expire_on_commit=False) as first,
        AsyncSession(db.bind, expire_on_commit=False) as second,
    ):
        results = await asyncio.gather(
            orm_users.orm_add_user(first, make_user_data()),
            orm_users.orm_add_user(second, make_user_data()),
        )
    assert sorted(created for _, created in results) == [False, True]
    assert await db.scalar(select(func.count()).select_from(Users)) == 1


@pytest.mark.asyncio
async def test_unknown_locale_and_long_name(db: AsyncSession) -> None:
    """Неподдерживаемый язык и длинное имя безопасно нормализуются."""
    user, _ = await orm_users.orm_add_user(
        db, make_user_data(locale="uk", user_name=None, full_name="x" * 256)
    )
    assert user.locale == "ru"
    assert user.user_name == ""
    assert len(user.full_name) == 150


@pytest.mark.asyncio
@pytest.mark.parametrize("test_mode,groups", [(True, [999]), (False, [])])
async def test_start_optional_service_group(
    db: AsyncSession, state: FSMContext, test_mode: bool, groups: list[int]
) -> None:
    """Тестовый режим и пустые группы не вызывают служебных отправок."""
    bot = SimpleNamespace(
        test_mode=test_mode, home_group=groups,
        notify_service_chat=True, send_message=AsyncMock(),
    )
    message = make_message()
    await start.start_cmd(message, db, bot, state, {})
    assert await orm_users.orm_get_user(db, USER_ID) is not None
    message.answer.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_error_does_not_block_registration(
    db: AsyncSession, state: FSMContext
) -> None:
    """Ошибка уведомления не откатывает регистрацию и активацию."""
    bot = SimpleNamespace(
        test_mode=False, home_group=[999], notify_service_chat=True,
        send_message=AsyncMock(side_effect=RuntimeError("service error")),
    )
    message = make_message()
    message.answer_photo.side_effect = RuntimeError("photo error")
    await start.start_cmd(message, db, bot, state, {})
    user = await orm_users.orm_get_user(db, USER_ID)
    assert user is not None and user.status == "member"
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_resets_state_preserves_locale(
    db: AsyncSession, state: FSMContext
) -> None:
    """/start сбрасывает диалог, а не ранее выбранный язык."""
    await orm_users.orm_add_user(db, make_user_data(locale="en"))
    await state.set_state("Any:state")
    await state.update_data(locale="en", recipe_id=5)
    bot = SimpleNamespace(
        test_mode=True, home_group=[], send_message=AsyncMock()
    )
    message = make_message(language_code="ru")
    await start.start_cmd(message, db, bot, state, {})
    assert await state.get_state() is None
    assert await state.get_data() == {"locale": "en"}
    assert (await orm_users.orm_get_user(db, USER_ID)).locale == "en"
    message.answer_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_names_escaped_in_html(
    db: AsyncSession, state: FSMContext
) -> None:
    """Произвольное имя не становится HTML-разметкой в сообщениях."""
    bot = SimpleNamespace(
        test_mode=False, home_group=[999], notify_service_chat=True,
        send_message=AsyncMock(),
    )
    message = make_message(first_name="<Test & User>", username=None)
    await start.start_cmd(message, db, bot, state, {})
    caption = message.answer_photo.await_args.kwargs["caption"]
    service_text = bot.send_message.await_args.kwargs["text"]
    assert "&lt;Test &amp; User&gt;" in caption
    assert "&lt;Test &amp; User&gt;" in service_text


@pytest.mark.asyncio
async def test_group_membership_is_not_user_block() -> None:
    """Удаление бота из группы не меняет статус человека в users."""
    bot_user = {"id": 123, "is_bot": True, "first_name": "Bot"}
    event = ChatMemberUpdated.model_validate({
        "chat": {"id": -100, "type": "supergroup", "title": "Group"},
        "from_user": {"id": USER_ID, "is_bot": False, "first_name": "Test"},
        "date": datetime.now(timezone.utc),
        "old_chat_member": {"status": "member", "user": bot_user},
        "new_chat_member": {
            "status": "kicked", "user": bot_user, "until_date": 0
        },
    })
    session = AsyncMock()
    bot = SimpleNamespace(send_message=AsyncMock())
    await start.start_router.propagate_event(
        "my_chat_member", event, session=session, bot=bot, workflow_data={}
    )
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    bot.send_message.assert_not_awaited()
