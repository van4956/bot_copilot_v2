"""Офлайн-проверки напоминаний без настоящих сообщений Telegram."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from sqlalchemy import BigInteger, Column, MetaData, String, Table, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from prototypes.wordli import production_server, reminders, telegram_server
from prototypes.wordli.production_server import (
    ProductionConfig,
    create_production_app,
    load_production_config,
)
from prototypes.wordli.engine import LocalGameEngine
from prototypes.wordli.storage import (
    WordliStorage,
    profiles_table,
    settings_table,
    wordli_metadata,
)
from prototypes.wordli.bot_texts import (
    GAME_LANGUAGE_NOTICE,
    normalize_game_locale,
    with_game_language_notice,
)
from prototypes.wordli.reminders import (
    REMINDER_TEXT,
    ReminderResult,
    ReminderWorker,
    TelegramReminderSender,
    reminder_service,
)

NOW = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
TOKEN = "123456:SYNTHETIC_REMINDER_TOKEN"
USER_ID = 900001
ORIGIN = "https://wordli-reminders.example"


class Response:
    """Подменяет только ответ Telegram, не открывая сокет."""

    def __init__(self, status: int, body: object) -> None:
        """Запоминает вымышленные статус и JSON."""
        self.status = status
        self.body = body

    async def __aenter__(self) -> Response:
        """Возвращает локальный ответ."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Не использует внешние ресурсы."""

    async def json(self, **kwargs: object) -> object:
        """Может воспроизвести ошибку разбора JSON."""
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def storage() -> SimpleNamespace:
    """Создаёт очередь с одной заявкой на добровольное напоминание."""
    return SimpleNamespace(
        user_locale=AsyncMock(return_value="ru"),
        claim_due_reminders=AsyncMock(
            side_effect=[
                [{"user_id": USER_ID, "claim_token": "claim-one"}],
                [],
            ]
        ),
        finish_reminder=AsyncMock(),
    )


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"ok": True}, ReminderResult("sent")),
        (200, {"ok": False}, ReminderResult("failed")),
        (403, {"ok": False}, ReminderResult("blocked")),
        (429, {"parameters": {"retry_after": 7}}, ReminderResult("retry", 7)),
        (429, {}, ReminderResult("retry", 60)),
        (429, {"parameters": []}, ReminderResult("retry", 60)),
        (429, {"parameters": {"retry_after": True}}, ReminderResult("retry", 60)),
        (429, {"parameters": {"retry_after": -1}}, ReminderResult("retry", 60)),
        (429, {"parameters": {"retry_after": 999999}}, ReminderResult("retry", 60)),
        (500, {"ok": False}, ReminderResult("failed")),
        (401, {}, ReminderResult("failed")),
        (200, [True], ReminderResult("failed")),
        (200, ValueError("bad json"), ReminderResult("failed")),
    ],
)
async def test_sender_sends_plain_text_and_two_inline_buttons(
    status: int,
    body: object,
    expected: ReminderResult,
) -> None:
    """Ответы API не превращаются в бесконечные повторы и HTML-сообщения."""
    session = SimpleNamespace(post=Mock(return_value=Response(status, body)))
    sender = TelegramReminderSender(session, TOKEN, ORIGIN)
    assert await sender.send(USER_ID) == expected
    session.post.assert_called_once_with(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": USER_ID,
            "text": REMINDER_TEXT,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "5БУКВ", "web_app": {"url": ORIGIN}}],
                    [
                        {
                            "text": "Назад на главную ↩️",
                            "callback_data": "wordli_back_to_main",
                        }
                    ],
                ]
            },
        },
        allow_redirects=False,
    )


@pytest.mark.parametrize("error", [aiohttp.ClientError("offline"), TimeoutError()])
async def test_uncertain_network_delivery_is_not_retried(error: Exception) -> None:
    """Потеря ответа не означает, что Telegram не доставил сообщение."""
    session = SimpleNamespace(post=Mock(side_effect=error))
    assert await TelegramReminderSender(session, TOKEN, ORIGIN).send(USER_ID) == (
        ReminderResult("failed")
    )
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://wordli-reminders.example",
        "https://user:synthetic@wordli-reminders.example",
        "https://wordli-reminders.example/?token=synthetic",
        "https://wordli-reminders.example/#fragment",
    ],
)
def test_reminder_rejects_unsafe_game_link_before_sending(origin: str) -> None:
    """Не отправляет кнопку с пустым адресом, секретами или HTTP."""
    session = SimpleNamespace(post=Mock())
    with pytest.raises(ValueError):
        TelegramReminderSender(session, TOKEN, origin)
    session.post.assert_not_called()


