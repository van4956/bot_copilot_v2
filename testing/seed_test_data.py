"""Безопасное добавление искусственных фикстур только в локальную test-БД."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config_data.config import validate_test_database
from database.models import Base, Cookbook, Games, Users
from testing.prepare_fixtures import FIXTURE_DIR

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TABLES = {
    "users": (Users, "user_id"),
    "recipes": (Cookbook, "recipe_id"),
    "games": (Games, "game_id"),
}


def load_fixture_records(
    fixtures_dir: Path = FIXTURE_DIR,
) -> dict[str, list[dict[str, Any]]]:
    """Читает списки записей и исключает повторяющиеся идентификаторы."""
    result = {}
    for name, (_, primary_key) in FIXTURE_TABLES.items():
        rows = json.loads(
            (Path(fixtures_dir) / f"{name}.json").read_text(encoding="utf-8")
        )
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) and primary_key in row for row in rows
        ):
            raise ValueError(f"Некорректная фикстура: {name}")
        keys = [row[primary_key] for row in rows]
        if not all(type(key) is int for key in keys):
            raise ValueError(f"Некорректный идентификатор фикстуры: {name}")
        if len(keys) != len(set(keys)):
            raise ValueError(f"Повтор идентификатора в фикстуре: {name}")
        result[name] = rows
    return result


async def seed_database(
    database_url: str, fixtures_dir: Path = FIXTURE_DIR
) -> dict[str, int]:
    """Добавляет отсутствующие фикстуры, не меняя существующие записи."""
    if os.getenv("APP_ENV", "test").lower() != "test":
        raise ValueError("Фикстуры разрешены только при APP_ENV=test")
    validate_test_database(database_url)
    records = load_fixture_records(fixtures_dir)
    parsed = make_url(database_url)
    if parsed.drivername.startswith("sqlite"):
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(database_url)
    counts = {}
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            pending = []
            for name, (model, primary_key) in FIXTURE_TABLES.items():
                added = 0
                existing = 0
                for row in records[name]:
                    current = await session.get(model, row[primary_key])
                    if current is not None:
                        if any(
                            getattr(current, field) != value
                            for field, value in row.items()
                        ):
                            raise ValueError(
                                f"Конфликт фикстуры: {name}, "
                                f"{primary_key}={row[primary_key]}"
                            )
                        existing += 1
                    else:
                        pending.append(model(**row))
                        added += 1
                counts[f"{name}_added"] = added
                counts[f"{name}_existing"] = existing
            # Запись начинается лишь после проверки коллизий во всех таблицах.
            session.add_all(pending)
    finally:
        await engine.dispose()
    return counts


def main() -> None:
    """Запускает заполнение без чтения .env и без вывода адреса БД."""
    parser = argparse.ArgumentParser(description=__doc__)
    default_url = (
        f"sqlite+aiosqlite:///{ROOT / '.test-data' / 'copilot_review.sqlite3'}"
    )
    parser.add_argument(
        "--database-url", default=os.getenv("TEST_DATABASE_URL", default_url)
    )
    parser.add_argument("--fixtures-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args()
    try:
        counts = asyncio.run(seed_database(
            args.database_url, args.fixtures_dir
        ))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Фикстуры не записаны: {error}\n")
    except SQLAlchemyError:
        parser.exit(1, "Ошибка тестовой БД; фикстуры не записаны.\n")
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
