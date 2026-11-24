"""Проверяет напоминания КВОРДЛИ без отправки сообщений людям."""

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from dotenv import dotenv_values

from .engine import QuordleEngine
from .server import (
    NOTICE,
    PG_FILE,
    initialise_connection,
    notify_due,
    notify_players,
)
from .storage import TEST_DATABASE, Player, ReminderClaim, Storage, _reminder_day

NOW = datetime(2026, 11, 24, 6, 9, 30, tzinfo=timezone.utc)
ORIGIN = "https://games.example.org/"


class ReminderTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет расписание и различает ответы Telegram без внешних служб."""

    def test_slots_dst_and_date(self) -> None:
        """Окно следует местным часам при переходе DST и смене суток."""
        for name, month in (("Europe/Berlin", 1), ("Europe/Berlin", 7),
                            ("Pacific/Auckland", 1)):
            with self.subTest(zone=name, month=month):
                zone = ZoneInfo(name)
                start = datetime(2026, month, 4, 5, 50, tzinfo=zone)
                for minute in (-1, 0, 17, 18, 19, 20, 21):
                    instant = (start + timedelta(minutes=minute)).astimezone(timezone.utc)
                    due = [_reminder_day(instant, uid, name) for uid in range(1, 20)]
                    expected = min(19, minute + 1) if 0 <= minute <= 20 else 0
                    self.assertEqual(due.count(start.date()), expected)
        self.assertIsNone(_reminder_day(NOW, 42, "Invalid/Missing"))

    async def test_poll_interval_includes_processing(self) -> None:
        """Длительность обхода вычитается, а задержка никогда не отрицательна."""
        loop = Mock(time=Mock(side_effect=[0, 3, 15, 32, 32, 40]))
        notify = AsyncMock()
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        with (
            patch("prototypes.quordle.server.notify_due", new=notify),
            patch("asyncio.get_running_loop", return_value=loop),
            patch("asyncio.sleep", new=sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await notify_players(AsyncMock(), AsyncMock(), ORIGIN)
        self.assertEqual(notify.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [12, 0, 7])

    async def test_delivery_outcomes(self) -> None:
        """Успех, явный отказ и потеря ответа не приравниваются к доставке."""
        method = SendMessage(chat_id=42, text="Пример")
        for error, status in (
            (None, "sent"),
            (TelegramRetryAfter(method, "rate limit", 90), "retry"),
            (TelegramForbiddenError(method, "blocked"), "blocked"),
            (TimeoutError(), "failed"),
        ):
            with self.subTest(status=status):
                storage, bot = AsyncMock(), AsyncMock()
                claim = ReminderClaim(42, "en", NOW.date(), "synthetic")
                storage.reminder_candidates.return_value = [42]
                storage.claim_reminder.return_value = claim
                bot.send_message.side_effect = error
                await notify_due(storage, bot, ORIGIN, {42}, lambda: NOW)
                storage.claim_reminder.assert_awaited_once_with(42, NOW, {42})
                args = (claim, status)
                if status == "retry":
                    args += (NOW + timedelta(seconds=90),)
                storage.finish_reminder.assert_awaited_once_with(*args)
                self.assertEqual(bot.send_message.await_args.args, (42, NOTICE["en"][0]))
        self.assertEqual(set(NOTICE), {"ru", "en", "es", "uk"})
        self.assertFalse(any("(" in text for text, _ in NOTICE.values()))

    async def test_cancel_does_not_claim_remaining_players(self) -> None:
        """При выключении неопределённым остаётся только начатый запрос."""
        storage, bot = AsyncMock(), AsyncMock()
        storage.reminder_candidates.return_value = [42, 43]
        storage.claim_reminder.return_value = ReminderClaim(42, "ru", NOW.date(), "claim")
        bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await notify_due(storage, bot, ORIGIN, {42, 43}, lambda: NOW)
        storage.claim_reminder.assert_awaited_once_with(42, NOW, {42, 43})
        storage.finish_reminder.assert_not_awaited()


@unittest.skipUnless(os.getenv("QUORDLE_RUN_DB_TESTS") == "1", "Требуется тестовая БД")
class DatabaseReminderTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет настоящие запросы только во временной таблице тестовой базы."""

    async def asyncSetUp(self) -> None:
        """Открывает один сеанс со своей временной копией структуры таблицы."""
        pg = dotenv_values(PG_FILE, interpolate=False)
        self.conn = await asyncpg.connect(
            host="127.0.0.1", port=55492, database=TEST_DATABASE, user="quordle_test",
            password=pg["POSTGRES_PASSWORD"], timeout=4,
        )
        self.addAsyncCleanup(self.conn.close)
        self.assertEqual(await self.conn.fetchval("select current_database()"), TEST_DATABASE)
        await initialise_connection(self.conn)
        await self.conn.execute(
            "create temporary table quordle_players "
            "(like public.quordle_players including all)"
        )
        await self.conn.execute("set search_path = pg_temp, pg_catalog")
        self.assertTrue(await self.conn.fetchval(
            "select relpersistence = 't' from pg_class "
            "where oid = 'quordle_players'::regclass"
        ))

        @asynccontextmanager
        async def acquire():
            """Предоставляет только изолированный сеанс теста."""
            yield self.conn

        pool = AsyncMock(acquire=acquire, execute=self.conn.execute, fetch=self.conn.fetch)
        self.storage = Storage(pool, QuordleEngine())
        self.user = Player(42, "Тест")
        await self.conn.execute(
            """
            insert into quordle_players (user_id, display_name, timezone_name, reminders_enabled)
            values (42, 'Тест', 'UTC', true), (43, 'Нет допуска', 'UTC', true),
                (44, 'Нет согласия', 'UTC', false)
            """
        )

    async def test_claim_retry_and_delivery(self) -> None:
        """Поздний ответ, новый запуск и выключение не создают повторов."""
        storage = self.storage
        allowed = {42, 44}
        self.assertEqual(await storage.reminder_candidates(NOW, allowed), [42])
        self.assertEqual(await storage.reminder_candidates(NOW, set()), [])
        for uid in (43, 44):
            self.assertIsNone(await storage.claim_reminder(uid, NOW, allowed))
        first = await storage.claim_reminder(42, NOW, allowed)
        self.assertIsNotNone(first)
        self.assertIsNone(await storage.claim_reminder(42, NOW, allowed))
        mark = "select reminded_on from quordle_players where user_id = 42"
        self.assertIsNone(await self.conn.fetchval(mark))
        # Профиль содержит только заявку: первое открытие игры всё равно работает.
        state = await storage.operate(self.user, "state", {})
        self.assertNotIn("reminder", state)
        self.assertIsNone(await storage.claim_reminder(42, NOW, allowed))
        later = NOW + timedelta(seconds=10)
        await storage.finish_reminder(first, "retry", later)
        self.assertIsNone(await storage.claim_reminder(42, NOW, allowed))
        second = await storage.claim_reminder(42, later, allowed)
        self.assertIsNotNone(second)
        await storage.finish_reminder(first, "sent")
        self.assertIsNone(await self.conn.fetchval(mark))
        await storage.finish_reminder(second, "sent")
        self.assertEqual(await self.conn.fetchval(mark), NOW.date())
        self.assertIsNone(await storage.claim_reminder(42, later, allowed))

    async def test_no_repeat_after_failure_or_outside_window(self) -> None:
        """Сетевой сбой не повторяется, запрос 429 ограничен тремя попытками."""
        for i in range(3):
            claim = await self.storage.claim_reminder(42, NOW, None)
            self.assertIsNotNone(claim, i)
            await self.storage.finish_reminder(claim, "retry", NOW)
        self.assertIsNone(await self.storage.claim_reminder(42, NOW, None))
        tomorrow = NOW + timedelta(days=1)
        self.assertIsNone(await self.storage.claim_reminder(42, tomorrow + timedelta(hours=1), None))
        claim = await self.storage.claim_reminder(42, tomorrow, None)
        await self.storage.finish_reminder(claim, "failed")
        self.assertIsNone(await self.storage.claim_reminder(42, tomorrow, None))
        next_day = tomorrow + timedelta(days=1)
        claim = await self.storage.claim_reminder(42, next_day, None)
        await self.storage.finish_reminder(claim, "blocked")
        self.assertFalse(await self.conn.fetchval(
            "select reminders_enabled from quordle_players where user_id = 42"
        ))
        self.assertIsNone(await self.conn.fetchval(
            "select reminded_on from quordle_players where user_id = 42"
        ))

    async def test_consent_and_utc_game_rechecked_before_send(self) -> None:
        """Отзыв согласия и сыгранная UTC-партия отменяют выбранное сообщение."""
        await self.conn.execute(
            "update quordle_players set timezone_name = 'Pacific/Auckland' where user_id = 42"
        )
        local = NOW.replace(tzinfo=ZoneInfo("Pacific/Auckland"))
        utc = local.astimezone(timezone.utc)
        self.assertNotEqual(utc.date(), local.date())
        self.assertEqual(await self.storage.reminder_candidates(utc, {42}), [42])
        await self.conn.execute(
            "update quordle_players set reminders_enabled = false where user_id = 42"
        )
        self.assertIsNone(await self.storage.claim_reminder(42, utc, {42}))
        profile = {}
        self.storage.engine.bootstrap(profile, utc.date())
        for answer in profile["history"][-1]["answers"]:
            state = self.storage.engine.submit(profile, answer, utc.date())
        self.assertEqual(state["game"]["status"], "won")
        await self.conn.execute(
            "update quordle_players set game_data = $1::jsonb, reminders_enabled = true "
            "where user_id = 42",
            profile,
        )
        self.assertIsNone(await self.storage.claim_reminder(42, utc, {42}))
