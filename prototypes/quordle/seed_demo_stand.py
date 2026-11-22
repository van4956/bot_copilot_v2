"""Добавляет вымышленных участников, партии и рецепты в локальный стенд."""

from __future__ import annotations

import asyncio
import random
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import dotenv_values
from sqlalchemy import insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config_data.config import ROOT, Config
from database.models import Cookbook, CookbookEnglish, Users
from database.orm_cookbook_translations import recipe_source_hash
from prototypes.wordli.engine import LocalGameEngine
from prototypes.wordli.storage import WordliStorage, profiles_table, settings_table

from .demo_bot import DemoError, load_test_identity
from .demo_cookbook_data import DEMO_AUTHOR, generate_demo_recipes
from .demo_histories import make_quordle_profile, make_wordli_profile
from .engine import QuordleEngine
from .full_test_bot import load_isolated_config
from .server import PG_FILE, initialise_connection
from .storage import TEST_DATABASE, Storage

SEED = 20261122
ID_BASE = 4_450_112_200_000_000
ALIASES = (
    "Астра", "Бриз", "Вега", "Гранит", "Дюна", "Зефир", "Искра", "Кедр",
    "Лис", "Маяк", "Неон", "Оникс", "Панда", "Риф", "Сова", "Янтарь",
)


@dataclass(frozen=True, slots=True)
class DemoPlayer:
    """Запись синтетического игрока без настоящего Telegram-аккаунта."""

    user_id: int
    username: str
    name: str
    locale: str


def demo_players() -> list[DemoPlayer]:
    """Возвращает один воспроизводимый набор вымышленных пользователей."""
    rng = random.Random(SEED)
    names = list(ALIASES)
    rng.shuffle(names)
    return [
        DemoPlayer(
            ID_BASE + number, f"local_wordgames_{number:02d}",
            f"Тест · {alias}", rng.choice(("ru", "ru", "en", "es", "uk")),
        )
        for number, alias in enumerate(names, 1)
    ]


def local_sqlite(config: Config) -> str:
    """Разрешает только существующую локальную SQLite тестового Copilot."""
    url = make_url(config.database_url)
    folder = (ROOT / "local_testing/data").resolve()
    path = Path(url.database or "").resolve()
    if (
        not config.test_mode
        or url.drivername != "sqlite+aiosqlite"
        or url.query
        or folder not in path.parents
        or not path.is_file()
    ):
        raise DemoError("Для фикстур нужна существующая локальная тестовая SQLite.")
    return config.database_url


async def add_recipes(session: AsyncSession) -> int:
    """Добавляет новые учебные рецепты с переводами, не заменяя старые."""
    present = set(await session.scalars(
        select(Cookbook.recipe_name).where(Cookbook.author == DEMO_AUTHOR)
    ))
    missing = [
        item for item in generate_demo_recipes(SEED) if item.recipe_name not in present
    ]
    for item in missing:
        recipe = Cookbook(
            recipe_name=item.recipe_name, description=item.description,
            author=item.author, price=item.price, image=item.image,
        )
        session.add(recipe)
        await session.flush()
        session.add(CookbookEnglish(
            recipe_id=recipe.recipe_id, recipe_name=item.recipe_name_en,
            description=item.description_en,
            source_hash=recipe_source_hash(item.recipe_name, item.description),
            model="local-random-demo",
        ))
    return len(missing)


async def seed_sqlite(
    url: str, players: list[DemoPlayer], today: date
) -> dict[str, int]:
    """Записывает рецепты, карточки пользователей и истории 5БУКВ."""
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    rules = LocalGameEngine()
    counts = dict(recipes=0, users=0, wordli_profiles=0, wordli_games=0)
    try:
        async with sessions() as session, session.begin():
            for player in players:
                user = await session.get(Users, player.user_id)
                if user is None:
                    session.add(Users(
                        user_id=player.user_id, user_name=player.username,
                        full_name=player.name, locale=player.locale,
                        status="active", flag=0,
                    ))
                    counts["users"] += 1
                elif user.user_name != player.username:
                    raise DemoError("ID фикстуры уже занят в тестовой SQLite.")
                history = await session.scalar(select(profiles_table.c.history).where(
                    profiles_table.c.user_id == player.user_id
                ))
                settings = await session.scalar(select(settings_table.c.data).where(
                    settings_table.c.user_id == player.user_id
                ))
                if history is None and settings is None:
                    rng = random.Random(SEED ^ player.user_id)
                    profile = make_wordli_profile(rules, rng, player.user_id, today)
                    history = profile["history"]
                    await session.execute(insert(profiles_table).values(
                        user_id=player.user_id, **profile
                    ))
                    await session.execute(insert(settings_table).values(
                        user_id=player.user_id,
                        data={"name": player.name, "reminder_enabled": False},
                    ))
                    counts["wordli_profiles"] += 1
                elif not settings or settings.get("name") != player.name:
                    raise DemoError("ID фикстуры уже занят в профилях 5БУКВ.")
                stats = rules.statistics(history, today=today)
                if stats["played"] < 5 or stats["score"] is None:
                    raise DemoError("Синтетическая история 5БУКВ не прошла проверку.")
                counts["wordli_games"] += stats["played"]
            counts["recipes"] = await add_recipes(session)
        leaderboard = await WordliStorage(engine, rules).leaderboard(players[0].user_id)
        if not leaderboard["self_eligible"] or leaderboard["total"] < len(players):
            raise DemoError("Синтетические участники не попали в рейтинг 5БУКВ.")
        return counts
    finally:
        await engine.dispose()


