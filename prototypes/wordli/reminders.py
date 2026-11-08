"""Отправка добровольных напоминаний без повторов неопределённых доставок."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import aiohttp

if TYPE_CHECKING:
    from .storage import WordliStorage

logger = logging.getLogger(__name__)
REMINDER_TEXT = "Пора сыграть в 5БУКВ! Сегодня вас ждёт новое слово."
POLL_INTERVAL = 15.0
SEND_INTERVAL = 0.1
MAX_BATCH_SIZE = 100


def _utc_now() -> datetime:
    """Возвращает текущее время с часовым поясом."""
    return datetime.now(timezone.utc)


def parse_reminders_flag(value: str) -> bool:
    """Принимает только явные true/false без неявного включения рассылки."""
    if not isinstance(value, str) or value not in {"true", "false"}:
        raise ValueError("WORDLI_REMINDERS_ENABLED должен быть true или false")
    return value == "true"


@dataclass(frozen=True, slots=True)
class ReminderResult:
    """Описывает подтверждённый результат одной отправки."""

    outcome: Literal["sent", "blocked", "retry", "failed"]
    retry_after: int | None = None


class TelegramReminderSender:
    """Посылает только текст через переданную HTTP-сессию."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        """Не создаёт сессию и не выполняет сетевых запросов."""
        self._session = session
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"

    async def send(self, user_id: int) -> ReminderResult:
        """Не повторяет отправку, если Telegram мог уже принять сообщение."""
        try:
            async with self._session.post(
                self._url,
                json={"chat_id": user_id, "text": REMINDER_TEXT},
                allow_redirects=False,
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if response.status == 200 and body.get("ok") is True:
                    return ReminderResult("sent")
                if response.status == 403:
                    return ReminderResult("blocked")
                if response.status == 429:
                    parameters = body.get("parameters", {})
                    delay = (
                        parameters.get("retry_after")
                        if isinstance(parameters, dict)
                        else None
                    )
                    if type(delay) is not int or not 1 <= delay <= 86400:
                        delay = 60
                    return ReminderResult("retry", retry_after=delay)
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
            # При потере ответа неизвестно, было ли сообщение доставлено.
            return ReminderResult("failed")
        return ReminderResult("failed")


class ReminderWorker:
    """Обрабатывает сохранённые разрешения и заявки по одной за раз."""

    def __init__(
        self,
        storage: WordliStorage,
        sender: Callable[[int], Awaitable[ReminderResult]],
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = POLL_INTERVAL,
        send_interval: float = SEND_INTERVAL,
        batch_size: int = MAX_BATCH_SIZE,
        allowed_user_ids: set[int] | frozenset[int] | None = None,
    ) -> None:
        """Позволяет проверять время и отправку без внешних служб."""
        if poll_interval <= 0 or send_interval < 0:
            raise ValueError("Неверный интервал напоминаний")
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError("Неверный размер очереди напоминаний")
        self._storage = storage
        self._sender = sender
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._send_interval = send_interval
        self._batch_size = batch_size
        self._allowed_user_ids = (
            None if allowed_user_ids is None else frozenset(allowed_user_ids)
        )

    def _now(self) -> datetime:
        """Не позволяет передать хранилищу время без часового пояса."""
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("Часы напоминаний должны включать часовой пояс")
        return value.astimezone(timezone.utc)

    async def run_once(self) -> int:
        """Повторно проверяет срок и согласие перед каждой новой заявкой."""
        processed = 0
        for _ in range(self._batch_size):
            if self._allowed_user_ids is None:
                claims = await self._storage.claim_due_reminders(self._now(), limit=1)
            else:
                claims = await self._storage.claim_due_reminders(
                    self._now(),
                    limit=1,
                    allowed_user_ids=self._allowed_user_ids,
                )
            if not claims:
                break
            claim = claims[0]
            if (
                self._allowed_user_ids is not None
                and claim["user_id"] not in self._allowed_user_ids
            ):
                await self._storage.finish_reminder(
                    claim["user_id"],
                    claim["claim_token"],
                    "failed",
                    retry_at=None,
                )
                processed += 1
                continue
            try:
                result = await self._sender(claim["user_id"])
            except asyncio.CancelledError:
                # Нельзя повторять неопределённую доставку после перезапуска.
                raise
            except Exception:
                logger.warning("Не удалось подтвердить отправку напоминания")
                result = ReminderResult("failed")
            retry_at = (
                self._now() + timedelta(seconds=result.retry_after or 60)
                if result.outcome == "retry"
                else None
            )
            await self._storage.finish_reminder(
                claim["user_id"],
                claim["claim_token"],
                result.outcome,
                retry_at=retry_at,
            )
            processed += 1
            if self._send_interval:
                await self._sleep(self._send_interval)
        return processed

    async def run(self) -> None:
        """Продолжает опрос после временной ошибки, не раскрывая секреты."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Не удалось проверить очередь напоминаний")
            await self._sleep(self._poll_interval)


@asynccontextmanager
async def reminder_service(
    storage: WordliStorage,
    token: str,
    *,
    allowed_user_ids: set[int] | frozenset[int] | None = None,
) -> AsyncIterator[None]:
    """Закрывает задачу до HTTP-сессии и внешнего пула БД."""
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10, connect=5)
    ) as session:
        sender = TelegramReminderSender(session, token)
        worker = ReminderWorker(
            storage,
            sender.send,
            allowed_user_ids=allowed_user_ids,
        )
        task = asyncio.create_task(worker.run(), name="wordli-reminders")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
