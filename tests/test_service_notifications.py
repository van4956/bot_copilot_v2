"""Служебные события остаются русскими и не используют внешние сервисы."""

import ast
from datetime import datetime, timezone
from html import escape
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ChatMemberUpdated, User
from aiogram.utils.i18n import I18n

import app
from config_data.config import Config, DataBase, ROOT, Redis, TgBot
from handlers import start

USER_ID = 9000012345
SERVICE_GROUP = -9000098765
USER_NAME = "<Said & Ahmad> 🤍"


def _user(username: str | None = None, locale: str = "ru") -> User:
    """Создаёт вымышленного пользователя с произвольным именем."""
    return User(
        id=USER_ID,
        is_bot=False,
        first_name=USER_NAME,
        username=username,
        language_code=locale,
    )


def _bot(*, test_mode: bool = False) -> SimpleNamespace:
    """Подменяет только Telegram-транспорт, не создавая настоящего клиента."""
    return SimpleNamespace(
        test_mode=test_mode,
        notify_service_chat=True,
        home_group=[SERVICE_GROUP],
        admin_list=[USER_ID],
        send_message=AsyncMock(),
    )


def _event(user: User, *, blocked: bool) -> ChatMemberUpdated:
    """Готовит изменение статуса бота только в вымышленном личном чате."""
    bot_user = User(id=123456, is_bot=True, first_name="Offline bot")
    member = {"status": "member", "user": bot_user}
    kicked = {"status": "kicked", "user": bot_user, "until_date": 0}
    return ChatMemberUpdated.model_validate(
        {
            "chat": {"id": user.id, "type": "private", "first_name": user.first_name},
            "from_user": user,
            "date": datetime.now(timezone.utc),
            "old_chat_member": member if blocked else kicked,
            "new_chat_member": kicked if blocked else member,
        }
    )


