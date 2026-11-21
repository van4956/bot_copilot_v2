"""Хранилище партий КВОРДЛИ в отдельной базе PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any
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
        """Разрешает определения только для разгаданных слов текущего результата."""
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

    async def claim_reminders(self, now: datetime) -> list[asyncpg.Record]:
        """Не резервирует повторное уведомление на ту же местную дату."""
        players = await self.pool.fetch(
            """
            select user_id, locale, timezone_name, reminded_on
            from quordle_players
            where reminders_enabled and user_id > 0
            """
        )
        result = []
        for player in players:
            local = now.astimezone(ZoneInfo(player["timezone_name"]))
            if not time(5, 50) <= local.time() < time(6, 11):
                continue
            if player["reminded_on"] == local.date():
                continue
            async with self.pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    "select * from quordle_players where user_id = $1 for update",
                    player["user_id"],
                )
                if (
                    not row["reminders_enabled"]
                    or row["timezone_name"] != player["timezone_name"]
                    or row["reminded_on"] == local.date()
                ):
                    continue
                if self.engine.first_game_completed(row["game_data"], now.date()):
                    continue
                await connection.execute(
                    """
                    update quordle_players
                    set reminded_on = $2
                    where user_id = $1
                    """,
                    player["user_id"],
                    local.date(),
                )
                result.append(player)
        return result
