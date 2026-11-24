"""Хранилище партий КВОРДЛИ в отдельной базе PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from .engine import GameError, QuordleEngine

TEST_DATABASE = "copilot_quordle_test"

SCHEMA = """
-- профили, рейтинг и настройки принадлежат только квордли
create table if not exists quordle_players (
    user_id bigint primary key,
    display_name text not null,
    locale text not null default 'ru',
    game_data jsonb not null default '{}',
    revision bigint not null default 0,
    rating integer,
    played integer not null default 0,
    reminders_enabled boolean not null default false,
    timezone_name text not null default 'Europe/Moscow',
    reminded_on date
);
create index if not exists quordle_rating_idx
    on quordle_players (rating desc, played desc) where rating is not null;
"""


def validate_timezone(value: Any) -> str:
    """Принимает только известное имя часового пояса IANA."""
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise GameError("invalid_timezone", "Неизвестный часовой пояс.")
    try:
        return ZoneInfo(value).key
    except (ValueError, ZoneInfoNotFoundError):
        raise GameError("invalid_timezone", "Неизвестный часовой пояс.") from None


@dataclass(frozen=True, slots=True)
class Player:
    """Минимальные сведения из подписанного Telegram-профиля."""

    user_id: int
    name: str
    locale: str = "ru"


@dataclass(frozen=True, slots=True)
class ReminderClaim:
    """Связывает ответ Telegram с одной попыткой утренней рассылки."""

    user_id: int
    locale: str
    day: date
    token: str


def _reminder_day(now: datetime, user_id: int, zone: str) -> date | None:
    """Раскладывает личные слоты по утреннему окну в местном часовом поясе."""
    if now.utcoffset() is None:
        raise ValueError("Для напоминаний нужны часы с часовым поясом.")
    try:
        local = now.astimezone(ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError):
        return None
    # Смещение стабильно для игрока и даты даже после перезапуска сервера.
    # Последняя группа открывается в 06:08, оставляя две минуты запаса.
    offset = (user_id + local.date().toordinal()) % 19
    due = datetime.combine(local.date(), time(5, 50), local.tzinfo)
    if due + timedelta(minutes=offset) <= local <= due + timedelta(minutes=20):
        return local.date()
    return None


class RevisionConflict(GameError):
    """Передаёт актуальное состояние при повторном или устаревшем запросе."""

    def __init__(self, state: dict[str, Any]) -> None:
        """Сохраняет актуальные данные для восстановления экрана."""
        super().__init__("revision_conflict", "Партия обновилась.", 409)
        self.state = state


class Storage:
    """Сериализует изменения профиля и сохраняет общие ходы атомарно."""

    def __init__(self, pool: asyncpg.Pool, engine: QuordleEngine) -> None:
        """Принимает выделенное подключение и игровой движок."""
        self.pool = pool
        self.engine = engine

    async def initialise_test_database(self) -> None:
        """Создаёт таблицы только в явно выделенной тестовой базе."""
        async with self.pool.acquire() as connection, connection.transaction():
            database = await connection.fetchval("select current_database()")
            if database != TEST_DATABASE:
                raise RuntimeError("Схема доступна только тестовой базе КВОРДЛИ.")
            await connection.execute("select pg_advisory_xact_lock(52741368)")
            await connection.execute(SCHEMA)

    @staticmethod
    def settings_state(row: asyncpg.Record) -> dict[str, Any]:
        """Преобразует настройки БД в формат интерфейса."""
        return {
            "enabled": row["reminders_enabled"],
            "timezone": row["timezone_name"],
        }

    async def operate(
        self,
        player: Player,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Загружает или меняет одну игровую партию под блокировкой."""
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                -- создаём профиль при первом открытии и поддерживаем имя
                insert into quordle_players (user_id, display_name, locale)
                values ($1, $2, $3)
                on conflict (user_id) do update
                    set display_name = excluded.display_name, locale = excluded.locale
                """,
                player.user_id,
                player.name,
                player.locale,
            )
            row = await connection.fetchrow(
                "select * from quordle_players where user_id = $1 for update",
                player.user_id,
            )
            game_data = row["game_data"]
            # Служебная заявка не превращает пустой игровой профиль в начатый.
            reminder = game_data.pop("reminder", None)
            today = datetime.now(timezone.utc).date()
            revision = row["revision"]
            settings = self.settings_state(row)
            request_id = payload.get("request_id")
            if request_id and game_data.get("last_request") == [operation, request_id]:
                return self.engine.public_state(game_data, today) | {
                    "revision": revision, "settings": settings,
                }
            if operation == "state":
                previous_count = len(game_data.get("history", []))
                state = self.engine.bootstrap(game_data, today)
                revision += len(game_data["history"]) != previous_count
            elif (
                type(payload.get("revision")) is not int
                or payload["revision"] != revision
            ):
                raise RevisionConflict(
                    self.engine.public_state(game_data, today)
                    | {"revision": revision, "settings": settings}
                )
            elif operation == "guess":
                state = self.engine.submit(game_data, payload.get("word"), today)
                revision += 1
            elif operation == "draft":
                previous = self.engine.public_state(game_data, today)
                state = self.engine.set_draft(game_data, payload.get("draft"), today)
                revision += state["game"]["draft"] != previous["game"]["draft"]
            else:
                state = self.engine.next_game(game_data, today)
                revision += 1
            if request_id:
                game_data["last_request"] = [operation, request_id]
            stats = state["stats"]
            if reminder is not None:
                game_data["reminder"] = reminder
            await connection.execute(
                """
                -- игровой снимок и таблица результатов обновляются вместе
                update quordle_players
                set game_data = $2, revision = $3, rating = $4, played = $5
                where user_id = $1
                """,
                player.user_id,
                game_data,
                revision,
                stats["score"],
                stats["played"],
            )
            return state | {"revision": revision, "settings": settings}

    async def get_definition_permission(self, user_id: int, word: str) -> bool:
        """Проверяет доступ к раскрытому ответу текущего результата игрока."""
        game_data = await self.pool.fetchval(
            "select game_data from quordle_players where user_id = $1",
            user_id,
        )
        return bool(game_data and self.engine.can_define(game_data, word))

    async def update_settings(
        self, player: Player, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Читает или меняет добровольные напоминания и локальные часы."""
        enabled = payload.get("enabled")
        if "enabled" in payload and type(enabled) is not bool:
            raise GameError("invalid_settings", "Неверная настройка напоминаний.")
        zone = validate_timezone(payload["timezone"]) if "timezone" in payload else None
        if "timezone" not in payload and "enabled" not in payload:
            row = await self.pool.fetchrow(
                "select * from quordle_players where user_id = $1",
                player.user_id,
            )
            if row is None:
                return {"enabled": False, "timezone": "Europe/Moscow"}
            return self.settings_state(row)
        row = await self.pool.fetchrow(
            """
            -- изменение предпочтений не меняет черновик и ревизию партии
            insert into quordle_players
                (user_id, display_name, locale, reminders_enabled, timezone_name)
            values ($1, $2, $3, coalesce($4, false), coalesce($5, 'Europe/Moscow'))
            on conflict (user_id) do update set
                reminders_enabled = coalesce($4, quordle_players.reminders_enabled),
                timezone_name = coalesce($5, quordle_players.timezone_name)
            returning reminders_enabled, timezone_name
            """,
            player.user_id,
            player.name,
            player.locale,
            enabled,
            zone,
        )
        return self.settings_state(row)

    async def rating(self, user_id: int) -> list[dict[str, Any]]:
        """Возвращает топ-10 и позицию игрока вне топа."""
        rows = await self.pool.fetch(
            """
            -- одинаковые рейтинг и число партий дают одно место
            with ranked as (
                select
                    user_id,
                    display_name,
                    played,
                    rating,
                    dense_rank() over (order by rating desc, played desc) as rank
                from quordle_players
                where rating is not null and user_id > 0
            ),

            top_players as (
                select user_id
                from ranked
                order by rating desc, played desc, user_id
                limit 10
            )
            select rank, display_name as name, played, rating as score, user_id = $1 as is_self
            from ranked
            where user_id = $1 or user_id in (select user_id from top_players)
            order by rating desc, played desc, user_id
            """,
            user_id,
        )
        return list(map(dict, rows))

    async def reminder_candidates(
        self, now: datetime, allowed: set[int] | None
    ) -> list[int]:
        """Выбирает кандидатов без резервирования и без чужих тестеров."""
        players = await self.pool.fetch(
            """
            select user_id, timezone_name from quordle_players
            where reminders_enabled and user_id > 0
                and ($1::bigint[] is null or user_id = any($1))
            order by user_id
            """,
            None if allowed is None else sorted(allowed),
        )
        return [
            p["user_id"] for p in players
            if _reminder_day(now, p["user_id"], p["timezone_name"])
        ]

    async def claim_reminder(
        self, user_id: int, now: datetime, allowed: set[int] | None
    ) -> ReminderClaim | None:
        """Перед отправкой атомарно резервирует одного согласившегося игрока."""
        if user_id <= 0 or (allowed is not None and user_id not in allowed):
            return None
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                select locale, timezone_name, game_data, reminded_on
                from quordle_players where user_id = $1 and reminders_enabled
                for update
                """,
                user_id,
            )
            if not row:
                return None
            day = _reminder_day(now, user_id, row["timezone_name"])
            if not day or (row["reminded_on"] and row["reminded_on"] >= day):
                return None
            profile = row["game_data"]
            utc_day = now.astimezone(timezone.utc).date()
            if self.engine.first_game_completed(profile, utc_day):
                return None
            previous = profile.get("reminder", {})
            attempts = 0
            if previous.get("day", "") > day.isoformat():
                return None
            if previous.get("day") == day.isoformat():
                attempts = previous["attempts"]
                if (
                    previous["status"] != "retry" or attempts >= 3
                    or previous["retry_at"] > now.timestamp()
                ):
                    return None
            claim = ReminderClaim(user_id, row["locale"], day, uuid4().hex)
            await conn.execute(
                """
                update quordle_players
                set game_data = jsonb_set(game_data, '{reminder}', $2::jsonb)
                where user_id = $1
                """,
                user_id,
                {"day": day.isoformat(), "token": claim.token,
                 "status": "claimed", "attempts": attempts + 1},
            )
            return claim

    async def finish_reminder(
        self, claim: ReminderClaim, status: str, retry_at: datetime | None = None
    ) -> None:
        """Записывает доставку только при подтверждении именно этого запроса."""
        if status not in {"sent", "retry", "blocked", "failed"}:
            raise ValueError("Неизвестный исход напоминания.")
        if status == "retry" and retry_at is None:
            raise ValueError("Не указан срок повторной попытки.")
        await self.pool.execute(
            """
            update quordle_players
            set game_data = jsonb_set(
                    game_data, '{reminder}', (game_data -> 'reminder') || $3::jsonb
                ),
                reminded_on = case when $4 then $5::date else reminded_on end,
                reminders_enabled = reminders_enabled and not $6
            where user_id = $1 and game_data -> 'reminder' ->> 'token' = $2
                and game_data -> 'reminder' ->> 'status' = 'claimed'
            """,
            claim.user_id, claim.token,
            {"status": status, "retry_at": retry_at.timestamp() if retry_at else None},
            status == "sent", claim.day, status == "blocked",
        )