@pytest.mark.parametrize(
    "username,expected",
    [
        ("test_test", "@test_test"),
        ("@test_test", "@test_test"),
        ("@@test_test", "@test_test"),
        ('<name>&"', "@&lt;name&gt;&amp;&quot;"),
        (None, str(USER_ID)),
        ("", str(USER_ID)),
        ("@", str(USER_ID)),
    ],
)
def test_service_identity_is_username_or_only_id(
    username: str | None,
    expected: str,
) -> None:
    """Метка содержит одну собачку либо только ID, без имени и HTML-инъекций."""
    assert start._service_user_label(_user(username)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en", "es", "uk"])
@pytest.mark.parametrize("username", ["test_test", None])
@pytest.mark.parametrize("created", [True, False])
async def test_signup_notification_is_russian_once_and_greeting_keeps_locale(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    username: str | None,
    created: bool,
) -> None:
    """Служебная подписка русская, повторный /start не создаёт новое событие."""
    actor = _user(username, language)
    message = SimpleNamespace(
        from_user=actor,
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )
    bot = _bot()
    saved = SimpleNamespace(locale=language)
    add_user = AsyncMock(return_value=(saved, created))
    monkeypatch.setattr(start, "orm_add_user", add_user)
    monkeypatch.setattr(start.keyboard, "start_keyboard", lambda bot=None: None)
    storage = MemoryStorage()
    state = FSMContext(
        storage, StorageKey(bot_id=123456, chat_id=USER_ID, user_id=USER_ID)
    )
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    try:
        await state.set_data({"locale": language})
        with i18n.context(), i18n.use_locale(language):
            await start.start_cmd(message, AsyncMock(), bot, state, {})
            assert message.answer.await_args.args[0] == i18n.gettext("Бот активирован!")
        assert await state.get_data() == {"locale": language}
        assert add_user.await_args.args[1]["user_id"] == USER_ID
        if created:
            label = f"@{username}" if username else str(USER_ID)
            bot.send_message.assert_awaited_once_with(
                chat_id=SERVICE_GROUP,
                text=f"✅ {label} - подписался на бота",
            )
            expected_name = escape(username or USER_NAME)
            assert expected_name in message.answer_photo.await_args.kwargs["caption"]
        else:
            bot.send_message.assert_not_awaited()
            message.answer_photo.assert_not_awaited()
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en", "es", "uk"])
@pytest.mark.parametrize("username", ["test_test", None])
@pytest.mark.parametrize("blocked", [True, False])
async def test_membership_notifications_russian_and_user_welcome_translated(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    username: str | None,
    blocked: bool,
) -> None:
    """События берут username либо ID; личное приветствие остаётся переводимым."""
    actor = _user(username, language)
    event = _event(actor, blocked=blocked)
    session = AsyncMock()
    bot = _bot()
    update_status = AsyncMock()
    monkeypatch.setattr(start, "orm_update_status", update_status)
    monkeypatch.setattr(
        start, "orm_get_user", AsyncMock(return_value=SimpleNamespace())
    )
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    with i18n.context(), i18n.use_locale(language):
        if blocked:
            await start.process_user_blocked_bot(event, session, bot, {})
        else:
            await start.process_user_unblocked_bot(event, session, bot, {})
            welcome = i18n.gettext("{full_name}, Добро пожаловать обратно!").format(
                full_name=escape(USER_NAME),
            )
            assert bot.send_message.await_args_list[0].kwargs == {
                "chat_id": USER_ID,
                "text": welcome,
            }
    label = f"@{username}" if username else str(USER_ID)
    expected = (
        f"⛔️ {label} - заблокировал бота"
        if blocked
        else f"♻️ {label} - разблокировал бота"
    )
    calls = [
        call.kwargs
        for call in bot.send_message.await_args_list
        if call.kwargs.get("chat_id") == SERVICE_GROUP
    ]
    assert calls == [{"chat_id": SERVICE_GROUP, "text": expected}]
    update_status.assert_awaited_once_with(
        session,
        USER_ID,
        "kicked" if blocked else "member",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", ["signup", "block", "unblock"])
async def test_test_mode_never_sends_events_to_service_group(
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
) -> None:
    """Локальный тест не пишет в рабочую группу даже при заданном адресе."""
    bot = _bot(test_mode=True)
    session = AsyncMock()
    monkeypatch.setattr(start, "orm_update_status", AsyncMock())
    monkeypatch.setattr(
        start, "orm_get_user", AsyncMock(return_value=SimpleNamespace())
    )
    monkeypatch.setattr(start, "_", lambda text: text)
    if event_name == "signup":
        await start._notify_service(bot, f"✅ {USER_ID} - подписался на бота")
    elif event_name == "block":
        await start.process_user_blocked_bot(
            _event(_user(), blocked=True), session, bot, {}
        )
    else:
        await start.process_user_unblocked_bot(
            _event(_user(), blocked=False), session, bot, {}
        )
    assert all(
        call.kwargs.get("chat_id") != SERVICE_GROUP
        for call in bot.send_message.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en", "es", "uk"])
@pytest.mark.parametrize("polling_fails", [False, True])
async def test_lifecycle_service_notifications_exact_and_cleanup_preserved(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    polling_fails: bool,
) -> None:
    """Жизненный цикл отправляет два русских события и закрывает ресурсы при сбое."""
    bot = _bot()
    bot.get_me = AsyncMock(return_value=_user("offline_bot"))
    bot.delete_webhook = AsyncMock()
    bot.set_my_commands = AsyncMock()
    bot.session = SimpleNamespace(close=AsyncMock())
    dispatcher = SimpleNamespace(
        storage=SimpleNamespace(close=AsyncMock()),
        resolve_used_update_types=lambda: ["message"],
        start_polling=AsyncMock(
            side_effect=RuntimeError("offline polling failure")
            if polling_fails
            else None,
        ),
    )
    config = Config(
        tg_bot=TgBot(
            token="123456:OFFLINE_TEST_ONLY", token_test="", home_group=[SERVICE_GROUP]
        ),
        db=DataBase(),
        redis=Redis(),
        mode="production",
        database_url="sqlite+aiosqlite:///:memory:",
        create_schema=True,
        notify_service_chat=True,
    )
    monkeypatch.setattr(app, "create_bot", lambda config: bot)
    monkeypatch.setattr(app, "create_dispatcher", lambda *args: dispatcher)
    i18n = I18n(path=ROOT / "locales", domain="bot_00_template")
    with i18n.context(), i18n.use_locale(language):
        if polling_fails:
            with pytest.raises(RuntimeError, match="offline polling failure"):
                await app.main(config)
        else:
            await app.main(config)
    assert [call.args for call in bot.send_message.await_args_list] == [
        (SERVICE_GROUP, "🤖 Бот запущен"),
        (SERVICE_GROUP, "☠️ Бот остановлен"),
    ]
    dispatcher.storage.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


def test_service_templates_never_pass_through_gettext() -> None:
    """Защищает служебные шаблоны от случайного включения в будущие переводы."""
    expected = {
        "🤖 Бот запущен",
        "☠️ Бот остановлен",
        "✅  - подписался на бота",
        "⛔️  - заблокировал бота",
        "♻️  - разблокировал бота",
    }
    found = set()
    for module in (app, start):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"notify_service", "_notify_service"}:
                continue
            template = node.args[-1]
            assert isinstance(template, (ast.Constant, ast.JoinedStr))
            if isinstance(template, ast.JoinedStr):
                found.add(
                    "".join(
                        value.value
                        for value in template.values
                        if isinstance(value, ast.Constant)
                    )
                )
            else:
                found.add(template.value)
    assert found == expected
