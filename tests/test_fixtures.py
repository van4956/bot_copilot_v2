"""Проверки искусственных фикстур и безопасного заполнения test-БД."""

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config_data.config import ROOT
from database.models import Base, Cookbook, Games, Users
from testing import seed_test_data
from testing.prepare_fixtures import RECORD_ID_BASE, prepare_fixtures


@pytest.fixture
def fixture_workspace(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Выделяет временную папку строго внутри .test-data/tmp проекта."""
    monkeypatch.setenv("APP_ENV", "test")
    parent = (ROOT / ".test-data" / "tmp").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="fixtures-", dir=parent)).resolve()
    assert parent in folder.parents
    try:
        yield folder
    finally:
        if parent not in folder.resolve().parents:
            raise RuntimeError("Временная папка вышла за пределы test-data")
        shutil.rmtree(folder)


def _hashes(folder: Path) -> dict[str, str]:
    """Вычисляет контрольные суммы содержимого без временных меток."""
    return {
        str(path.relative_to(folder)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(folder.rglob("*")) if path.is_file()
    }


def test_generation_is_synthetic_and_repeatable(
    fixture_workspace: Path,
) -> None:
    """Повторная генерация сохраняет JSON и пиксели без изменений."""
    output = fixture_workspace / "fixtures"
    counts = prepare_fixtures(output)
    first_hashes = _hashes(output)

    assert prepare_fixtures(output) == counts
    assert _hashes(output) == first_hashes
    assert counts == {"users": 120, "recipes": 5, "games": 30}
    users = json.loads((output / "users.json").read_text(encoding="utf-8"))
    recipes = json.loads((output / "recipes.json").read_text(encoding="utf-8"))
    games = json.loads((output / "games.json").read_text(encoding="utf-8"))
    assert min(row["user_id"] for row in users) == 9_000_000_001
    assert len({row["user_id"] for row in users}) == 120
    assert {row["locale"] for row in users} == {"ru", "en"}
    assert {row["status"] for row in users} == {"member", "kicked"}
    assert any(not row["user_name"] for row in users)
    assert {row["game_name"] for row in games} == {"snake", "platform"}
    assert any(row["user_name"] is None for row in games)
    assert [row["image"] for row in recipes] == [
        f"test-fixture:{number}" for number in range(1, 6)
    ]
    for number in range(1, 6):
        image_path = output / "images" / f"test_image_{number}.png"
        with Image.open(image_path) as image:
            assert image.size == (800, 600)
            assert image.mode == "RGB"
            assert image.info["caption"] == f"тест картинка {number}"
            assert image.getpixel((0, 0)) == (255, 255, 255)
            assert image.getextrema() == ((0, 255), (0, 255), (0, 255))


async def _row_counts(database_url: str) -> dict[str, int]:
    """Читает размеры таблиц и закрывает все соединения."""
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine)
        async with factory() as session:
            return {
                name: await session.scalar(
                    select(func.count()).select_from(model)
                )
                for name, model in (
                    ("users", Users), ("recipes", Cookbook), ("games", Games)
                )
            }
    finally:
        await engine.dispose()


def test_seed_is_idempotent(fixture_workspace: Path) -> None:
    """Повторный seed не создаёт дубликатов и не меняет записи."""
    output = fixture_workspace / "fixtures"
    prepare_fixtures(output)
    url = f"sqlite+aiosqlite:///{fixture_workspace / 'seed.sqlite3'}"

    first = asyncio.run(seed_test_data.seed_database(url, output))
    second = asyncio.run(seed_test_data.seed_database(url, output))

    assert first == {
        "users_added": 120, "users_existing": 0,
        "recipes_added": 5, "recipes_existing": 0,
        "games_added": 30, "games_existing": 0,
    }
    assert second == {
        "users_added": 0, "users_existing": 120,
        "recipes_added": 0, "recipes_existing": 5,
        "games_added": 0, "games_existing": 30,
    }
    assert asyncio.run(_row_counts(url)) == {
        "users": 120, "recipes": 5, "games": 30
    }


async def _insert_collision(database_url: str) -> None:
    """Создаёт искусственную постороннюю запись для проверки отказа."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine)
        async with factory() as session, session.begin():
            session.add(Cookbook(
                recipe_id=RECORD_ID_BASE + 5,
                recipe_name="Посторонняя тестовая запись",
                author="Другой тест", description="Не перезаписывать",
                price=1, image="unrelated-test-image",
            ))
    finally:
        await engine.dispose()


def test_collision_aborts_before_any_insert(fixture_workspace: Path) -> None:
    """Чужая запись сохраняется, частичный seed не выполняется."""
    output = fixture_workspace / "fixtures"
    prepare_fixtures(output)
    url = f"sqlite+aiosqlite:///{fixture_workspace / 'collision.sqlite3'}"
    asyncio.run(_insert_collision(url))

    with pytest.raises(ValueError, match="Конфликт фикстуры"):
        asyncio.run(seed_test_data.seed_database(url, output))

    assert asyncio.run(_row_counts(url)) == {
        "users": 0, "recipes": 1, "games": 0
    }


def test_production_mode_is_rejected_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-окружение запрещает seed до создания движка."""
    monkeypatch.setenv("APP_ENV", "production")
    create_engine = Mock(side_effect=AssertionError("Подключение запрещено"))
    monkeypatch.setattr(seed_test_data, "create_async_engine", create_engine)

    with pytest.raises(ValueError, match="APP_ENV=test"):
        asyncio.run(seed_test_data.seed_database(
            "sqlite+aiosqlite:///:memory:"
        ))
    create_engine.assert_not_called()


@pytest.mark.parametrize("database_url", [
    "postgresql+asyncpg://user:password@remote.example/copilot_review_test",
    "postgresql+asyncpg://user:password@localhost/production",
    "sqlite+aiosqlite:///production.sqlite3",
])
def test_foreign_database_is_rejected_before_connect(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    """Посторонние адреса отклоняются общим валидатором проекта."""
    monkeypatch.setenv("APP_ENV", "test")
    create_engine = Mock(side_effect=AssertionError("Подключение запрещено"))
    monkeypatch.setattr(seed_test_data, "create_async_engine", create_engine)

    with pytest.raises(ValueError, match="Тестовая база"):
        asyncio.run(seed_test_data.seed_database(database_url))
    create_engine.assert_not_called()