async def seed_quordle(
    pool: asyncpg.Pool, players: list[DemoPlayer], today: date
) -> dict[str, int]:
    """Записывает личные партии КВОРДЛИ и проверяет штатную выдачу рейтинга."""
    rules = QuordleEngine()
    storage = Storage(pool, rules)
    counts = dict(quordle_profiles=0, quordle_games=0)
    async with pool.acquire() as connection, connection.transaction():
        for player in players:
            row = await connection.fetchrow(
                """
                select display_name, game_data, played, rating
                from quordle_players
                where user_id = $1
                """,
                player.user_id,
            )
            if row is None:
                rng = random.Random(SEED + player.user_id)
                profile = make_quordle_profile(rules, rng, today)
                stats = rules.stats(profile, today)
                await insert_quordle(connection, player, profile, stats)
                counts["quordle_profiles"] += 1
            else:
                if row["display_name"] != player.name:
                    raise DemoError("ID фикстуры уже занят в профилях КВОРДЛИ.")
                stats = rules.stats(row["game_data"], today)
                if row["played"] != stats["played"] or row["rating"] != stats["score"]:
                    raise DemoError("Сохранённый рейтинг КВОРДЛИ не совпадает с историей.")
            if stats["played"] < 5 or stats["score"] is None:
                raise DemoError("Синтетическая история КВОРДЛИ не прошла проверку.")
            counts["quordle_games"] += stats["played"]
    rating = await storage.rating(players[0].user_id)
    if not any(row["is_self"] for row in rating):
        raise DemoError("Синтетический участник не попал в рейтинг КВОРДЛИ.")
    return counts


async def insert_quordle(
    connection: asyncpg.Connection,
    player: DemoPlayer,
    profile: dict[str, Any],
    stats: dict[str, Any],
) -> None:
    """Записывает историю и рассчитанный движком рейтинг, без напоминаний."""
    await connection.execute(
        """
        insert into quordle_players (
            user_id, display_name, locale, game_data, revision, rating, played,
            reminders_enabled
        )
        values ($1, $2, $3, $4, $5, $6, $7, false)
        """,
        player.user_id, player.name, player.locale, profile,
        len(profile["history"]), stats["score"], stats["played"],
    )


async def seed_stand() -> dict[str, int]:
    """Проверяет обе локальные базы перед запуском одного учебного набора."""
    token, bot_id, allowed = load_test_identity()
    sqlite_url = local_sqlite(load_isolated_config(token, bot_id))
    players = demo_players()
    if allowed.intersection(player.user_id for player in players):
        raise DemoError("ID учебного набора пересекается с тестовым доступом.")
    config = dotenv_values(PG_FILE, interpolate=False)
    secret = config.get("POSTGRES_PASSWORD") or ""
    if (
        config.get("POSTGRES_DB") != TEST_DATABASE
        or config.get("POSTGRES_USER") != "quordle_test"
        or not re.fullmatch(r"[a-f0-9]{64}", secret)
    ):
        raise DemoError("Параметры локального контейнера КВОРДЛИ не подтверждены.")
    today = datetime.now(timezone.utc).date()
    async with asyncpg.create_pool(
        host="127.0.0.1", port=55492, database=TEST_DATABASE,
        user="quordle_test", password=secret, init=initialise_connection,
        min_size=1, max_size=2, timeout=8, command_timeout=12,
    ) as pool:
        verified = await pool.fetchval(
            "select current_database() = $1 and current_user = 'quordle_test'",
            TEST_DATABASE,
        )
        if not verified:
            raise DemoError("Подключение не ведёт в тестовую базу КВОРДЛИ.")
        wordli = await seed_sqlite(sqlite_url, players, today)
        quordle = await seed_quordle(pool, players, today)
    return wordli | quordle


def main() -> int:
    """Запускает отдельную от бота команду без печати реквизитов."""
    try:
        counts = asyncio.run(seed_stand())
    except DemoError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Учебный набор не записан полностью: {type(error).__name__}.", file=sys.stderr)
        return 1
    print("Учебный набор: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