@pytest.mark.parametrize(
    "origin",
    [
        ORIGIN,
        "https://other-wordli.example",
        "https://synthetic-test.trycloudflare.com",
    ],
)
async def test_reminder_button_uses_only_configured_game_url(origin: str) -> None:
    """Тестовый и рабочий адрес передаются явно, без подстановки production."""
    session = SimpleNamespace(post=Mock(return_value=Response(200, {"ok": True})))
    assert await TelegramReminderSender(session, TOKEN, origin).send(USER_ID) == (
        ReminderResult("sent")
    )
    payload = session.post.call_args.kwargs["json"]
    rows = payload["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [1, 1]
    assert rows[0][0] == {"text": "5БУКВ", "web_app": {"url": origin}}
    assert rows[1][0]["callback_data"] == "wordli_back_to_main"
    assert "reply_keyboard" not in payload["reply_markup"]
    assert "parse_mode" not in payload
    assert "message_effect_id" not in payload


async def test_worker_claims_one_user_immediately_before_each_send() -> None:
    """Нет заранее забронированной пачки с устаревшим согласием и сроком."""
    backend = storage()
    sender = AsyncMock(return_value=ReminderResult("sent"))
    pause = AsyncMock()
    worker = ReminderWorker(backend, sender, clock=lambda: NOW, sleep=pause)
    assert await worker.run_once() == 1
    assert backend.claim_due_reminders.await_count == 2
    backend.claim_due_reminders.assert_awaited_with(NOW, limit=1)
    sender.assert_awaited_once_with(USER_ID)
    backend.finish_reminder.assert_awaited_once_with(
        USER_ID,
        "claim-one",
        "sent",
        retry_at=None,
    )
    pause.assert_awaited_once_with(0.1)


async def test_explicit_rate_limit_uses_telegram_retry_after() -> None:
    """Время повтора сохраняется в БД, а не теряется в таймере процесса."""
    backend = storage()
    worker = ReminderWorker(
        backend,
        AsyncMock(return_value=ReminderResult("retry", 37)),
        clock=lambda: NOW,
        sleep=AsyncMock(),
    )
    assert await worker.run_once() == 1
    backend.finish_reminder.assert_awaited_once_with(
        USER_ID,
        "claim-one",
        "retry",
        retry_at=NOW + timedelta(seconds=37),
    )


async def test_blocked_account_is_passed_to_persistent_storage() -> None:
    """Запрет бота выключает только добровольное напоминание пользователя."""
    backend = storage()
    worker = ReminderWorker(
        backend,
        AsyncMock(return_value=ReminderResult("blocked")),
        clock=lambda: NOW,
        sleep=AsyncMock(),
    )
    await worker.run_once()
    backend.finish_reminder.assert_awaited_once_with(
        USER_ID,
        "claim-one",
        "blocked",
        retry_at=None,
    )


async def test_sender_failure_is_final_and_does_not_log_secrets(caplog) -> None:
    """Непредвиденное исключение не выдаёт токен и не дублирует доставку."""
    backend = storage()
    worker = ReminderWorker(
        backend,
        AsyncMock(side_effect=RuntimeError(TOKEN)),
        clock=lambda: NOW,
        sleep=AsyncMock(),
    )
    assert await worker.run_once() == 1
    backend.finish_reminder.assert_awaited_once_with(
        USER_ID,
        "claim-one",
        "failed",
        retry_at=None,
    )
    assert TOKEN not in caplog.text


async def test_cancellation_keeps_uncertain_claim_reserved() -> None:
    """Перезапуск после отменённой доставки не создаёт второе сообщение."""
    backend = storage()
    worker = ReminderWorker(
        backend,
        AsyncMock(side_effect=asyncio.CancelledError),
        clock=lambda: NOW,
        sleep=AsyncMock(),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()
    backend.finish_reminder.assert_not_awaited()


async def test_bad_clock_stops_before_claiming() -> None:
    """Локальное время без часового пояса не используется как UTC."""
    backend = storage()
    worker = ReminderWorker(
        backend,
        AsyncMock(),
        clock=lambda: datetime(2026, 11, 8, 6),
    )
    with pytest.raises(ValueError, match="часовой пояс"):
        await worker.run_once()
    backend.claim_due_reminders.assert_not_awaited()


async def test_polling_handles_storage_error_without_secret_logs(caplog) -> None:
    """Временная ошибка БД не прекращает будущий опрос очереди."""
    backend = storage()
    backend.claim_due_reminders.side_effect = RuntimeError(TOKEN)
    pause = AsyncMock(side_effect=asyncio.CancelledError)
    worker = ReminderWorker(backend, AsyncMock(), clock=lambda: NOW, sleep=pause)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    pause.assert_awaited_once_with(15.0)
    assert TOKEN not in caplog.text


async def test_service_awaits_worker_before_closing_http_session(monkeypatch) -> None:
    """Завершение приложения не оставляет фоновую задачу с закрытой сессией."""
    events = []
    started = asyncio.Event()

    class Session:
        """Подменяет жизненный цикл одной HTTP-сессии."""

        async def __aenter__(self):
            """Запоминает открытие."""
            events.append("session-open")
            return self

        async def __aexit__(self, *args):
            """Запоминает закрытие."""
            events.append("session-close")

    async def run(self):
        """Имитирует задачу, которая ждёт до остановки приложения."""
        events.append("worker-start")
        started.set()
        try:
            await asyncio.Future()
        finally:
            events.append("worker-stop")

    factory = Mock(return_value=Session())
    monkeypatch.setattr(reminders.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(ReminderWorker, "run", run)
    async with reminder_service(storage(), TOKEN, public_origin=ORIGIN):
        await started.wait()
        events.append("application-ready")
    assert events == [
        "session-open",
        "worker-start",
        "application-ready",
        "worker-stop",
        "session-close",
    ]
    timeout = factory.call_args.kwargs["timeout"]
    assert timeout.total == 10
    assert timeout.connect == 5


def test_test_server_passes_its_origin_to_reminder_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Локальный сервер не подставляет рабочий адрес в тестовые напоминания."""
    database = SimpleNamespace(dispose=AsyncMock())
    backend = SimpleNamespace(init_schema=AsyncMock())
    test_origin = "https://local-wordli.example"
    config = SimpleNamespace(
        test_mode=True,
        database_url="sqlite+aiosqlite:///:memory:",
        tg_bot=SimpleNamespace(
            token_test=TOKEN,
            owner=[USER_ID],
            admin_list=[USER_ID + 1],
        ),
        expected_test_bot_id=123456,
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WORDLI_REMINDERS_ENABLED", "true")
    monkeypatch.setattr(telegram_server, "load_config", Mock(return_value=config))
    monkeypatch.setattr(telegram_server, "dotenv_values", Mock(return_value={}))
    monkeypatch.setattr(
        telegram_server, "create_async_engine", Mock(return_value=database)
    )
    monkeypatch.setattr(telegram_server, "WordliStorage", Mock(return_value=backend))
    monkeypatch.setattr("sys.argv", ["telegram_server", "--public-origin", test_origin])
    calls: list[tuple[str, str, set[int] | None]] = []

    @asynccontextmanager
    async def service(
        store: WordliStorage,
        token: str,
        *,
        public_origin: str,
        allowed_user_ids: set[int] | None = None,
    ) -> AsyncIterator[None]:
        """Проверяет параметры без запуска задачи и HTTP-клиента."""
        assert store is backend
        calls.append((token, public_origin, allowed_user_ids))
        yield

    def run(application: web.Application, **kwargs: object) -> None:
        """Вызывает только lifecycle без прослушивания порта."""

        async def lifecycle() -> None:
            """Открывает и закрывает подменённые ресурсы."""
            application.freeze()
            await application.startup()
            await application.cleanup()

        asyncio.run(lifecycle())

    monkeypatch.setattr(telegram_server, "reminder_service", service)
    monkeypatch.setattr(telegram_server.web, "run_app", run)
    telegram_server.main()
    assert calls == [(TOKEN, test_origin, {USER_ID, USER_ID + 1})]
    backend.init_schema.assert_awaited_once_with()
    database.dispose.assert_awaited_once_with()


@pytest.mark.parametrize("enabled", [False, True])
async def test_production_cleanup_closes_worker_before_database(
    monkeypatch,
    enabled: bool,
) -> None:
    """Фоновый отправитель запускается только явной настройкой сервера."""
    events = []
    database = SimpleNamespace(
        dispose=AsyncMock(side_effect=lambda: events.append("db-close"))
    )
    backend = storage()
    monkeypatch.setattr(
        production_server, "create_async_engine", Mock(return_value=database)
    )
    monkeypatch.setattr(production_server, "WordliStorage", Mock(return_value=backend))
    monkeypatch.setattr(production_server, "assert_schema", AsyncMock())
    monkeypatch.setattr(production_server, "assert_runtime_permissions", AsyncMock())

    @asynccontextmanager
    async def service(store, token, *, public_origin, allowed_user_ids=None):
        """Запоминает только безопасные этапы запуска и остановки."""
        assert store is backend
        assert token == TOKEN
        assert public_origin == ORIGIN
        assert allowed_user_ids == frozenset({USER_ID})
        events.append("reminders-open")
        try:
            yield
        finally:
            events.append("reminders-close")

    monkeypatch.setattr(production_server, "reminder_service", service)
    config = ProductionConfig(
        public_origin="https://wordli-reminders.example",
        database_url="postgresql+asyncpg://app:synthetic@db/copilot",
        bot_token=TOKEN,
        expected_bot_id=123456,
        allowed_user_ids=frozenset({USER_ID}),
        reminders_enabled=enabled,
    )
    app = create_production_app(config)
    app.freeze()
    await app.startup()
    events.append("ready")
    await app.cleanup()
    assert events == (
        ["reminders-open", "ready", "reminders-close", "db-close"]
        if enabled
        else ["ready", "db-close"]
    )


@pytest.mark.parametrize(
    "setting,expected", [(None, False), ("false", False), ("true", True)]
)
def test_reminders_require_explicit_production_flag(setting, expected: bool) -> None:
    """Добавление кода не включает рассылку в прежней конфигурации."""
    values = {
        "APP_ENV": "production",
        "WORDLI_PUBLIC_ORIGIN": "https://wordli-reminders.example",
        "WORDLI_DATABASE_URL": "postgresql+asyncpg://app:synthetic@db/copilot",
        "WORDLI_BOT_TOKEN": TOKEN,
        "WORDLI_EXPECTED_BOT_ID": "123456",
        "WORDLI_ALLOWED_USER_IDS": str(USER_ID),
    }
    if setting is not None:
        values["WORDLI_REMINDERS_ENABLED"] = setting
    assert load_production_config(values).reminders_enabled is expected


@pytest.mark.parametrize("setting", ["", "TRUE", "1", "yes", " true"])
def test_invalid_reminder_flag_is_rejected(setting: str) -> None:
    """Опечатка настройки не включает напоминания неявно."""
    with pytest.raises(ValueError, match="WORDLI_REMINDERS_ENABLED"):
        load_production_config(
            {
                "APP_ENV": "production",
                "WORDLI_PUBLIC_ORIGIN": "https://wordli-reminders.example",
                "WORDLI_DATABASE_URL": "postgresql+asyncpg://app:synthetic@db/copilot",
                "WORDLI_BOT_TOKEN": TOKEN,
                "WORDLI_EXPECTED_BOT_ID": "123456",
                "WORDLI_ALLOWED_USER_IDS": str(USER_ID),
                "WORDLI_REMINDERS_ENABLED": setting,
            }
        )


@pytest_asyncio.fixture
async def reminder_backend(tmp_path: Path):
    """Создаёт только временную SQLite с управляемыми часами и случайностью."""
    database = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'reminder-test.sqlite3'}"
    )
    clock = SimpleNamespace(value=NOW)
    rules = LocalGameEngine()
    backend = WordliStorage(
        database,
        rules,
        clock=lambda: clock.value,
        random_seconds=lambda: 600,
    )
    try:
        await backend.init_schema()
        yield backend, database, rules, clock
    finally:
        await database.dispose()


async def reminder_data(database, user_id: int = USER_ID) -> dict:
    """Читает внутреннее расписание только из временной тестовой БД."""
    async with database.connect() as connection:
        return await connection.scalar(
            select(settings_table.c.data).where(settings_table.c.user_id == user_id)
        )


async def profile_data(database, user_id: int = USER_ID) -> dict:
    """Читает историю и версию без создания новой партии."""
    async with database.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(profiles_table).where(profiles_table.c.user_id == user_id)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


@pytest.mark.parametrize(
    "seconds,hour,minute", [(0, 5, 50), (600, 6, 0), (1200, 6, 10)]
)
async def test_random_schedule_covers_exact_local_window(
    reminder_backend,
    seconds: int,
    hour: int,
    minute: int,
) -> None:
    """Крайние и центральный случайные моменты не выходят за 05:50–06:10."""
    backend, database, rules, clock = reminder_backend
    random = Mock(return_value=seconds)
    backend = WordliStorage(
        database,
        rules,
        clock=lambda: clock.value,
        random_seconds=random,
    )
    await backend.settings(USER_ID, reminder_enabled=True)
    assert await backend.claim_due_reminders(NOW - timedelta(minutes=30)) == []
    data = await reminder_data(database)
    due = datetime.fromisoformat(data["reminder_due_at"])
    local = due.astimezone(ZoneInfo("Etc/GMT-4"))
    assert (local.hour, local.minute, local.second) == (hour, minute, 0)
    assert len(await backend.claim_due_reminders(due)) == 1
    random.assert_called_once_with()


async def test_schedule_is_persistent_and_does_not_reroll_after_restart(
    reminder_backend,
) -> None:
    """Опрос и перезапуск процесса не подбирают новое время тому же дню."""
    backend, database, rules, clock = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    assert await backend.claim_due_reminders(NOW - timedelta(minutes=20)) == []
    first = await reminder_data(database)
    random = Mock(return_value=1200)
    restarted = WordliStorage(
        database,
        rules,
        clock=lambda: clock.value,
        random_seconds=random,
    )
    assert await restarted.claim_due_reminders(NOW - timedelta(minutes=5)) == []
    assert await reminder_data(database) == first
    random.assert_not_called()


@pytest.mark.parametrize("day,expected_hour", [(28, 5), (29, 4)])
async def test_reminder_uses_player_timezone_including_dst(
    reminder_backend,
    day: int,
    expected_hour: int,
) -> None:
    """Переход Европы на летнее время не сдвигает местные 06:00."""
    backend, database, _, clock = reminder_backend
    clock.value = datetime(2026, 3, day, tzinfo=timezone.utc)
    await backend.settings(
        USER_ID,
        timezone_name="Europe/Berlin",
        reminder_enabled=True,
    )
    assert await backend.claim_due_reminders(clock.value) == []
    data = await reminder_data(database)
    due = datetime.fromisoformat(data["reminder_due_at"])
    assert due.hour == expected_hour
    assert due.astimezone(ZoneInfo("Europe/Berlin")).hour == 6


async def test_disabled_user_is_not_scheduled_or_claimed(reminder_backend) -> None:
    """Добавление пользователя не подписывает его на сообщения автоматически."""
    backend, database, _, _ = reminder_backend
    assert (await backend.settings(USER_ID))["reminder_enabled"] is False
    assert await backend.claim_due_reminders(NOW) == []
    assert "reminder_day" not in await reminder_data(database)


async def test_schedule_does_not_create_or_expire_any_game(reminder_backend) -> None:
    """Работа очереди не выдаёт слова и не меняет игровые версии."""
    backend, database, _, _ = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    before = await profile_data(database)
    assert before["history"] == []
    assert len(await backend.claim_due_reminders(NOW)) == 1
    assert await profile_data(database) == before


@pytest.mark.parametrize("outcome", ["win", "loss"])
async def test_completed_first_daily_word_skips_reminder(
    reminder_backend,
    outcome: str,
) -> None:
    """Завершённое первое слово исключает напоминание и при поражении."""
    backend, database, rules, _ = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    state = await backend.bootstrap(USER_ID)
    answer = (await profile_data(database))["history"][0]["answer"]
    if outcome == "win":
        guesses = [answer]
    else:
        wrong = next(word for word in rules._words if word != answer)
        guesses = [wrong] * 6
    for guess in guesses:
        state = await backend.submit(
            USER_ID,
            state["day"],
            state["game"]["index"],
            guess,
            state["revision"],
            str(uuid4()),
        )
    assert state["game"]["outcome"] == outcome
    before = await profile_data(database)
    assert await backend.claim_due_reminders(NOW) == []
    assert (await reminder_data(database))["reminder_status"] == "skipped"
    assert await profile_data(database) == before


async def test_expired_unfinished_word_does_not_skip_new_daily_reminder(
    reminder_backend,
) -> None:
    """Вчерашний незаконченный день не становится сегодняшним поражением."""
    backend, database, _, clock = reminder_backend
    clock.value = NOW - timedelta(days=1)
    await backend.settings(USER_ID, reminder_enabled=True)
    await backend.bootstrap(USER_ID)
    clock.value = NOW
    before = await profile_data(database)
    assert len(await backend.claim_due_reminders(NOW)) == 1
    assert await profile_data(database) == before


async def test_restarted_and_parallel_workers_cannot_claim_twice(
    reminder_backend,
) -> None:
    """Два экземпляра и перезапуск не повторяют неопределённую доставку."""
    backend, database, rules, clock = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    other = WordliStorage(
        database,
        rules,
        clock=lambda: clock.value,
        random_seconds=lambda: 600,
    )
    results = await asyncio.gather(
        backend.claim_due_reminders(NOW),
        other.claim_due_reminders(NOW),
    )
    assert sum(map(len, results)) == 1
    restarted = WordliStorage(
        database,
        rules,
        clock=lambda: clock.value,
        random_seconds=lambda: 600,
    )
    assert await restarted.claim_due_reminders(NOW + timedelta(minutes=1)) == []


async def test_retry_claims_are_bounded_to_three_attempts(reminder_backend) -> None:
    """Явный отказ 429 не создаёт бесконечную очередь повторов."""
    backend, database, _, _ = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    for attempt in range(3):
        now = NOW + timedelta(minutes=attempt)
        claim = (await backend.claim_due_reminders(now))[0]
        await backend.finish_reminder(
            USER_ID,
            claim["claim_token"],
            "retry",
            retry_at=now + timedelta(minutes=1),
        )
    assert await backend.claim_due_reminders(NOW + timedelta(minutes=3)) == []
    data = await reminder_data(database)
    assert data["reminder_attempts"] == 3
    assert data["reminder_status"] == "failed"


async def test_retry_outside_morning_window_is_not_sent(reminder_backend) -> None:
    """Длинный retry_after не переносит утреннее сообщение на позднее время."""
    backend, database, _, _ = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    claim = (await backend.claim_due_reminders(NOW))[0]
    await backend.finish_reminder(
        USER_ID,
        claim["claim_token"],
        "retry",
        retry_at=NOW + timedelta(minutes=11),
    )
    assert (await reminder_data(database))["reminder_status"] == "failed"
    assert await backend.claim_due_reminders(NOW + timedelta(minutes=11)) == []


async def test_late_start_skips_missed_morning_instead_of_catchup(
    reminder_backend,
) -> None:
    """Поздний запуск не отправляет напоминание за прошедшее утро."""
    backend, database, _, _ = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    assert (
        await backend.claim_due_reminders(NOW + timedelta(minutes=10, seconds=1)) == []
    )
    assert (await reminder_data(database))["reminder_status"] == "skipped"


async def test_blocked_account_is_disabled_persistently(reminder_backend) -> None:
    """Telegram403 сохраняет отписку и после перезапуска отправителя."""
    backend, _, _, _ = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    claim = (await backend.claim_due_reminders(NOW))[0]
    await backend.finish_reminder(USER_ID, claim["claim_token"], "blocked")
    assert (await backend.settings(USER_ID))["reminder_enabled"] is False
    assert await backend.claim_due_reminders(NOW + timedelta(days=1)) == []


async def test_next_local_day_can_receive_new_reminder(reminder_backend) -> None:
    """Вчерашняя подтверждённая отправка не блокирует новое утро."""
    backend, _, _, clock = reminder_backend
    await backend.settings(USER_ID, reminder_enabled=True)
    claim = (await backend.claim_due_reminders(NOW))[0]
    await backend.finish_reminder(USER_ID, claim["claim_token"], "sent")
    assert await backend.claim_due_reminders(NOW + timedelta(minutes=1)) == []
    clock.value = NOW + timedelta(days=1)
    assert len(await backend.claim_due_reminders(clock.value)) == 1


async def test_worker_passes_restricted_rollout_allowlist_to_storage() -> None:
    """Ограниченный запуск не отправляет напоминания вне списка доступа."""
    backend = storage()
    sender = AsyncMock(return_value=ReminderResult("sent"))
    worker = ReminderWorker(
        backend,
        sender,
        clock=lambda: NOW,
        sleep=AsyncMock(),
        allowed_user_ids={USER_ID},
    )
    assert await worker.run_once() == 1
    backend.claim_due_reminders.assert_awaited_with(
        NOW,
        limit=1,
        allowed_user_ids=frozenset({USER_ID}),
    )
    sender.assert_awaited_once_with(USER_ID)


async def test_worker_defensively_rejects_out_of_scope_claim() -> None:
    """Даже неверная заявка хранилища не обходит список доступа."""
    backend = storage()
    sender = AsyncMock()
    worker = ReminderWorker(
        backend,
        sender,
        clock=lambda: NOW,
        sleep=AsyncMock(),
        allowed_user_ids={USER_ID + 1},
    )
    assert await worker.run_once() == 1
    sender.assert_not_awaited()
    backend.finish_reminder.assert_awaited_once_with(
        USER_ID,
        "claim-one",
        "failed",
        retry_at=None,
    )


async def test_restricted_claim_does_not_touch_other_subscribers(
    reminder_backend,
) -> None:
    """Ограничение списка применяется до бронирования и изменения чужого утра."""
    backend, database, _, _ = reminder_backend
    for user_id in (USER_ID, USER_ID + 1):
        await backend.settings(user_id, reminder_enabled=True)
    before = await reminder_data(database, USER_ID + 1)
    result = await backend.claim_due_reminders(
        NOW,
        allowed_user_ids=frozenset({USER_ID}),
    )
    assert [item["user_id"] for item in result] == [USER_ID]
    assert await reminder_data(database, USER_ID + 1) == before
    assert await backend.claim_due_reminders(NOW, allowed_user_ids=set()) == []


async def _locale_users(database, rows: list[dict]) -> Table:
    """Создаёт только тестовые ID и языки без импорта моделей бота."""
    users = Table(
        "users",
        MetaData(),
        Column("user_id", BigInteger, primary_key=True),
        Column("locale", String(150)),
    )
    async with database.begin() as connection:
        await connection.run_sync(users.metadata.create_all)
        if rows:
            await connection.execute(users.insert(), rows)
    return users


@pytest.mark.parametrize("locale", ["ru", "RU", "ru-RU", "ru_RU"])
def test_russian_invitation_and_reminder_have_no_language_notice(
    locale: str,
) -> None:
    """Общий помощник сохраняет русский текст без вызова переводчика."""
    translate = Mock(side_effect=AssertionError("Перевод не нужен"))
    assert with_game_language_notice("Текст", locale, translate) == "Текст"
    translate.assert_not_called()


@pytest.mark.parametrize("locale", ["en", "en-US", "de", "fr"])
def test_non_russian_notice_has_exact_parentheses_and_spacing(
    locale: str,
) -> None:
    """Пометка отделена пустой строкой и не добавляет точку внутри скобок."""
    translate = Mock(
        return_value=(
            "Due to the game's mechanics, it is currently available only in Russian"
        )
    )
    assert with_game_language_notice("Translated text", locale, translate) == (
        "Translated text\n\n(Due to the game's mechanics, "
        "it is currently available only in Russian)"
    )
    translate.assert_called_once_with(GAME_LANGUAGE_NOTICE)


@pytest.mark.parametrize("locale", ["en", "de", "fr-FR", "en_US"])
async def test_sender_localizes_text_notice_and_back_without_aiogram(
    locale: str,
) -> None:
    """Неизвестный каталог заменяет EN, а не русским интерфейсом."""
    session = SimpleNamespace(post=Mock(return_value=Response(200, {"ok": True})))
    locale_reader = AsyncMock(return_value=locale)
    sender = TelegramReminderSender(session, TOKEN, ORIGIN, locale_reader=locale_reader)
    assert await sender.send(USER_ID) == ReminderResult("sent")
    payload = session.post.call_args.kwargs["json"]
    assert payload["text"] == (
        "Time to play 5БУКВ! A new word is waiting for you today.\n\n"
        "(Due to the game's mechanics, it is currently available only in Russian)"
    )
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0][0] == {"text": "5БУКВ", "web_app": {"url": ORIGIN}}
    assert rows[1][0] == {
        "text": "Back to main ↩️",
        "callback_data": "wordli_back_to_main",
    }
    locale_reader.assert_awaited_once_with(USER_ID)


async def test_concurrent_russian_and_english_do_not_share_markup() -> None:
    """Параллельные отправки не меняют чужой текст или подпись кнопки."""
    session = SimpleNamespace(post=Mock(return_value=Response(200, {"ok": True})))

    async def locale_reader(user_id: int) -> str:
        """Возвращает разные локали после переключения задач."""
        await asyncio.sleep(0)
        return "ru" if user_id == USER_ID else "en"

    sender = TelegramReminderSender(session, TOKEN, ORIGIN, locale_reader=locale_reader)
    assert await asyncio.gather(sender.send(USER_ID), sender.send(USER_ID + 1)) == [
        ReminderResult("sent"),
        ReminderResult("sent"),
    ]
    messages = {
        call.kwargs["json"]["chat_id"]: call.kwargs["json"]
        for call in session.post.call_args_list
    }
    russian, english = messages[USER_ID], messages[USER_ID + 1]
    assert russian["text"] == REMINDER_TEXT
    assert "(Due to" in english["text"]
    ru_keyboard = russian["reply_markup"]["inline_keyboard"]
    en_keyboard = english["reply_markup"]["inline_keyboard"]
    assert ru_keyboard[1][0]["text"] == "Назад на главную ↩️"
    assert en_keyboard[1][0]["text"] == "Back to main ↩️"
    assert ru_keyboard is not en_keyboard
    ru_keyboard[1][0]["text"] = "Проверка"
    assert en_keyboard[1][0]["text"] == "Back to main ↩️"


@pytest.mark.parametrize("locale", ["../../etc", "en/ru", "en\\ru", "", None])
async def test_sender_rejects_unsafe_injected_locale_before_catalog_lookup(
    locale: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Произвольный поставщик языка не передаёт пути в поиск каталога."""
    session = SimpleNamespace(post=Mock(return_value=Response(200, {"ok": True})))
    original = reminders.gettext.translation
    translation = Mock(side_effect=original)
    monkeypatch.setattr(reminders.gettext, "translation", translation)
    sender = TelegramReminderSender(
        session,
        TOKEN,
        ORIGIN,
        locale_reader=AsyncMock(return_value=locale),
    )
    assert await sender.send(USER_ID) == ReminderResult("sent")
    assert translation.call_args.kwargs["languages"] == ["ru"]
    assert session.post.call_args.kwargs["json"]["text"] == REMINDER_TEXT


async def test_sender_preserves_future_catalog_language_before_english_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Будущий немецкий каталог имеет приоритет перед английским резервом."""
    session = SimpleNamespace(post=Mock(return_value=Response(200, {"ok": True})))
    original = reminders.gettext.translation
    translation = Mock(side_effect=original)
    monkeypatch.setattr(reminders.gettext, "translation", translation)
    sender = TelegramReminderSender(
        session,
        TOKEN,
        ORIGIN,
        locale_reader=AsyncMock(return_value="de-DE"),
    )
    assert await sender.send(USER_ID) == ReminderResult("sent")
    assert translation.call_args.kwargs["languages"] == ["de_de", "de", "en"]
    assert session.post.call_args.kwargs["json"]["text"].startswith("Time to play")


@pytest.mark.parametrize("value", ["../ru", "en/ru", "en\\ru", "", "en " * 30])
def test_locale_normalization_rejects_paths_and_invalid_values(value: str) -> None:
    """Единая нормализация не допускает локали-пути в gettext."""
    with pytest.raises(ValueError):
        normalize_game_locale(value)


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("ru", "ru"),
        ("RU", "ru"),
        ("ru_RU", "ru"),
        ("ru-RU", "ru"),
        ("en", "en"),
        ("en_US", "en-us"),
        ("de", "de"),
        ("fr-FR", "fr-fr"),
        ("../en", "ru"),
        ("en/ru", "ru"),
        ("", "ru"),
        (None, "ru"),
    ],
)
async def test_storage_reads_only_database_locale(
    reminder_backend,
    locale: str | None,
    expected: str,
) -> None:
    """Сохранённый язык не заменяется FSM, Telegram или настройками игры."""
    backend, database, _, _ = reminder_backend
    await _locale_users(database, [{"user_id": USER_ID, "locale": locale}])
    assert await backend.user_locale(USER_ID) == expected
    assert await backend.user_locale(USER_ID + 1) == "ru"
    async with database.connect() as connection:
        assert await connection.scalar(select(profiles_table.c.user_id)) is None
        assert await connection.scalar(select(settings_table.c.user_id)) is None
    assert "users" not in wordli_metadata.tables


async def test_storage_missing_users_table_falls_back_with_safe_warning(
    reminder_backend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Самостоятельная игра не создаёт таблицу бота и не падает без неё."""
    backend, _, _, _ = reminder_backend
    assert await backend.user_locale(USER_ID) == "ru"
    assert "недоступны" in caplog.text
    assert TOKEN not in caplog.text
    assert "select users" not in caplog.text.lower()


@pytest.mark.parametrize(
    "code,fallback",
    [
        ("42501", True),
        ("42P01", True),
        ("08006", False),
        ("42703", False),
    ],
)
async def test_storage_locale_masks_only_missing_table_or_permission(
    reminder_backend,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: str,
    fallback: bool,
) -> None:
    """Сбой соединения или схемы не выдаётся за обычный русский язык."""
    backend, _, _, _ = reminder_backend
    original = Exception(f"private-database-detail-{TOKEN}")
    original.sqlstate = code
    error = DBAPIError("private-query", {}, original)

    @asynccontextmanager
    async def failed_session():
        """Подменяет только запрос языка без настоящей PostgreSQL."""
        yield SimpleNamespace(scalar=AsyncMock(side_effect=error))

    monkeypatch.setattr(backend, "_sessions", failed_session)
    if fallback:
        assert await backend.user_locale(USER_ID) == "ru"
        assert "недоступны" in caplog.text
    else:
        with pytest.raises(DBAPIError):
            await backend.user_locale(USER_ID)
    assert TOKEN not in caplog.text
    assert "private-query" not in caplog.text


async def test_sender_reloads_locale_after_user_changes_database_language(
    reminder_backend,
) -> None:
    """Каждое напоминание использует актуальную users.locale, не кэш FSM."""
    backend, database, _, _ = reminder_backend
    users = await _locale_users(database, [{"user_id": USER_ID, "locale": "ru"}])
    session = SimpleNamespace(post=Mock(return_value=Response(200, {"ok": True})))
    sender = TelegramReminderSender(
        session, TOKEN, ORIGIN, locale_reader=backend.user_locale
    )
    await sender.send(USER_ID)
    russian = session.post.call_args.kwargs["json"]
    async with database.begin() as connection:
        await connection.execute(update(users).values(locale="en"))
    await sender.send(USER_ID)
    english = session.post.call_args.kwargs["json"]
    assert russian["text"] == REMINDER_TEXT
    assert english["text"].startswith("Time to play")


async def test_locale_read_error_never_attempts_delivery_and_keeps_failure_semantics() -> (
    None
):
    """Неопределённая ошибка чтения языка не отправляет сообщение не на том языке."""
    session = SimpleNamespace(post=Mock())
    sender = TelegramReminderSender(
        session,
        TOKEN,
        ORIGIN,
        locale_reader=AsyncMock(side_effect=RuntimeError("offline read failure")),
    )
    backend = storage()
    worker = ReminderWorker(backend, sender.send, clock=lambda: NOW, sleep=AsyncMock())
    assert await worker.run_once() == 1
    session.post.assert_not_called()
    backend.finish_reminder.assert_awaited_once_with(
        USER_ID,
        "claim-one",
        "failed",
        retry_at=None,
    )
